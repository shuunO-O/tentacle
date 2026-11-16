"""
自我修改备份核心模块

为回滚三通道提供统一的备份列举与恢复逻辑：
- tools/self_rollback.py   agent 工具壳（强制禁止恢复不可变清单文件）
- scripts/rollback.py      人工 CLI
- web/server.py            Web 面板 API

备份布局（与 tools/file_edit.py 的写入约定一致）：
    data/self_backups/<源文件相对目录>/<文件名>.<YYYYmmdd-HHMMSS>
审计日志：
    data/self_backups/audit.log（JSONL，与 editor 共用）

本文件在不可变清单内，agent 无法自行修改，只能手动维护。
"""

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from runtime.paths import get_paths, state_path

logger = logging.getLogger(__name__)


# 与 tools/file_edit.py 保持一致（恢复目标校验用）。
# 注意：agent 通道不允许恢复清单内文件，防止绕过 editor 的写入限制。
_IMMUTABLE_FILES = (
    "tools/file_edit.py",
    "tools/str_replace_editor.py",
    "terminal_manager.py",
    "self_backup.py",
    "runtime/terminal_manager.py",
    "runtime/self_backup.py",
    "tools/self_rollback.py",
    "scripts/rollback.py",
)

# 备份文件名规则：<原始名>.<YYYYmmdd-HHMMSS> 或同秒防冲突后缀 <YYYYmmdd-HHMMSS-xxxx>
_STAMP_RE = re.compile(r"^(?P<name>.+)\.(?P<stamp>\d{8}-\d{6}(?:-[0-9a-f]{4})?)$")


def safe_rel_path(path_str: str) -> Path:
    """
    把用户输入路径规范化为项目根下的相对路径。

    Raises:
        ValueError: 路径为空、指向项目根本身或越出项目根（防路径穿越）。
    """
    raw = str(path_str or "").strip()
    if not raw:
        raise ValueError("path is empty")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = get_paths().state_root / p
    # normpath 消解 .. 与 .；不解析符号链接（与 file_edit 行为一致）
    normalized = Path(os.path.normpath(str(p)))
    try:
        rel = normalized.relative_to(get_paths().state_root)
    except ValueError as e:
        raise ValueError(f"path escapes project root: {raw}") from e
    if rel.as_posix() == ".":
        raise ValueError("path is the project root itself")
    return rel


def _parse_stamped_name(stamped: str) -> tuple[str, str] | None:
    """从 'server.py.20260824-172103' 解析 ('server.py', '20260824-172103')；不匹配返回 None"""
    m = _STAMP_RE.match(stamped)
    if not m:
        return None
    return m.group("name"), m.group("stamp")


def list_backups(path: str | None = None) -> list[dict]:
    """
    列出备份区中的版本条目。

    Args:
        path: 源文件的项目相对路径；None 列出全部。

    Returns:
        条目列表（按 source、timestamp 升序），每项含
        source / timestamp / backup_path / size_bytes，路径均为相对项目根的 POSIX 格式。
    """
    rel_filter = safe_rel_path(path) if path else None
    items: list[dict] = []
    if not state_path("data/self_backups").is_dir():
        return items
    for f in sorted(state_path("data/self_backups").rglob("*")):
        if not f.is_file() or f == state_path("data/self_backups/audit.log"):
            continue
        parsed = _parse_stamped_name(f.name)
        if parsed is None:
            continue
        source_name, stamp = parsed
        source = (f.parent.relative_to(state_path("data/self_backups")) / source_name).as_posix()
        if rel_filter is not None and source != rel_filter.as_posix():
            continue
        items.append(
            {
                "source": source,
                "timestamp": stamp,
                "backup_path": f.relative_to(get_paths().state_root).as_posix(),
                "size_bytes": f.stat().st_size,
            }
        )
    items.sort(key=lambda x: (x["source"], x["timestamp"]))
    return items


def _append_audit(entry: dict) -> None:
    """追加 JSONL 审计记录（与 editor 的 audit.log 同格式）"""
    state_path("data/self_backups/audit.log").parent.mkdir(parents=True, exist_ok=True)
    payload = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    with open(state_path("data/self_backups/audit.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _atomic_write_bytes(p: Path, data: bytes) -> None:
    """原子写入字节内容：临时文件 + os.replace"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _snapshot_current(target: Path, rel: Path) -> str:
    """把当前版本快照进备份区，返回相对项目根的快照路径（带随机后缀防同秒冲突）"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap_path = state_path("data/self_backups") / rel.parent / f"{target.name}.{stamp}"
    if snap_path.exists():
        snap_path = state_path("data/self_backups") / rel.parent / f"{target.name}.{stamp}-{uuid.uuid4().hex[:4]}"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_bytes(target.read_bytes())
    return snap_path.relative_to(get_paths().state_root).as_posix()


def restore(path: str, timestamp: str | None = None, allow_immutable: bool = False) -> dict:
    """
    把源文件恢复为指定备份版本（缺省最新版）。

    恢复前会把当前版本再快照一份到备份区，保证任何一次恢复本身可被撤销。

    Args:
        path: 源文件的项目相对路径
        timestamp: 备份时间戳（YYYYmmdd-HHMMSS）；None 取最新
        allow_immutable: 是否允许恢复不可变清单文件。
            agent 工具必须传 False；人工 CLI / Web 面板传 True。

    Returns:
        {"ok": True, ...} 或 {"ok": False, "error": ...}
    """
    try:
        rel = safe_rel_path(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    posix = rel.as_posix()
    if rel.name == ".env":
        return {"ok": False, "error": "refusing to touch .env"}
    if not allow_immutable and posix in _IMMUTABLE_FILES:
        return {
            "ok": False,
            "error": f"refusing to restore immutable file: {posix} (manual channel only)",
        }

    versions = list_backups(posix)
    if not versions:
        return {"ok": False, "error": f"no backups found for {posix}"}

    chosen: dict | None = None
    if timestamp:
        stamp = str(timestamp).strip()
        chosen = next((v for v in versions if v["timestamp"] == stamp), None)
        if chosen is None:
            avail = ", ".join(v["timestamp"] for v in versions[-5:])
            return {"ok": False, "error": f"timestamp {stamp} not found; available: {avail}"}
    else:
        chosen = versions[-1]

    target = get_paths().state_root / rel
    backup_file = get_paths().state_root / chosen["backup_path"]

    snapshot = ""
    if target.exists():
        snapshot = _snapshot_current(target, rel)

    try:
        _atomic_write_bytes(target, backup_file.read_bytes())
    except OSError as e:
        return {"ok": False, "error": f"restore failed: {e}"}

    _append_audit(
        {
            "command": "restore",
            "path": posix,
            "backup": snapshot,
            "restored_from": chosen["backup_path"],
        }
    )
    logger.info(f"[self_backup] restore {posix} <- {chosen['backup_path']} (snapshot: {snapshot or '-'})")

    result = {
        "ok": True,
        "path": posix,
        "restored_from": chosen["backup_path"],
        "restored_timestamp": chosen["timestamp"],
    }
    if snapshot:
        result["previous_version_snapshot"] = snapshot
    return result
