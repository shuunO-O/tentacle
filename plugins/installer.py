"""插件安装/暂存服务：主 Agent 只能以 pending（enabled: false）状态暂存，不可越权启用。"""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from config.store import ConfigStore, ConfigurationError
from plugins.manifest import PLUGIN_ID_RE, RESERVED_PLUGIN_IDS, parse_manifest
from plugins.mcp_client import parse_mcp_config, resolve_mcp_secrets
from plugins.permissions import PluginManifestError, describe_permissions
from runtime.paths import state_path


def _safe_copy_directory(source: Path, dest: Path) -> None:
    """安全复制插件目录到暂存目录，拒绝符号链接与隐藏敏感文件（.env/.git）。"""
    if source.is_symlink() or not source.is_dir():
        raise PluginManifestError(f"插件源路径必须为真实目录: {source}")
    root_resolved = source.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for current_dir, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in {".git", "__pycache__", ".venv"} and not (Path(current_dir) / d).is_symlink()
        )
        cur = Path(current_dir)
        rel_dir = cur.relative_to(source)
        target_dir = (dest / rel_dir).resolve()
        if not target_dir.is_relative_to(dest.resolve()):
            raise PluginManifestError("插件目录包含非法越界路径")
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in sorted(filenames):
            if fname == ".env" or fname.startswith(".env."):
                continue
            src_file = cur / fname
            if src_file.is_symlink() or not src_file.is_file():
                continue
            if not src_file.resolve().is_relative_to(root_resolved):
                raise PluginManifestError(f"插件文件越界: {fname}")
            shutil.copy2(src_file, target_dir / fname)


def _write_inline_files(files: dict[str, str], dest: Path) -> None:
    dest_resolved = dest.resolve()
    for rel, content in files.items():
        if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
            raise PluginManifestError(f"无效文件名: {rel!r}")
        normalized = rel.strip().replace("\\", "/")
        if normalized.startswith("/") or normalized in {".", ".."} or normalized.split("/")[-1].startswith(".env"):
            raise PluginManifestError(f"不允许写入的文件路径: {rel!r}")
        target = (dest / normalized).resolve()
        if not target.is_relative_to(dest_resolved) or target == dest_resolved:
            raise PluginManifestError(f"文件路径越界: {rel!r}")
        if not isinstance(content, str):
            raise PluginManifestError(f"文件内容必须为字符串: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def stage_local_plugin(
    *,
    plugin_id: str,
    files: dict[str, str] | None = None,
    source_dir: Path | None = None,
    overwrite: bool = False,
    plugins_dir: Path | None = None,
    config_store: ConfigStore | None = None,
) -> dict[str, Any]:
    """校验并暂存本地插件（强行写入 enabled=false, trusted=false 等待 WebUI 审核启用）。"""
    if not isinstance(plugin_id, str) or not PLUGIN_ID_RE.fullmatch(plugin_id) or plugin_id in RESERVED_PLUGIN_IDS:
        raise PluginManifestError("plugin_id 须以小写字母开头，仅含小写字母/数字/_/-，最长 48 字符，且不可为保留名称")
    if bool(files) == bool(source_dir):
        raise PluginManifestError("必须且只能提供 files 或 source_dir 其中之一")

    root = plugins_dir or state_path("plugins")
    root.mkdir(parents=True, exist_ok=True)
    target_dir = (root / plugin_id).resolve()
    if not target_dir.is_relative_to(root.resolve()) or target_dir == root.resolve():
        raise PluginManifestError("目标插件路径越界")
    if target_dir.exists() and not overwrite:
        raise ValueError(f"插件 {plugin_id} 已存在；如需覆盖请传入 overwrite=true")

    with tempfile.TemporaryDirectory(prefix=f".stage-{plugin_id}-", dir=root) as tmp:
        stage_dir = Path(tmp) / plugin_id
        stage_dir.mkdir(parents=True, exist_ok=True)
        if source_dir is not None:
            _safe_copy_directory(source_dir, stage_dir)
        else:
            assert files is not None
            _write_inline_files(files, stage_dir)

        manifest = parse_manifest(stage_dir)
        if manifest.id != plugin_id:
            raise PluginManifestError(f"manifest.id ({manifest.id}) 与目标 ID ({plugin_id}) 不一致")

        backup_old = None
        if target_dir.exists():
            backup_old = Path(tmp) / f"old-{plugin_id}"
            shutil.move(str(target_dir), str(backup_old))
        try:
            shutil.move(str(stage_dir), str(target_dir))
        except Exception:
            if backup_old is not None and backup_old.exists() and not target_dir.exists():
                shutil.move(str(backup_old), str(target_dir))
            raise

    store = config_store or ConfigStore()
    _, _, raw = store.read()
    existing = ((raw.get("plugins") or {}).get("items") or {}).get(plugin_id)
    entry = {
        "enabled": False,
        "trusted": False,
        "disabled_tools": list(existing.get("disabled_tools") or []) if isinstance(existing, dict) else [],
        "config": deepcopy(existing.get("config") or {}) if isinstance(existing, dict) else {},
    }
    store.upsert_plugin_entry("items", plugin_id, entry)

    env_vars = list(manifest.requires_env)
    for k, spec in manifest.config_schema.items():
        if spec.type == "env":
            val = entry["config"].get(k) or spec.default
            if isinstance(val, str) and val.strip() and val.strip() not in env_vars:
                env_vars.append(val.strip())
    missing_env = [name for name in env_vars if not os.getenv(name)]

    return {
        "status": "pending",
        "kind": "local",
        "id": plugin_id,
        "manifest": manifest.to_dict(),
        "permission_details": describe_permissions(manifest.permissions),
        "env_vars": env_vars,
        "missing_env": missing_env,
        "next_step": "插件已暂存为未启用状态，需在 WebUI /plugins 页面审核权限、录入缺失环境变量并启用重启。",
    }


def stage_mcp_server(
    *,
    server_id: str,
    spec: dict[str, Any],
    overwrite: bool = False,
    config_store: ConfigStore | None = None,
) -> dict[str, Any]:
    """校验并暂存 MCP 服务器配置（强行写入 enabled=false, trusted=false）。"""
    if not isinstance(spec, dict):
        raise ConfigurationError("MCP 配置必须为对象")
    sanitized = deepcopy(spec)
    sanitized["enabled"] = False
    sanitized["trusted"] = False
    cfg = parse_mcp_config(server_id, sanitized)

    store = config_store or ConfigStore()
    _, _, raw = store.read()
    existing = ((raw.get("plugins") or {}).get("mcp") or {}).get(server_id)
    if existing is not None and not overwrite:
        raise ValueError(f"MCP 服务 {server_id} 已存在；如需覆盖请传入 overwrite=true")

    store.upsert_plugin_entry("mcp", server_id, sanitized)
    _, _, env_vars, missing_env = resolve_mcp_secrets(cfg)
    return {
        "status": "pending",
        "kind": "mcp",
        "id": cfg.id,
        "transport": cfg.transport,
        "env_vars": env_vars,
        "missing_env": missing_env,
        "next_step": "MCP 服务已暂存为未启用状态，需在 WebUI /plugins 页面补充所需 .env 变量并启用连接。",
    }


def remove_local_plugin(
    plugin_id: str,
    *,
    delete_files: bool = False,
    plugins_dir: Path | None = None,
    config_store: ConfigStore | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """从 config.yaml 移除本地插件配置（可选删除插件目录文件）。"""
    if not PLUGIN_ID_RE.fullmatch(plugin_id):
        raise PluginManifestError(f"插件 ID 无效: {plugin_id!r}")
    store = config_store or ConfigStore()
    result = store.delete_plugin_entry("items", plugin_id, revision=revision)
    if delete_files:
        root = plugins_dir or state_path("plugins")
        target = (root / plugin_id).resolve()
        if target.exists() and target.is_dir() and target.is_relative_to(root.resolve()) and target != root.resolve():
            shutil.rmtree(target)
    return result
