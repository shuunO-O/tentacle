"""TENTACLE_HOME 定期备份与迁移导入/导出模块（覆盖 workspace 之外的持久化数据）。"""

from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from datetime import datetime, timezone
import fnmatch
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from dotenv.parser import parse_stream

from attachments.outgoing import _SECRET_DIRS, _SECRET_NAMES
from config.environment import MAX_DOTENV_BYTES
from config.files import ConfigFiles, parse_config
from config.google_auth import credential_paths
from config.providers import validate_structure
from runtime.paths import APP_ROOT

logger = logging.getLogger(__name__)

BACKUP_SCHEMA_VERSION = 1
ENCRYPTED_MAGIC = b"TENTBK1\x00"
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
CRYPTO_CHUNK_SIZE = 1024 * 1024
MAX_CRYPTO_HEADER_BYTES = 16 * 1024

BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}\Z")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*\Z")

BACKUP_DIR_REL = "data/backups"
STAGING_DIR_REL = "data/backup_staging"
POLICY_REL = "data/backup_policy.json"
LAST_RESTORE_REL = "data/backup_last_restore.json"
PENDING_APPLY_REL = "data/backup_staging/pending_apply.json"

DEFAULT_INTERVAL_HOURS = 24
DEFAULT_RETENTION_COUNT = 7
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 720
MIN_RETENTION_COUNT = 1
MAX_RETENTION_COUNT = 100
PRE_IMPORT_RETENTION = 3
SCHEDULER_POLL_SECONDS = 60.0
MIN_PASSPHRASE_LENGTH = 8

MAX_IMPORT_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_IMPORT_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_IMPORT_FILES = 50_000
STAGING_TTL_SECONDS = 2 * 3600

MANAGED_CONTENT_DIRS = ("prompt", "skills", "plugins")
BASE_BACKUP_SCOPES = ("config", "data", "prompt", "skills", "plugins")
MAX_WORKSPACE_SELECTION = 500
MAX_WORKSPACE_TREE_ENTRIES = 500
IGNORED_TREE_NAMES = frozenset({
    ".git", ".venv", ".dev-python", "__pycache__", "node_modules",
    ".pytest_cache", ".ruff_cache", ".cache", "tmp", "logs",
})
EXTRA_SECRET_FILES = frozenset({
    "webui_settings.json",
    "device_enroll_pending.json",
    "device_auth_freeze.json",
    "device_access_policy.json",
})
DATA_EXCLUDED_SUBDIRS = frozenset({
    "backups",
    "backup_staging",
    "self_backups",
    "config_backups",
    "dsh",
    "history_repairs",
})
DATA_EXCLUDED_FILES = frozenset({
    "backup_policy.json",
    "backup_last_restore.json",
    "gemini_explicit_cache.json",
    "webui_settings.json",
    "device_enroll_pending.json",
    "device_auth_freeze.json",
    "device_access_policy.json",
})
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
LADYBUG_SIDECAR_SUFFIXES = (".wal", ".shadow", ".tmp")

DEFAULT_POLICY: dict = {
    "enabled": True,
    "interval_hours": DEFAULT_INTERVAL_HOURS,
    "retention_count": DEFAULT_RETENTION_COUNT,
    "include_files": True,
    "last_auto_backup_at": None,
    "last_status": None,
    "last_error": None,
    "last_backup_id": None,
}


class BackupError(ValueError):
    """可展示给前端或 CLI 的备份/恢复业务错误（不含凭据明文）。"""


class BackupConflictError(BackupError):
    """备份或恢复操作正在进行中，发生并发冲突。"""


class PassphraseRequiredError(BackupError):
    """上传或指定的归档为加密格式，缺少解密口令。"""


def read_data_epoch() -> int:
    """读取当前代码版本对应的数据兼容代号。"""
    raw = (APP_ROOT / "runtime" / "data_epoch.txt").read_text(encoding="utf-8").strip()
    return int(raw)


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        _chmod_safe(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: object, mode: int = 0o600) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, encoded, mode=mode)


def _chmod_safe(path: Path, mode: int) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod_safe(path, 0o700)


def _safe_rel_posix(raw_rel: str) -> str | None:
    """校验归档或相对路径不含路径穿越、绝对前缀、控制符或空段。"""
    if not raw_rel or "\x00" in raw_rel or "\\" in raw_rel or raw_rel.startswith("/"):
        return None
    if len(raw_rel) >= 2 and raw_rel[1] == ":" and raw_rel[0].isalpha():
        return None
    posix = PurePosixPath(raw_rel)
    if posix.is_absolute() or not posix.parts:
        return None
    if any(part in {"", ".", ".."} for part in posix.parts):
        return None
    normalized = posix.as_posix()
    return normalized if normalized == raw_rel.strip("/") else None


def _is_secret_rel(rel_posix: str, *, allow_config_env: bool = False) -> bool:
    if rel_posix == "config/.env":
        return not allow_config_env
    parts = [p.lower() for p in PurePosixPath(rel_posix).parts]
    if parts and parts[0] == "certs":
        return True
    if any(part in _SECRET_DIRS for part in parts[:-1]):
        return True
    for part in parts:
        if part in EXTRA_SECRET_FILES:
            return True
        if any(fnmatch.fnmatch(part, pat) for pat in _SECRET_NAMES):
            return True
    return False


def _is_excluded_data_rel(rel_posix: str, *, include_files: bool) -> bool:
    parts = PurePosixPath(rel_posix).parts
    if not parts or parts[0] != "data" or len(parts) < 2:
        return True
    sub = parts[1]
    if sub in DATA_EXCLUDED_SUBDIRS:
        return True
    if len(parts) == 2 and sub in DATA_EXCLUDED_FILES:
        return True
    if sub == "files" and not include_files:
        return True
    if any(p in IGNORED_TREE_NAMES for p in parts[1:]):
        return True
    return False


def _is_allowed_payload_rel(
    rel_posix: str,
    *,
    include_files: bool,
    allow_env: bool,
    allow_workspace: bool = False,
    scopes: tuple[str, ...] = BASE_BACKUP_SCOPES,
) -> bool:
    if _is_secret_rel(rel_posix, allow_config_env=allow_env):
        return False
    parts = PurePosixPath(rel_posix).parts
    if not parts:
        return False
    top = parts[0]
    if top == "workspace":
        return (
            allow_workspace
            and len(parts) >= 2
            and not any(p in IGNORED_TREE_NAMES for p in parts[1:])
        )
    if rel_posix == "config/config.yaml":
        return "config" in scopes
    if rel_posix == "config/.env":
        return allow_env
    if top == "config":
        return False
    if top in MANAGED_CONTENT_DIRS:
        return top in scopes and len(parts) >= 2 and not any(p in IGNORED_TREE_NAMES for p in parts[1:])
    if top == "data":
        if len(parts) >= 2 and parts[1] == "files":
            return include_files and not any(p in IGNORED_TREE_NAMES for p in parts[2:])
        return "data" in scopes and not _is_excluded_data_rel(rel_posix, include_files=include_files)
    return False


def normalize_backup_scopes(scopes: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if scopes is None:
        return BASE_BACKUP_SCOPES
    result: list[str] = []
    for item in scopes:
        if not isinstance(item, str) or item not in BASE_BACKUP_SCOPES:
            raise BackupError(f"不支持的备份模块：{item}")
        if item not in result:
            result.append(item)
    return tuple(result)


def _resolve_workspace_child(ws_root: Path, rel_norm: str) -> Path:
    cur = ws_root
    if cur.is_symlink():
        raise BackupError("workspace 目录不能是符号链接")
    for part in PurePosixPath(rel_norm).parts:
        if part in IGNORED_TREE_NAMES or part.lower() in _SECRET_DIRS:
            raise BackupError(f"工作区路径属于排除或受限目录：{rel_norm}")
        cur = cur / part
        if cur.is_symlink():
            raise BackupError(f"工作区路径包含符号链接：{rel_norm}")
    resolved_ws = ws_root.resolve()
    resolved_cur = cur.resolve()
    if resolved_cur != resolved_ws and not resolved_cur.is_relative_to(resolved_ws):
        raise BackupError(f"工作区路径越界：{rel_norm}")
    return cur


def normalize_workspace_paths(
    state_root: Path,
    workspace_paths: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    if not workspace_paths:
        return ()
    if len(workspace_paths) > MAX_WORKSPACE_SELECTION:
        raise BackupError(f"一次最多只能选择 {MAX_WORKSPACE_SELECTION} 个工作区路径")
    ws_root = state_root / "workspace"
    if not ws_root.is_dir() or ws_root.is_symlink():
        raise BackupError("当前实例 workspace 目录不存在或不可访问")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in workspace_paths:
        if not isinstance(raw, str):
            raise BackupError("工作区路径格式无效")
        cleaned = raw.strip().strip("/")
        if cleaned.startswith("workspace/"):
            cleaned = cleaned[len("workspace/"):]
        norm = _safe_rel_posix(cleaned)
        if not norm:
            raise BackupError(f"工作区路径无效：{raw}")
        if _is_secret_rel(f"workspace/{norm}"):
            raise BackupError(f"工作区路径包含受保护凭据文件，不允许备份：{norm}")
        target = _resolve_workspace_child(ws_root, norm)
        if not target.exists():
            raise BackupError(f"选中的工作区路径不存在：{norm}")
        if norm not in seen:
            seen.add(norm)
            normalized.append(norm)
    return tuple(normalized)


def list_workspace_tree(
    state_root: Path,
    rel_path: str = "",
    *,
    limit: int = MAX_WORKSPACE_TREE_ENTRIES,
) -> dict:
    """安全浏览 TENTACLE_HOME/workspace 下允许勾选的目录与文件（不跟随符号链接，跳过敏感路径）。"""
    ws_root = state_root / "workspace"
    raw = str(rel_path or "").strip().strip("/")
    if raw.startswith("workspace/"):
        raw = raw[len("workspace/"):]
    elif raw == "workspace":
        raw = ""

    if not ws_root.exists() or ws_root.is_symlink() or not ws_root.is_dir():
        return {"exists": False, "current_path": "", "parent_path": None, "entries": [], "truncated": False}

    if raw:
        norm = _safe_rel_posix(raw)
        if not norm:
            raise BackupError("工作区路径格式无效")
        target_dir = _resolve_workspace_child(ws_root, norm)
        if not target_dir.is_dir():
            raise BackupError("目标不是可浏览的工作区目录")
    else:
        norm = ""
        target_dir = ws_root

    items: list[dict] = []
    try:
        children = list(target_dir.iterdir())
    except OSError as exc:
        raise BackupError("读取工作区目录失败") from exc

    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    truncated = False
    for child in children:
        if child.is_symlink():
            continue
        name = child.name
        if name in IGNORED_TREE_NAMES or name.lower() in _SECRET_DIRS:
            continue
        child_rel = f"{norm}/{name}" if norm else name
        if _is_secret_rel(f"workspace/{child_rel}"):
            continue
        is_dir = child.is_dir()
        if not is_dir and not child.is_file():
            continue
        if len(items) >= limit:
            truncated = True
            break
        try:
            st = child.stat()
            size = 0 if is_dir else st.st_size
            mtime = float(st.st_mtime)
        except OSError:
            continue
        items.append({
            "path": child_rel,
            "name": name,
            "is_dir": is_dir,
            "size": size,
            "mtime": mtime,
        })

    parent_path = None if not norm else ("" if "/" not in norm else norm.rsplit("/", 1)[0])
    return {
        "exists": True,
        "current_path": norm,
        "parent_path": parent_path,
        "entries": items,
        "truncated": truncated,
    }


def _load_effective_config_safe(state_root: Path, raw_config: dict | None = None) -> dict:
    if isinstance(raw_config, dict):
        return raw_config
    cfg_path = state_root / "config" / "config.yaml"
    defaults_path = APP_ROOT / "config.defaults.yaml"
    try:
        files = ConfigFiles(cfg_path)
        files.defaults_path = defaults_path
        return files.read()[2]
    except Exception:
        try:
            return parse_config(defaults_path.read_text(encoding="utf-8"))
        except Exception:
            return {}


def _configured_lbdb_rel(state_root: Path, effective_cfg: dict) -> str:
    raw_lbdb = (
        ((effective_cfg.get("memory") or {}).get("ladybug") or {}).get("path")
        or "data/memory.lbdb"
    )
    return _normalize_custom_state_rel(state_root, str(raw_lbdb), default="data/memory.lbdb")


def _normalize_custom_state_rel(state_root: Path, path_str: str, *, default: str) -> str:
    p = Path(str(path_str or default)).expanduser()
    if not p.is_absolute():
        p = state_root / p
    resolved_root = state_root.resolve()
    try:
        rel = p.resolve().relative_to(resolved_root).as_posix()
    except ValueError:
        return default
    return rel


def collect_referenced_env_vars(effective_cfg: dict) -> list[str]:
    """收集当前配置引用的环境变量名（只返回键名，绝不读取值）。"""
    seen: list[str] = []

    def add(name: object) -> None:
        if isinstance(name, str):
            val = name.strip()
            if ENV_NAME_RE.fullmatch(val) and val not in seen:
                seen.append(val)

    providers = effective_cfg.get("providers") or {}
    if isinstance(providers, dict):
        for item in providers.values():
            if isinstance(item, dict):
                add(item.get("api_key_env"))
                add(item.get("credentials_env"))
                add(item.get("project_env"))

    models = effective_cfg.get("models") or {}
    if isinstance(models, dict):
        for item in models.values():
            if isinstance(item, dict):
                add(item.get("api_key_env"))
                add(item.get("credentials_env"))
                add(item.get("project_env"))

    add(((effective_cfg.get("web_tools") or {}).get("api_key_env")))
    add((((effective_cfg.get("media_inputs") or {}).get("download") or {}).get("api_key_env")))

    plugin_items = ((effective_cfg.get("plugins") or {}).get("items") or {})
    if isinstance(plugin_items, dict):
        from plugins.manifest import bundled_plugins_dir, parse_manifest

        bundled_root = bundled_plugins_dir()
        for pid, item in plugin_items.items():
            if not isinstance(item, dict) or not item.get("enabled"):
                continue
            item_cfg = item.get("config") if isinstance(item.get("config"), dict) else {}
            manifest = None
            entry = bundled_root / str(pid)
            if entry.is_dir():
                try:
                    manifest = parse_manifest(entry)
                except Exception:
                    manifest = None
            if manifest is not None:
                for env_name in (*manifest.requires_env, *manifest.optional_env):
                    add(env_name)
                for key, spec in manifest.config_schema.items():
                    if spec.type == "env":
                        add(item_cfg.get(key) if key in item_cfg else spec.default)
            for key, val in item_cfg.items():
                if isinstance(key, str) and key.endswith("_env"):
                    add(val)

    uploaders = ((effective_cfg.get("files") or {}).get("uploaders") or {})
    if isinstance(uploaders, dict):
        for item in uploaders.values():
            if isinstance(item, dict):
                add(item.get("api_key_env"))

    mcp_map = ((effective_cfg.get("plugins") or {}).get("mcp") or {})
    if isinstance(mcp_map, dict):
        for item in mcp_map.values():
            if not isinstance(item, dict):
                continue
            add(item.get("auth_bearer_env"))
            for field in ("env_secrets", "header_secrets"):
                mapping = item.get(field) or {}
                if isinstance(mapping, dict):
                    for host_var in mapping.values():
                        add(host_var)

    return seen


def read_dotenv_key_names(dotenv_path: Path) -> set[str]:
    """仅解析 .env 中的变量名集合，不保留或返回任何凭据值。"""
    if dotenv_path.is_symlink() or not dotenv_path.is_file():
        return set()
    try:
        raw = dotenv_path.read_bytes()
        if len(raw) > MAX_DOTENV_BYTES:
            return set()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    keys: set[str] = set()
    for entry in parse_stream(io.StringIO(text)):
        if not entry.error and entry.key and ENV_NAME_RE.fullmatch(entry.key):
            keys.add(entry.key)
    return keys


def compute_missing_env_vars(state_root: Path, required_vars: list[str]) -> list[str]:
    dotenv_keys = read_dotenv_key_names(state_root / "config" / ".env")
    missing = []
    for name in required_vars:
        if os.environ.get(name, "").strip():
            continue
        if name in dotenv_keys:
            continue
        missing.append(name)
    return missing


def _validate_dotenv_bytes(raw: bytes) -> None:
    if len(raw) > MAX_DOTENV_BYTES:
        raise BackupError(".env 超过 1 MiB 大小限制")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupError("导入包内的 .env 不是有效 UTF-8 文本") from exc
    for entry in parse_stream(io.StringIO(text)):
        if entry.error:
            raise BackupError("导入包内的 .env 包含无效语法")


def _validate_config_yaml_bytes(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BackupError("导入包内的 config.yaml 不是有效 UTF-8 编码") from exc
    try:
        parsed = parse_config(text)
        defaults_path = APP_ROOT / "config.defaults.yaml"
        defaults = parse_config(defaults_path.read_text(encoding="utf-8")) if defaults_path.is_file() else {}
        from config.files import merge_config
        merged = merge_config(defaults, parsed)
        validate_structure(merged)
    except Exception as exc:
        raise BackupError(f"导入包内的 config.yaml 校验失败：{exc}") from exc


def _protected_host_paths() -> set[Path]:
    protected: set[Path] = set()
    for item in credential_paths():
        try:
            protected.add(Path(item).expanduser().resolve())
        except OSError:
            pass
    if push_cred := os.environ.get("HUAWEI_PUSH_CREDENTIALS_FILE", "").strip():
        try:
            protected.add(Path(push_cred).expanduser().resolve())
        except OSError:
            pass
    return protected


def _is_sqlite_file(path: Path) -> bool:
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _snapshot_sqlite_sync(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(str(src), timeout=15.0)
        dst_conn = sqlite3.connect(str(dst))
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()


def _copy_regular_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _ladybug_has_sidecars(lbdb_path: Path) -> bool:
    return any(os.path.lexists(str(lbdb_path) + s) for s in LADYBUG_SIDECAR_SUFFIXES)


def _checkpoint_and_copy_closed_lbdb(lbdb_path: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _ladybug_has_sidecars(lbdb_path):
        import ladybug

        db = ladybug.Database(str(lbdb_path))
        try:
            with closing(ladybug.Connection(db)) as conn:
                res = conn.execute("CHECKPOINT")
                res.close()
        finally:
            db.close()
    shutil.copy2(lbdb_path, dst)


def _checkpoint_and_copy_open_lbdb(database, lbdb_path: Path, dst: Path) -> None:
    import ladybug

    dst.parent.mkdir(parents=True, exist_ok=True)
    with closing(ladybug.Connection(database)) as conn:
        res = conn.execute("CHECKPOINT")
        res.close()
    shutil.copy2(lbdb_path, dst)


async def _snapshot_ladybug(lbdb_path: Path, dst: Path, ladybug_store=None) -> None:
    if lbdb_path.is_symlink() or not lbdb_path.is_file():
        return
    driver = getattr(ladybug_store, "driver", None) if ladybug_store is not None else None
    same_driver = False
    if driver is not None:
        try:
            same_driver = Path(driver.path).resolve() == lbdb_path.resolve()
        except OSError:
            same_driver = False

    if same_driver and getattr(driver, "database", None) is not None:
        async with driver._write_lock:
            await asyncio.to_thread(_checkpoint_and_copy_open_lbdb, driver.database, lbdb_path, dst)
        return
    if same_driver and hasattr(driver, "_ready_lock"):
        async with driver._ready_lock:
            await asyncio.to_thread(_checkpoint_and_copy_closed_lbdb, lbdb_path, dst)
        return
    await asyncio.to_thread(_checkpoint_and_copy_closed_lbdb, lbdb_path, dst)


def _sha256_file(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(256 * 1024):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def _derive_scrypt_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_archive_file(src_tar_gz: Path, dst_enc: Path, passphrase: str) -> None:
    """使用 Scrypt + 分块 AES-256-GCM 流式加密备份归档，恒定低内存开销。"""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise BackupError(f"加密口令至少需要 {MIN_PASSPHRASE_LENGTH} 个字符")
    salt = secrets.token_bytes(16)
    nonce_prefix = secrets.token_bytes(8)
    header_obj = {
        "v": 1,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce_prefix": base64.b64encode(nonce_prefix).decode("ascii"),
        "chunk_size": CRYPTO_CHUNK_SIZE,
    }
    header_bytes = json.dumps(header_obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    key = _derive_scrypt_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    aesgcm = AESGCM(key)

    dst_enc.parent.mkdir(parents=True, exist_ok=True)
    with src_tar_gz.open("rb") as src, dst_enc.open("wb") as dst:
        dst.write(ENCRYPTED_MAGIC)
        dst.write(len(header_bytes).to_bytes(4, "big"))
        dst.write(header_bytes)
        idx = 0
        current = src.read(CRYPTO_CHUNK_SIZE)
        while True:
            nxt = src.read(CRYPTO_CHUNK_SIZE)
            is_final = len(nxt) == 0
            nonce = nonce_prefix + idx.to_bytes(4, "big")
            aad = header_bytes + idx.to_bytes(4, "big") + (b"\x01" if is_final else b"\x00")
            ct = aesgcm.encrypt(nonce, current, aad)
            dst.write(len(ct).to_bytes(4, "big"))
            dst.write(ct)
            if is_final:
                break
            current = nxt
            idx += 1
        dst.flush()
    _chmod_safe(dst_enc, 0o600)


def is_encrypted_archive(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(len(ENCRYPTED_MAGIC)) == ENCRYPTED_MAGIC


def decrypt_archive_file(src_enc: Path, dst_tar_gz: Path, passphrase: str) -> None:
    """流式解密并严格校验分块 AES-256-GCM 完整性与末尾终结标记。"""
    if not passphrase:
        raise PassphraseRequiredError("此迁移包已加密，请输入导出时设置的解密口令")
    with src_enc.open("rb") as src:
        magic = src.read(len(ENCRYPTED_MAGIC))
        if magic != ENCRYPTED_MAGIC:
            raise BackupError("不支持的加密备份格式")
        raw_hlen = src.read(4)
        if len(raw_hlen) != 4:
            raise BackupError("加密备份头不完整")
        hlen = int.from_bytes(raw_hlen, "big")
        if hlen <= 2 or hlen > MAX_CRYPTO_HEADER_BYTES:
            raise BackupError("加密备份头大小无效")
        header_bytes = src.read(hlen)
        if len(header_bytes) != hlen:
            raise BackupError("加密备份头截断")
        try:
            header = json.loads(header_bytes.decode("utf-8"))
            salt = base64.b64decode(header["salt"], validate=True)
            nonce_prefix = base64.b64decode(header["nonce_prefix"], validate=True)
            n = int(header["n"])
            r = int(header["r"])
            p = int(header["p"])
            chunk_size = int(header["chunk_size"])
        except Exception as exc:
            raise BackupError("加密备份元数据损坏") from exc
        if (
            header.get("v") != 1
            or header.get("kdf") != "scrypt"
            or len(salt) != 16
            or len(nonce_prefix) != 8
            or not (1024 <= n <= (1 << 16))
            or not (1 <= r <= 16)
            or not (1 <= p <= 4)
            or not (1024 <= chunk_size <= 8 * 1024 * 1024)
        ):
            raise BackupError("加密备份参数不受支持")

        key = _derive_scrypt_key(passphrase, salt, n, r, p)
        aesgcm = AESGCM(key)

        dst_tar_gz.parent.mkdir(parents=True, exist_ok=True)
        with dst_tar_gz.open("wb") as dst:
            idx = 0
            saw_final = False
            while True:
                raw_clen = src.read(4)
                if len(raw_clen) == 0:
                    break
                if len(raw_clen) != 4:
                    raise BackupError("加密备份数据块截断")
                clen = int.from_bytes(raw_clen, "big")
                if clen < 16 or clen > chunk_size + 16:
                    raise BackupError("加密备份数据块长度异常")
                ct = src.read(clen)
                if len(ct) != clen:
                    raise BackupError("加密备份数据块不完整")
                nonce = nonce_prefix + idx.to_bytes(4, "big")
                # 先探是否已到文件尾（用于区分普通块和终结块 AAD，防截断攻击）
                peek = src.read(1)
                is_final = len(peek) == 0
                if peek:
                    src.seek(-1, os.SEEK_CUR)
                aad = header_bytes + idx.to_bytes(4, "big") + (b"\x01" if is_final else b"\x00")
                try:
                    pt = aesgcm.decrypt(nonce, ct, aad)
                except InvalidTag as exc:
                    raise BackupError("解密口令错误或备份文件已损坏") from exc
                dst.write(pt)
                if is_final:
                    saw_final = True
                    break
                idx += 1
            if not saw_final:
                raise BackupError("加密备份缺少完整结束标记")
            dst.flush()
    _chmod_safe(dst_tar_gz, 0o600)


def _scan_payload_sources(
    state_root: Path,
    *,
    include_files: bool,
    include_env: bool,
    lbdb_rel: str,
    scopes: tuple[str, ...] = BASE_BACKUP_SCOPES,
    workspace_paths: tuple[str, ...] = (),
) -> tuple[list[tuple[str, Path, str]], str | None]:
    """扫描待打包文件，返回 ([(rel_posix, abs_path, kind)], lbdb_abs_rel_or_None)。
    kind 为 'sqlite' | 'file' | 'ladybug'。
    """
    entries: list[tuple[str, Path, str]] = []
    seen_rels: set[str] = set()
    protected = _protected_host_paths()

    def is_protected(path: Path) -> bool:
        try:
            return path.resolve() in protected
        except OSError:
            return True

    def add_entry(rel: str, src: Path, kind: str) -> None:
        if rel not in seen_rels:
            seen_rels.add(rel)
            entries.append((rel, src, kind))

    if "config" in scopes:
        cfg_yaml = state_root / "config" / "config.yaml"
        if cfg_yaml.is_file() and not cfg_yaml.is_symlink() and not is_protected(cfg_yaml):
            add_entry("config/config.yaml", cfg_yaml, "file")

    if include_env:
        dotenv_file = state_root / "config" / ".env"
        if dotenv_file.is_file() and not dotenv_file.is_symlink():
            add_entry("config/.env", dotenv_file, "file")

    scan_dirs: list[tuple[str, Path]] = [(top, state_root / top) for top in MANAGED_CONTENT_DIRS if top in scopes]
    if "data" in scopes:
        scan_dirs.append(("data", state_root / "data"))
    elif include_files:
        scan_dirs.append(("data", state_root / "data" / "files"))

    for top, base in scan_dirs:
        if not base.exists() or base.is_symlink() or not base.is_dir():
            continue
        for current_dir, dirnames, filenames in os.walk(base, followlinks=False):
            cur = Path(current_dir)
            rel_dir = cur.relative_to(state_root).as_posix()
            # 过滤目录避免进入 node_modules / backups / files / secrets 等
            kept_dirs = []
            for dname in sorted(dirnames):
                child = cur / dname
                if child.is_symlink():
                    continue
                if dname in IGNORED_TREE_NAMES or dname.lower() in _SECRET_DIRS:
                    continue
                if top == "data" and cur == state_root / "data":
                    if dname in DATA_EXCLUDED_SUBDIRS or (dname == "files" and not include_files):
                        continue
                kept_dirs.append(dname)
            dirnames[:] = kept_dirs

            for fname in sorted(filenames):
                src = cur / fname
                if src.is_symlink() or not src.is_file():
                    continue
                rel = f"{rel_dir}/{fname}"
                if top == "data" and "data" in scopes:
                    if rel == lbdb_rel:
                        add_entry(rel, src, "ladybug")
                        continue
                    if any(fname.endswith(s) for s in LADYBUG_SIDECAR_SUFFIXES) and f"{rel_dir}/{fname.rsplit('.', 1)[0]}" == lbdb_rel:
                        continue
                    if fname.startswith(".") and fname.endswith(".reembed.lock"):
                        continue
                    if any(fname.endswith(s) for s in SQLITE_SIDECAR_SUFFIXES):
                        base_db = fname.rsplit("-", 1)[0]
                        if base_db.endswith(".db"):
                            continue
                if not _is_allowed_payload_rel(
                    rel,
                    include_files=include_files,
                    allow_env=False,
                    allow_workspace=False,
                    scopes=scopes,
                ):
                    continue
                if is_protected(src):
                    continue
                kind = "sqlite" if (top == "data" and "data" in scopes and fname.endswith(".db") and _is_sqlite_file(src)) else "file"
                add_entry(rel, src, kind)

    if workspace_paths:
        ws_root = state_root / "workspace"
        for wrel in workspace_paths:
            src_root = _resolve_workspace_child(ws_root, wrel)
            if src_root.is_file():
                rel = f"workspace/{wrel}"
                if _is_allowed_payload_rel(rel, include_files=False, allow_env=False, allow_workspace=True, scopes=scopes) and not is_protected(src_root):
                    add_entry(rel, src_root, "file")
                continue
            if src_root.is_dir():
                for current_dir, dirnames, filenames in os.walk(src_root, followlinks=False):
                    cur = Path(current_dir)
                    kept_dirs = []
                    for dname in sorted(dirnames):
                        child = cur / dname
                        if child.is_symlink() or dname in IGNORED_TREE_NAMES or dname.lower() in _SECRET_DIRS:
                            continue
                        kept_dirs.append(dname)
                    dirnames[:] = kept_dirs
                    rel_dir = cur.relative_to(state_root).as_posix()
                    for fname in sorted(filenames):
                        src = cur / fname
                        if src.is_symlink() or not src.is_file():
                            continue
                        rel = f"{rel_dir}/{fname}"
                        if not _is_allowed_payload_rel(
                            rel,
                            include_files=False,
                            allow_env=False,
                            allow_workspace=True,
                            scopes=scopes,
                        ):
                            continue
                        if is_protected(src):
                            continue
                        add_entry(rel, src, "file")

    entries.sort(key=lambda item: item[0])
    return entries, lbdb_rel if any(k == "ladybug" for _, _, k in entries) else None


def _estimate_entries_bytes(entries: list[tuple[str, Path, str]]) -> int:
    total = 0
    for _, src, _ in entries:
        try:
            total += src.stat().st_size
        except OSError:
            pass
    return total


def _check_free_space(target_dir: Path, required_bytes: int) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target_dir).free
    needed = int(required_bytes * 1.2) + 8 * 1024 * 1024
    if free < needed:
        raise BackupError("磁盘可用空间不足，已中止备份操作")


async def _stage_and_build_tar(
    state_root: Path,
    dest_tar_gz: Path,
    *,
    backup_id: str,
    kind: str,
    include_files: bool,
    include_env: bool,
    scopes: tuple[str, ...] = BASE_BACKUP_SCOPES,
    workspace_paths: tuple[str, ...] = (),
    ladybug_store=None,
    raw_config: dict | None = None,
) -> dict:
    effective_cfg = _load_effective_config_safe(state_root, raw_config)
    lbdb_rel = _configured_lbdb_rel(state_root, effective_cfg)
    required_env_vars = collect_referenced_env_vars(effective_cfg) if "config" in scopes else []
    configured_env_vars = [
        name for name in required_env_vars
        if name in read_dotenv_key_names(state_root / "config" / ".env") or bool(os.environ.get(name, "").strip())
    ]

    entries, has_lbdb = _scan_payload_sources(
        state_root,
        include_files=include_files,
        include_env=include_env,
        lbdb_rel=lbdb_rel,
        scopes=scopes,
        workspace_paths=workspace_paths,
    )
    if not entries:
        raise BackupError("当前勾选范围下没有可备份的文件，请至少勾选一项数据或工作区文件")
    est_bytes = _estimate_entries_bytes(entries)
    _check_free_space(dest_tar_gz.parent, est_bytes)

    created_ts = time.time()
    created_at = datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat()
    data_epoch = read_data_epoch()

    with tempfile.TemporaryDirectory(prefix=".tentacle-bk-", dir=str(dest_tar_gz.parent)) as tmp_dir_str:
        work = Path(tmp_dir_str)
        payload_root = work / "payload"
        payload_root.mkdir(parents=True, exist_ok=True)

        file_records: list[dict] = []
        total_payload_bytes = 0

        for rel, src, entry_kind in entries:
            dst = payload_root / rel
            if entry_kind == "ladybug":
                try:
                    await _snapshot_ladybug(src, dst, ladybug_store=ladybug_store)
                except Exception:
                    if kind != "pre_import":
                        raise
                    logger.warning("导入前保护快照无法执行 Ladybug CHECKPOINT，改为直接复制原库文件: %s", rel, exc_info=True)
                    await asyncio.to_thread(_copy_regular_file, src, dst)
            elif entry_kind == "sqlite":
                try:
                    await asyncio.to_thread(_snapshot_sqlite_sync, src, dst)
                except sqlite3.Error:
                    if kind != "pre_import":
                        raise
                    logger.warning("导入前保护快照无法执行 SQLite 在线备份，改为直接复制原库文件: %s", rel, exc_info=True)
                    await asyncio.to_thread(_copy_regular_file, src, dst)
            else:
                await asyncio.to_thread(_copy_regular_file, src, dst)
            size, digest = await asyncio.to_thread(_sha256_file, dst)
            total_payload_bytes += size
            file_records.append({
                "path": f"payload/{rel}",
                "size": size,
                "sha256": digest,
            })

        workspace_records = [rec for rec in file_records if rec["path"].startswith("payload/workspace/")]
        workspace_bytes = sum(int(rec["size"]) for rec in workspace_records)

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "backup_id": backup_id,
            "kind": kind,
            "created_at": created_at,
            "created_ts": created_ts,
            "data_epoch": data_epoch,
            "included_scopes": list(scopes),
            "includes_files": include_files,
            "includes_env": include_env,
            "includes_workspace": bool(workspace_records),
            "workspace_paths": list(workspace_paths),
            "has_ladybug": bool(has_lbdb),
            "required_env_vars": required_env_vars,
            "configured_env_vars": configured_env_vars,
            "stats": {
                "file_count": len(file_records),
                "payload_bytes": total_payload_bytes,
                "workspace_file_count": len(workspace_records),
                "workspace_bytes": workspace_bytes,
            },
            "files": file_records,
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        def write_tar():
            tmp_tar = dest_tar_gz.with_name(f".{dest_tar_gz.name}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                with tarfile.open(tmp_tar, "w:gz", compresslevel=6) as archive:
                    info = tarfile.TarInfo("manifest.json")
                    info.size = len(manifest_bytes)
                    info.mtime = int(created_ts)
                    info.mode = 0o600
                    archive.addfile(info, io.BytesIO(manifest_bytes))
                    for rec in file_records:
                        arc_name = rec["path"]
                        rel = arc_name[len("payload/"):]
                        fpath = payload_root / rel
                        ti = archive.gettarinfo(fpath, arcname=arc_name)
                        ti.mode = 0o600
                        with fpath.open("rb") as fstream:
                            archive.addfile(ti, fstream)
                _chmod_safe(tmp_tar, 0o600)
                os.replace(tmp_tar, dest_tar_gz)
            finally:
                if tmp_tar.exists():
                    tmp_tar.unlink(missing_ok=True)

        await asyncio.to_thread(write_tar)
        archive_bytes, archive_sha256 = await asyncio.to_thread(_sha256_file, dest_tar_gz)

    return {
        "backup_id": backup_id,
        "kind": kind,
        "created_at": created_at,
        "created_ts": created_ts,
        "data_epoch": data_epoch,
        "included_scopes": list(scopes),
        "includes_files": include_files,
        "includes_env": include_env,
        "includes_workspace": bool(workspace_records),
        "workspace_paths": list(workspace_paths),
        "workspace_file_count": len(workspace_records),
        "workspace_bytes": workspace_bytes,
        "file_count": len(file_records),
        "payload_bytes": total_payload_bytes,
        "archive_bytes": archive_bytes,
        "archive_sha256": archive_sha256,
        "required_env_vars": required_env_vars,
        "configured_env_vars": configured_env_vars,
    }


def _new_backup_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def _meta_from_manifest(archive_path: Path, manifest: dict) -> dict:
    archive_bytes, archive_sha256 = _sha256_file(archive_path)
    stats = manifest.get("stats") or {}
    return {
        "backup_id": manifest.get("backup_id") or archive_path.name.removesuffix(".tar.gz"),
        "kind": manifest.get("kind") or "manual",
        "created_at": manifest.get("created_at") or "",
        "created_ts": float(manifest.get("created_ts") or archive_path.stat().st_mtime),
        "data_epoch": int(manifest.get("data_epoch") or 0),
        "included_scopes": list(manifest.get("included_scopes") or BASE_BACKUP_SCOPES),
        "includes_files": bool(manifest.get("includes_files", True)),
        "includes_env": False,
        "includes_workspace": bool(manifest.get("includes_workspace", False)),
        "workspace_paths": list(manifest.get("workspace_paths") or []),
        "workspace_file_count": int(stats.get("workspace_file_count") or 0),
        "workspace_bytes": int(stats.get("workspace_bytes") or 0),
        "file_count": int(stats.get("file_count") or len(manifest.get("files") or [])),
        "payload_bytes": int(stats.get("payload_bytes") or 0),
        "archive_bytes": archive_bytes,
        "archive_sha256": archive_sha256,
        "required_env_vars": list(manifest.get("required_env_vars") or []),
        "configured_env_vars": list(manifest.get("configured_env_vars") or []),
    }


def list_backups_sync(state_root: Path) -> list[dict]:
    backup_dir = state_root / BACKUP_DIR_REL
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        return []
    items: list[dict] = []
    for arc in backup_dir.iterdir():
        if arc.is_symlink() or not arc.is_file() or not arc.name.endswith(".tar.gz"):
            continue
        bid = arc.name[:-len(".tar.gz")]
        if not BACKUP_ID_RE.fullmatch(bid):
            continue
        meta_path = backup_dir / f"{bid}.meta.json"
        meta: dict | None = None
        if meta_path.is_file() and not meta_path.is_symlink():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("backup_id") == bid:
                    meta = loaded
            except Exception:
                meta = None
        if meta is None:
            try:
                with tarfile.open(arc, "r:gz") as tar:
                    m_member = tar.getmember("manifest.json")
                    if m_member.isfile() and m_member.size <= 16 * 1024 * 1024:
                        with tar.extractfile(m_member) as stream:
                            manifest = json.loads(stream.read().decode("utf-8"))
                        meta = _meta_from_manifest(arc, manifest)
                        _atomic_write_json(meta_path, meta)
            except Exception:
                continue
        if meta is not None:
            meta["includes_env"] = False
            meta.setdefault("archive_bytes", arc.stat().st_size)
            items.append(meta)
    items.sort(key=lambda item: float(item.get("created_ts") or 0), reverse=True)
    return items


def _prune_backups_sync(state_root: Path, retention_count: int) -> list[str]:
    items = list_backups_sync(state_root)
    backup_dir = state_root / BACKUP_DIR_REL
    removed: list[str] = []

    auto_items = [item for item in items if item.get("kind") == "auto"]
    for old in auto_items[max(1, retention_count):]:
        bid = old["backup_id"]
        (backup_dir / f"{bid}.tar.gz").unlink(missing_ok=True)
        (backup_dir / f"{bid}.meta.json").unlink(missing_ok=True)
        removed.append(bid)

    pre_items = [item for item in items if item.get("kind") == "pre_import"]
    for old in pre_items[PRE_IMPORT_RETENTION:]:
        bid = old["backup_id"]
        (backup_dir / f"{bid}.tar.gz").unlink(missing_ok=True)
        (backup_dir / f"{bid}.meta.json").unlink(missing_ok=True)
        removed.append(bid)

    return removed


def _extract_verified_archive_sync(
    archive_path: Path,
    dest_staging: Path,
    *,
    state_root: Path,
    passphrase: str | None = None,
) -> dict:
    """校验（可选解密）归档并安全解包到隔离暂存目录，防止路径穿越、软链接与校验篡改。"""
    if dest_staging.exists():
        shutil.rmtree(dest_staging, ignore_errors=True)
    _mkdir_private(dest_staging)
    payload_dir = dest_staging / "payload"
    _mkdir_private(payload_dir)

    decrypted_temp: Path | None = None
    encrypted_source = False
    try:
        if is_encrypted_archive(archive_path):
            encrypted_source = True
            decrypted_temp = dest_staging / ".decrypted.tar.gz"
            decrypt_archive_file(archive_path, decrypted_temp, passphrase or "")
            tar_source = decrypted_temp
        else:
            tar_source = archive_path

        try:
            with tarfile.open(tar_source, "r:gz") as tar:
                members = tar.getmembers()
                if len(members) > MAX_IMPORT_FILES:
                    raise BackupError("备份归档内文件数量超出限制")

                manifest_member = next((m for m in members if m.name == "manifest.json"), None)
                if manifest_member is None or not manifest_member.isfile():
                    raise BackupError("备份包缺少有效 manifest.json")
                if manifest_member.size > 32 * 1024 * 1024:
                    raise BackupError("备份 manifest.json 体积超出限制")
                with tar.extractfile(manifest_member) as m_stream:
                    manifest = json.loads(m_stream.read(manifest_member.size + 1).decode("utf-8"))

                if int(manifest.get("schema_version") or 0) != BACKUP_SCHEMA_VERSION:
                    raise BackupError("不支持的备份包格式版本")
                current_epoch = read_data_epoch()
                pkg_epoch = int(manifest.get("data_epoch") or 0)
                if pkg_epoch != current_epoch:
                    raise BackupError(
                        f"备份包数据代号不兼容（包为 {pkg_epoch}，当前版本为 {current_epoch}）"
                    )

                manifest_files = {
                    rec["path"]: rec
                    for rec in (manifest.get("files") or [])
                    if isinstance(rec, dict) and isinstance(rec.get("path"), str)
                }
                includes_env = bool(manifest.get("includes_env"))
                if includes_env and not encrypted_source:
                    raise BackupError("含 .env 的迁移包必须是加密归档")

                seen_files: set[str] = set()
                total_uncompressed = 0

                for member in members:
                    if member.name == "manifest.json":
                        continue
                    safe_arc = _safe_rel_posix(member.name)
                    if not safe_arc:
                        raise BackupError(f"归档包含非法路径：{member.name}")
                    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                        raise BackupError(f"归档包含不受支持的文件类型：{member.name}")
                    if member.isdir():
                        if safe_arc == "payload":
                            continue
                        if safe_arc.startswith("payload/"):
                            rel_dir = safe_arc[len("payload/"):]
                            top_dir = PurePosixPath(rel_dir).parts[0]
                            if not _is_secret_rel(rel_dir) and top_dir in {"config", *MANAGED_CONTENT_DIRS, "data", "workspace"}:
                                continue
                        raise BackupError(f"归档包含受限目录：{member.name}")
                    if not safe_arc.startswith("payload/"):
                        raise BackupError(f"归档包含非法路径：{member.name}")
                    rel = safe_arc[len("payload/"):]
                    if not _is_allowed_payload_rel(
                        rel,
                        include_files=True,
                        allow_env=includes_env,
                        allow_workspace=True,
                        scopes=BASE_BACKUP_SCOPES,
                    ):
                        raise BackupError(f"归档包含受限或不允许导入的路径：{rel}")
                    if safe_arc in seen_files:
                        raise BackupError(f"归档包含重复文件路径：{rel}")
                    expected = manifest_files.get(safe_arc)
                    if expected is None:
                        raise BackupError(f"文件未在 manifest 清单中登记：{rel}")
                    seen_files.add(safe_arc)

                    target = payload_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    h = hashlib.sha256()
                    actual_size = 0
                    with tar.extractfile(member) as src_stream, target.open("wb") as out_stream:
                        if src_stream is None:
                            raise BackupError(f"无法读取归档文件：{rel}")
                        while chunk := src_stream.read(256 * 1024):
                            actual_size += len(chunk)
                            total_uncompressed += len(chunk)
                            if total_uncompressed > MAX_IMPORT_UNCOMPRESSED_BYTES:
                                raise BackupError("归档解压后总大小超出 2 GiB 限制")
                            h.update(chunk)
                            out_stream.write(chunk)
                    _chmod_safe(target, 0o600)
                    if actual_size != int(expected.get("size", -1)) or h.hexdigest() != expected.get("sha256"):
                        raise BackupError(f"文件校验和不一致，归档可能损坏：{rel}")

                if seen_files != set(manifest_files):
                    missing = sorted(set(manifest_files) - seen_files)
                    raise BackupError(f"归档缺少清单登记的文件：{missing[0]}")
        except tarfile.TarError as exc:
            raise BackupError("归档不是有效的 tar.gz 文件或文件已损坏") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("归档 manifest.json 解析失败") from exc

        cfg_file = payload_dir / "config" / "config.yaml"
        if cfg_file.is_file():
            _validate_config_yaml_bytes(cfg_file.read_bytes())

        env_file = payload_dir / "config" / ".env"
        env_key_names: list[str] = []
        if env_file.is_file():
            _validate_dotenv_bytes(env_file.read_bytes())
            env_key_names = sorted(read_dotenv_key_names(env_file))

        # 计算各目录文件统计与缺失环境变量（只检查键名）
        files_dir = payload_dir / "data" / "files"
        files_count = 0
        files_bytes = 0
        if files_dir.is_dir():
            for f in files_dir.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    files_count += 1
                    files_bytes += f.stat().st_size

        ws_dir = payload_dir / "workspace"
        workspace_files_count = 0
        workspace_bytes = 0
        workspace_sample_paths: list[str] = []
        if ws_dir.is_dir():
            for f in sorted(ws_dir.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    workspace_files_count += 1
                    workspace_bytes += f.stat().st_size
                    if len(workspace_sample_paths) < 20:
                        workspace_sample_paths.append(f.relative_to(ws_dir).as_posix())

        raw_scopes = manifest.get("included_scopes")
        included_scopes = list(normalize_backup_scopes(raw_scopes)) if raw_scopes is not None else list(BASE_BACKUP_SCOPES)

        required_env_vars = list(manifest.get("required_env_vars") or [])
        missing_if_no_env = compute_missing_env_vars(state_root, required_env_vars)
        missing_if_apply_env = [
            v for v in required_env_vars
            if v not in set(env_key_names) and not os.environ.get(v, "").strip()
        ] if env_file.is_file() else missing_if_no_env

        modules = sorted({
            PurePosixPath(rec["path"][len("payload/"):]).parts[0]
            for rec in (manifest.get("files") or [])
            if isinstance(rec, dict) and isinstance(rec.get("path"), str)
        })

        return {
            "manifest": manifest,
            "encrypted": encrypted_source,
            "included_scopes": included_scopes,
            "has_env": env_file.is_file(),
            "env_key_count": len(env_key_names),
            "env_key_names": env_key_names,
            "has_files": files_count > 0,
            "files_count": files_count,
            "files_bytes": files_bytes,
            "has_workspace": workspace_files_count > 0,
            "workspace_files_count": workspace_files_count,
            "workspace_bytes": workspace_bytes,
            "workspace_sample_paths": workspace_sample_paths,
            "modules": modules,
            "required_env_vars": required_env_vars,
            "missing_if_keep_env": missing_if_no_env,
            "missing_if_apply_env": missing_if_apply_env,
        }
    finally:
        if decrypted_temp is not None:
            decrypted_temp.unlink(missing_ok=True)


def _cleanup_stale_staging_sync(state_root: Path, *, keep_token: str | None = None) -> None:
    staging_root = state_root / STAGING_DIR_REL
    if not staging_root.is_dir() or staging_root.is_symlink():
        return
    pending_token = None
    pending_path = state_root / PENDING_APPLY_REL
    if pending_path.is_file():
        try:
            pending_token = json.loads(pending_path.read_text(encoding="utf-8")).get("import_token")
        except Exception:
            pending_token = None
    now = time.time()
    for child in staging_root.iterdir():
        if child.name == "pending_apply.json":
            continue
        if child.name in {keep_token, pending_token}:
            continue
        try:
            age = now - child.stat().st_mtime
            if age <= STAGING_TTL_SECONDS:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            elif child.is_file():
                child.unlink(missing_ok=True)
        except OSError:
            pass


def _is_preserved_data_rel(rel_posix: str, *, apply_files: bool) -> bool:
    parts = PurePosixPath(rel_posix).parts
    if len(parts) < 2 or parts[0] != "data":
        return False
    sub = parts[1]
    if sub in DATA_EXCLUDED_SUBDIRS:
        return True
    if len(parts) == 2 and sub in DATA_EXCLUDED_FILES:
        return True
    if sub == "files" and not apply_files:
        return True
    return False


def _move_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    _chmod_safe(dst, 0o600)


def apply_pending_restore(state_root: Path) -> dict | None:
    """在应用启动打开数据库前原子切换已确认的恢复暂存内容；异常时自动回滚。"""
    pending_path = state_root / PENDING_APPLY_REL
    if not pending_path.is_file() or pending_path.is_symlink():
        return None

    rollback_dir: Path | None = None
    consumed_dir: Path | None = None
    result: dict
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        token = str(pending.get("import_token") or "")
        if not TOKEN_RE.fullmatch(token):
            raise BackupError("待恢复标识无效")
        apply_files = bool(pending.get("apply_files", True))
        apply_env = bool(pending.get("apply_env", False))
        apply_workspace = bool(pending.get("apply_workspace", True))

        token_dir = state_root / STAGING_DIR_REL / token
        consumed_dir = token_dir
        staging_meta_path = token_dir / "staging.json"
        payload_dir = token_dir / "payload"
        if not staging_meta_path.is_file() or not payload_dir.is_dir():
            raise BackupError("找不到已校验的待恢复数据目录")

        meta = json.loads(staging_meta_path.read_text(encoding="utf-8"))
        manifest = meta.get("manifest") or {}
        if int(manifest.get("data_epoch") or 0) != read_data_epoch():
            raise BackupError("恢复中止：备份数据代号与当前版本不兼容")
        raw_scopes = manifest.get("included_scopes")
        included_scopes = normalize_backup_scopes(raw_scopes) if raw_scopes is not None else BASE_BACKUP_SCOPES

        # 重验暂存 payload 完整性（防暂存后被修改）
        for rec in (manifest.get("files") or []):
            arc_path = rec.get("path") or ""
            if not arc_path.startswith("payload/"):
                raise BackupError("暂存清单路径无效")
            rel = arc_path[len("payload/"):]
            fpath = payload_dir / rel
            if fpath.is_symlink() or not fpath.is_file():
                raise BackupError(f"待恢复文件缺失：{rel}")
            size, digest = _sha256_file(fpath)
            if size != int(rec.get("size", -1)) or digest != rec.get("sha256"):
                raise BackupError(f"待恢复文件校验失败：{rel}")

        cfg_staged = payload_dir / "config" / "config.yaml"
        if cfg_staged.is_file():
            _validate_config_yaml_bytes(cfg_staged.read_bytes())
        env_staged = payload_dir / "config" / ".env"
        if apply_env and env_staged.is_file():
            _validate_dotenv_bytes(env_staged.read_bytes())

        rollback_dir = Path(tempfile.mkdtemp(prefix=".restore-rollback-", dir=str(state_root / STAGING_DIR_REL)))
        replaced_targets: list[Path] = []
        backed_up_rel: list[str] = []

        def snapshot_existing(rel: str) -> None:
            cur = state_root / rel
            if cur.exists() or cur.is_symlink():
                rb = rollback_dir / rel
                rb.parent.mkdir(parents=True, exist_ok=True)
                if cur.is_file() and not cur.is_symlink():
                    shutil.copy2(cur, rb)
                    backed_up_rel.append(rel)

        try:
            # 1. config/config.yaml & 可选 config/.env
            if "config" in included_scopes:
                snapshot_existing("config/config.yaml")
                cfg_live = state_root / "config" / "config.yaml"
                if cfg_staged.is_file():
                    _mkdir_private(cfg_live.parent)
                    _atomic_write_bytes(cfg_live, cfg_staged.read_bytes(), mode=0o600)
                    replaced_targets.append(cfg_live)
                elif cfg_live.exists() and not cfg_live.is_symlink():
                    cfg_live.unlink()

            if apply_env and env_staged.is_file():
                snapshot_existing("config/.env")
                env_live = state_root / "config" / ".env"
                _mkdir_private(env_live.parent)
                _atomic_write_bytes(env_live, env_staged.read_bytes(), mode=0o600)
                replaced_targets.append(env_live)

            # 2. prompt / skills / plugins 目录替换（仅替换包中声明包含的模块）
            for top in MANAGED_CONTENT_DIRS:
                if top not in included_scopes:
                    continue
                live_dir = state_root / top
                if live_dir.is_dir() and not live_dir.is_symlink():
                    for root_str, _dirs, files in os.walk(live_dir, topdown=False, followlinks=False):
                        rp = Path(root_str)
                        for fname in files:
                            f = rp / fname
                            if not f.is_symlink() and f.is_file():
                                snapshot_existing(f.relative_to(state_root).as_posix())
                            f.unlink(missing_ok=True)
                        if rp != live_dir:
                            try:
                                if not any(rp.iterdir()):
                                    rp.rmdir()
                            except OSError:
                                pass
                else:
                    _mkdir_private(live_dir)
                staged_sub = payload_dir / top
                if staged_sub.is_dir():
                    for f in staged_sub.rglob("*"):
                        if f.is_file() and not f.is_symlink():
                            rel = f.relative_to(payload_dir).as_posix()
                            if not _is_allowed_payload_rel(rel, include_files=apply_files, allow_env=False, scopes=included_scopes):
                                continue
                            target = state_root / rel
                            _move_or_copy(f, target)
                            replaced_targets.append(target)

            # 3. data/ 受管文件替换（仅在包含 data 模块时替换数据库；仅包含 files 时只更新 data/files/）
            data_live = state_root / "data"
            _mkdir_private(data_live)
            if "data" in included_scopes:
                for root_str, dirnames, filenames in os.walk(data_live, topdown=False, followlinks=False):
                    rp = Path(root_str)
                    for fname in filenames:
                        f = rp / fname
                        rel = f.relative_to(state_root).as_posix()
                        if _is_preserved_data_rel(rel, apply_files=apply_files):
                            continue
                        if not f.is_symlink():
                            snapshot_existing(rel)
                        f.unlink(missing_ok=True)
                    for dname in dirnames:
                        sub = rp / dname
                        rel_d = sub.relative_to(state_root).as_posix()
                        if sub.is_symlink():
                            continue
                        if rel_d.startswith("data/") and PurePosixPath(rel_d).parts[1] in DATA_EXCLUDED_SUBDIRS:
                            continue
                        if rel_d == "data/files" and not apply_files:
                            continue
                        try:
                            if sub.is_dir() and not any(sub.iterdir()):
                                sub.rmdir()
                        except OSError:
                            pass

                data_staged = payload_dir / "data"
                if data_staged.is_dir():
                    for f in data_staged.rglob("*"):
                        if f.is_file() and not f.is_symlink():
                            rel = f.relative_to(payload_dir).as_posix()
                            if not _is_allowed_payload_rel(rel, include_files=apply_files, allow_env=False, scopes=included_scopes):
                                continue
                            if _is_preserved_data_rel(rel, apply_files=apply_files):
                                continue
                            target = state_root / rel
                            _move_or_copy(f, target)
                            replaced_targets.append(target)
            elif apply_files and (payload_dir / "data" / "files").is_dir():
                files_live = data_live / "files"
                if files_live.is_dir() and not files_live.is_symlink():
                    for f in files_live.rglob("*"):
                        if f.is_file() and not f.is_symlink():
                            snapshot_existing(f.relative_to(state_root).as_posix())
                            f.unlink(missing_ok=True)
                for f in (payload_dir / "data" / "files").rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        rel = f.relative_to(payload_dir).as_posix()
                        if not _is_allowed_payload_rel(rel, include_files=True, allow_env=False, scopes=included_scopes):
                            continue
                        target = state_root / rel
                        _move_or_copy(f, target)
                        replaced_targets.append(target)

            # 4. workspace/ 精选产出文件（仅按归档路径合并写入，不删除目标工作区无关文件）
            applied_workspace_count = 0
            ws_staged = payload_dir / "workspace"
            if apply_workspace and ws_staged.is_dir():
                _mkdir_private(state_root / "workspace")
                for f in ws_staged.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        rel = f.relative_to(payload_dir).as_posix()
                        if not _is_allowed_payload_rel(rel, include_files=False, allow_env=False, allow_workspace=True, scopes=included_scopes):
                            continue
                        snapshot_existing(rel)
                        target = state_root / rel
                        _move_or_copy(f, target)
                        replaced_targets.append(target)
                        applied_workspace_count += 1
        except BaseException:
            # 回滚：先清除新写入文件再放回原有备份文件
            for created in replaced_targets:
                if created.exists() and not created.is_symlink():
                    created.unlink(missing_ok=True)
            for rel in backed_up_rel:
                src_rb = rollback_dir / rel
                dst_live = state_root / rel
                if src_rb.is_file():
                    _move_or_copy(src_rb, dst_live)
            raise

        effective_after = _load_effective_config_safe(state_root)
        required_after = collect_referenced_env_vars(effective_after)
        missing_after = compute_missing_env_vars(state_root, required_after)

        result = {
            "status": "succeeded",
            "applied_at": time.time(),
            "backup_id": manifest.get("backup_id") or pending.get("backup_id") or "",
            "pre_backup_id": pending.get("pre_backup_id") or "",
            "applied_scopes": list(included_scopes),
            "applied_env": bool(apply_env and env_staged.is_file()),
            "applied_files": bool(apply_files and (payload_dir / "data" / "files").is_dir()),
            "applied_workspace": bool(applied_workspace_count > 0),
            "missing_env_vars": missing_after,
            "error": None,
        }
    except Exception as exc:
        logger.error("启动期恢复备份失败，已保留原数据: %s", type(exc).__name__, exc_info=True)
        result = {
            "status": "failed",
            "applied_at": time.time(),
            "backup_id": "",
            "pre_backup_id": "",
            "applied_scopes": [],
            "applied_env": False,
            "applied_files": False,
            "applied_workspace": False,
            "missing_env_vars": [],
            "error": str(exc) if isinstance(exc, BackupError) else "恢复备份时发生内部错误，已回滚原数据",
        }
    finally:
        pending_path.unlink(missing_ok=True)
        if rollback_dir is not None and rollback_dir.exists():
            shutil.rmtree(rollback_dir, ignore_errors=True)
        if consumed_dir is not None and consumed_dir.exists():
            shutil.rmtree(consumed_dir, ignore_errors=True)

    _atomic_write_json(state_root / LAST_RESTORE_REL, result, mode=0o600)
    return result


class HomeBackupManager:
    """管理 TENTACLE_HOME 定期备份、手动快照、加密导出与两步导入恢复。"""

    def __init__(
        self,
        state_root: Path,
        *,
        ladybug_store=None,
        config_manager=None,
        poll_seconds: float = SCHEDULER_POLL_SECONDS,
    ):
        self.state_root = Path(state_root).resolve()
        self.ladybug_store = ladybug_store
        self.config_manager = config_manager
        self.poll_seconds = poll_seconds
        self._lock = asyncio.Lock()
        self._running_op: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def policy_path(self) -> Path:
        return self.state_root / POLICY_REL

    @property
    def backup_dir(self) -> Path:
        return self.state_root / BACKUP_DIR_REL

    @property
    def staging_dir(self) -> Path:
        return self.state_root / STAGING_DIR_REL

    @property
    def last_restore_path(self) -> Path:
        return self.state_root / LAST_RESTORE_REL

    def read_policy(self) -> dict:
        try:
            raw = json.loads(self.policy_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("invalid policy")
        except (OSError, ValueError):
            raw = {}
        merged = {**DEFAULT_POLICY, **raw}
        merged["enabled"] = bool(merged["enabled"])
        try:
            hours = int(merged["interval_hours"])
        except (TypeError, ValueError):
            hours = DEFAULT_INTERVAL_HOURS
        merged["interval_hours"] = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, hours))
        try:
            ret = int(merged["retention_count"])
        except (TypeError, ValueError):
            ret = DEFAULT_RETENTION_COUNT
        merged["retention_count"] = max(MIN_RETENTION_COUNT, min(MAX_RETENTION_COUNT, ret))
        merged["include_files"] = bool(merged["include_files"])
        return merged

    def _save_policy(self, policy: dict) -> None:
        _atomic_write_json(self.policy_path, policy, mode=0o600)

    def update_policy(
        self,
        *,
        enabled: bool | None = None,
        interval_hours: int | None = None,
        retention_count: int | None = None,
        include_files: bool | None = None,
    ) -> dict:
        policy = self.read_policy()
        if enabled is not None:
            policy["enabled"] = bool(enabled)
        if interval_hours is not None:
            if type(interval_hours) is not int or not (MIN_INTERVAL_HOURS <= interval_hours <= MAX_INTERVAL_HOURS):
                raise BackupError(f"备份间隔须在 {MIN_INTERVAL_HOURS} 至 {MAX_INTERVAL_HOURS} 小时之间")
            policy["interval_hours"] = interval_hours
        if retention_count is not None:
            if type(retention_count) is not int or not (MIN_RETENTION_COUNT <= retention_count <= MAX_RETENTION_COUNT):
                raise BackupError(f"保留份数须在 {MIN_RETENTION_COUNT} 至 {MAX_RETENTION_COUNT} 份之间")
            policy["retention_count"] = retention_count
        if include_files is not None:
            policy["include_files"] = bool(include_files)
        self._save_policy(policy)
        return policy

    def read_last_restore(self) -> dict | None:
        if self.last_restore_path.is_symlink() or not self.last_restore_path.is_file():
            return None
        try:
            data = json.loads(self.last_restore_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def ack_last_restore(self) -> None:
        self.last_restore_path.unlink(missing_ok=True)

    def next_run_at(self, policy: dict | None = None) -> float | None:
        p = policy or self.read_policy()
        if not p["enabled"]:
            return None
        last = p.get("last_auto_backup_at")
        if last is None:
            return time.time() + 60.0
        return float(last) + float(p["interval_hours"]) * 3600.0

    def status_snapshot(self) -> dict:
        policy = self.read_policy()
        backups = list_backups_sync(self.state_root)
        total_bytes = sum(int(item.get("archive_bytes") or 0) for item in backups)
        raw_cfg = self.config_manager.snapshot() if self.config_manager is not None else None
        effective = _load_effective_config_safe(self.state_root, raw_cfg)
        required_env = collect_referenced_env_vars(effective)
        missing_env = compute_missing_env_vars(self.state_root, required_env)
        return {
            "policy": policy,
            "next_run_at": self.next_run_at(policy),
            "running": self._lock.locked(),
            "running_op": self._running_op,
            "backups": backups,
            "total_bytes": total_bytes,
            "data_epoch": read_data_epoch(),
            "required_env_vars": required_env,
            "missing_env_vars": missing_env,
            "last_restore": self.read_last_restore(),
        }

    def list_workspace(self, rel_path: str = "") -> dict:
        return list_workspace_tree(self.state_root, rel_path)

    async def create_backup(
        self,
        *,
        kind: str = "manual",
        include_files: bool | None = None,
        scopes: list[str] | tuple[str, ...] | None = None,
        workspace_paths: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        if kind not in {"auto", "manual", "pre_import"}:
            raise BackupError("不支持的备份类型")
        norm_scopes = normalize_backup_scopes(scopes) if kind == "manual" or scopes is not None else BASE_BACKUP_SCOPES
        norm_workspace = (
            normalize_workspace_paths(self.state_root, workspace_paths)
            if workspace_paths and kind in {"manual", "pre_import"}
            else ()
        )
        if self._lock.locked():
            raise BackupConflictError("已有备份或恢复任务正在进行")
        async with self._lock:
            self._running_op = f"backup:{kind}"
            try:
                policy = self.read_policy()
                inc_files = policy["include_files"] if include_files is None else bool(include_files)
                _mkdir_private(self.backup_dir)
                bid = _new_backup_id()
                arc_path = self.backup_dir / f"{bid}.tar.gz"
                meta_path = self.backup_dir / f"{bid}.meta.json"
                raw_cfg = self.config_manager.snapshot() if self.config_manager is not None else None
                meta = await _stage_and_build_tar(
                    self.state_root,
                    arc_path,
                    backup_id=bid,
                    kind=kind,
                    include_files=inc_files,
                    include_env=False,
                    scopes=norm_scopes,
                    workspace_paths=norm_workspace,
                    ladybug_store=self.ladybug_store,
                    raw_config=raw_cfg,
                )
                await asyncio.to_thread(_atomic_write_json, meta_path, meta)
                await asyncio.to_thread(_prune_backups_sync, self.state_root, policy["retention_count"])
                return meta
            finally:
                self._running_op = None

    def get_backup_archive_path(self, backup_id: str) -> Path:
        if not BACKUP_ID_RE.fullmatch(str(backup_id or "")):
            raise BackupError("备份 ID 格式无效")
        arc = self.backup_dir / f"{backup_id}.tar.gz"
        if arc.is_symlink() or not arc.is_file():
            raise FileNotFoundError("指定的备份文件不存在")
        return arc

    async def delete_backup(self, backup_id: str) -> None:
        if not BACKUP_ID_RE.fullmatch(str(backup_id or "")):
            raise BackupError("备份 ID 格式无效")
        if self._lock.locked():
            raise BackupConflictError("已有备份或恢复任务正在进行")
        async with self._lock:
            arc = self.backup_dir / f"{backup_id}.tar.gz"
            meta = self.backup_dir / f"{backup_id}.meta.json"
            if not arc.exists() and not meta.exists():
                raise FileNotFoundError("指定的备份不存在")
            arc.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)

    async def create_ephemeral_export(
        self,
        *,
        include_files: bool = True,
        include_env: bool = False,
        passphrase: str | None = None,
        scopes: list[str] | tuple[str, ...] | None = None,
        workspace_paths: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[tempfile.TemporaryDirectory, Path, str]:
        """生成一次性流式迁移包（绝不保存至 data/backups/）。调用方负责在响应结束后清理 TemporaryDirectory。"""
        if include_env and (not passphrase or len(passphrase) < MIN_PASSPHRASE_LENGTH):
            raise BackupError(f"包含 .env 凭据时必须设置至少 {MIN_PASSPHRASE_LENGTH} 位加密口令")
        if passphrase and len(passphrase) < MIN_PASSPHRASE_LENGTH:
            raise BackupError(f"加密口令至少需要 {MIN_PASSPHRASE_LENGTH} 个字符")
        norm_scopes = normalize_backup_scopes(scopes)
        norm_workspace = normalize_workspace_paths(self.state_root, workspace_paths)
        if self._lock.locked():
            raise BackupConflictError("已有备份或恢复任务正在进行")

        async with self._lock:
            self._running_op = "export"
            _mkdir_private(self.staging_dir)
            tmp = tempfile.TemporaryDirectory(prefix=".export-", dir=str(self.staging_dir))
            try:
                tmp_dir = Path(tmp.name)
                bid = _new_backup_id()
                tar_path = tmp_dir / f"{bid}.tar.gz"
                raw_cfg = self.config_manager.snapshot() if self.config_manager is not None else None
                await _stage_and_build_tar(
                    self.state_root,
                    tar_path,
                    backup_id=bid,
                    kind="export",
                    include_files=include_files,
                    include_env=include_env,
                    scopes=norm_scopes,
                    workspace_paths=norm_workspace,
                    ladybug_store=self.ladybug_store,
                    raw_config=raw_cfg,
                )
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
                if passphrase:
                    enc_path = tmp_dir / f"tentacle-migration-{stamp}.tbackup"
                    await asyncio.to_thread(encrypt_archive_file, tar_path, enc_path, passphrase)
                    tar_path.unlink(missing_ok=True)
                    return tmp, enc_path, enc_path.name
                out_name = f"tentacle-migration-{stamp}.tar.gz"
                named_path = tmp_dir / out_name
                os.replace(tar_path, named_path)
                return tmp, named_path, out_name
            except BaseException:
                tmp.cleanup()
                raise
            finally:
                self._running_op = None

    async def inspect_import(
        self,
        *,
        archive_path: Path | None = None,
        backup_id: str | None = None,
        passphrase: str | None = None,
    ) -> dict:
        if self._lock.locked():
            raise BackupConflictError("已有备份或恢复任务正在进行")
        async with self._lock:
            self._running_op = "inspect_import"
            try:
                if backup_id:
                    source_path = self.get_backup_archive_path(backup_id)
                    source_type = "local"
                elif archive_path is not None and archive_path.is_file():
                    source_path = archive_path
                    source_type = "upload"
                else:
                    raise BackupError("请上传备份归档或选择本机历史备份")

                token = uuid.uuid4().hex
                _mkdir_private(self.staging_dir)
                await asyncio.to_thread(_cleanup_stale_staging_sync, self.state_root, keep_token=token)
                dest = self.staging_dir / token
                try:
                    info = await asyncio.to_thread(
                        _extract_verified_archive_sync,
                        source_path,
                        dest,
                        state_root=self.state_root,
                        passphrase=passphrase,
                    )
                except BaseException:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise

                manifest = info["manifest"]
                staging_record = {
                    "import_token": token,
                    "source_type": source_type,
                    "created_at": time.time(),
                    "manifest": manifest,
                    "included_scopes": info["included_scopes"],
                    "has_env": info["has_env"],
                    "env_key_count": info["env_key_count"],
                    "has_files": info["has_files"],
                    "has_workspace": info["has_workspace"],
                    "workspace_sample_paths": info["workspace_sample_paths"],
                }
                await asyncio.to_thread(_atomic_write_json, dest / "staging.json", staging_record, mode=0o600)
                stats = manifest.get("stats") or {}
                return {
                    "import_token": token,
                    "source_type": source_type,
                    "backup_id": manifest.get("backup_id") or backup_id or "",
                    "backup_created_at": manifest.get("created_at") or "",
                    "data_epoch": int(manifest.get("data_epoch") or 0),
                    "encrypted": info["encrypted"],
                    "included_scopes": info["included_scopes"],
                    "includes_files": bool(manifest.get("includes_files", False)),
                    "includes_env": info["has_env"],
                    "env_key_count": info["env_key_count"],
                    "has_files": info["has_files"],
                    "files_count": info["files_count"],
                    "files_bytes": info["files_bytes"],
                    "has_workspace": info["has_workspace"],
                    "workspace_files_count": info["workspace_files_count"],
                    "workspace_bytes": info["workspace_bytes"],
                    "workspace_sample_paths": info["workspace_sample_paths"],
                    "file_count": int(stats.get("file_count") or len(manifest.get("files") or [])),
                    "payload_bytes": int(stats.get("payload_bytes") or 0),
                    "modules": info["modules"],
                    "required_env_vars": info["required_env_vars"],
                    "missing_if_keep_env": info["missing_if_keep_env"],
                    "missing_if_apply_env": info["missing_if_apply_env"],
                }
            finally:
                self._running_op = None

    async def prepare_apply_import(
        self,
        import_token: str,
        *,
        apply_files: bool = True,
        apply_env: bool = False,
        apply_workspace: bool = True,
        create_pre_backup: bool = True,
    ) -> dict:
        if not TOKEN_RE.fullmatch(str(import_token or "")):
            raise BackupError("导入预览凭证格式无效")
        staging_json = self.staging_dir / import_token / "staging.json"
        if not staging_json.is_file():
            raise BackupError("导入预览已过期或不存在，请重新校验备份文件")

        staging = json.loads(staging_json.read_text(encoding="utf-8"))
        manifest = staging.get("manifest") or {}
        if int(manifest.get("data_epoch") or 0) != read_data_epoch():
            raise BackupError("备份包数据代号与当前版本不兼容")

        pre_backup_id = ""
        if create_pre_backup:
            staging_scopes = staging.get("included_scopes") or list(BASE_BACKUP_SCOPES)
            pre_ws: list[str] = []
            if apply_workspace and staging.get("has_workspace"):
                ws_root = self.state_root / "workspace"
                ws_staged = self.staging_dir / import_token / "payload" / "workspace"
                if ws_staged.is_dir() and ws_root.is_dir():
                    for f in ws_staged.rglob("*"):
                        if f.is_file() and not f.is_symlink():
                            wrel = f.relative_to(ws_staged).as_posix()
                            if (ws_root / wrel).is_file() and not (ws_root / wrel).is_symlink():
                                pre_ws.append(wrel)
            pre_files = bool(apply_files and staging.get("has_files"))
            if staging_scopes or pre_files or pre_ws:
                pre_meta = await self.create_backup(
                    kind="pre_import",
                    include_files=pre_files,
                    scopes=staging_scopes,
                    workspace_paths=pre_ws[:MAX_WORKSPACE_SELECTION] if pre_ws else None,
                )
                pre_backup_id = pre_meta["backup_id"]

        if self._lock.locked():
            raise BackupConflictError("已有备份或恢复任务正在进行")
        async with self._lock:
            self._running_op = "prepare_apply"
            try:
                pending = {
                    "import_token": import_token,
                    "backup_id": manifest.get("backup_id") or "",
                    "pre_backup_id": pre_backup_id,
                    "apply_files": bool(apply_files),
                    "apply_env": bool(apply_env and staging.get("has_env")),
                    "apply_workspace": bool(apply_workspace and staging.get("has_workspace")),
                    "requested_at": time.time(),
                }
                await asyncio.to_thread(_atomic_write_json, self.state_root / PENDING_APPLY_REL, pending, mode=0o600)
                return pending
            finally:
                self._running_op = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await asyncio.to_thread(_cleanup_stale_staging_sync, self.state_root)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._scheduler_loop(), name="home-backup-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run_auto_tick(self) -> None:
        policy = self.read_policy()
        if not policy["enabled"]:
            return
        now = time.time()
        last = policy.get("last_auto_backup_at")
        interval_sec = float(policy["interval_hours"]) * 3600.0
        if last is not None and now < float(last) + interval_sec:
            return
        try:
            meta = await self.create_backup(kind="auto", include_files=policy["include_files"])
            updated = self.read_policy()
            updated.update(
                last_auto_backup_at=time.time(),
                last_status="succeeded",
                last_error=None,
                last_backup_id=meta["backup_id"],
            )
            self._save_policy(updated)
        except BackupConflictError:
            return
        except Exception as exc:
            logger.warning("定期自动备份失败: %s", exc, exc_info=True)
            updated = self.read_policy()
            updated.update(
                last_auto_backup_at=time.time(),
                last_status="failed",
                last_error=str(exc) if isinstance(exc, BackupError) else "自动备份失败，请检查磁盘与日志",
            )
            self._save_policy(updated)

    async def _scheduler_loop(self) -> None:
        # 避免刚启动阶段抢占初始化 IO
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=min(60.0, self.poll_seconds))
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            await self._run_auto_tick()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_seconds)
                return
            except asyncio.TimeoutError:
                pass
