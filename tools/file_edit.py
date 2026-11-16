"""file_edit 工具：专注创建新文本文件与精确字符串替换，与 file_read 分工配合。"""

import json
import logging
import os
import time
import uuid
from pathlib import Path

from attachments.outgoing import check_file_access
from runtime.paths import get_paths, state_path

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
_BIN_CHECK_SIZE = 64 * 1024
_MAX_ERR_SNIPPET = 200
_BACKUP_EXCLUDES = (
    "workspace",
    "data",
    "logs",
    "tmp",
    ".venv",
    ".git",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
    "certs",
)
_FORBIDDEN_DIR_PREFIXES = (("data", "self_backups"),)
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

DEFAULT_DESCRIPTION = (
    "Create a new UTF-8 text file or replace exact strings in an existing file.\n"
    "* Omit `old_str` (or pass empty string) to create a new file with `new_str`; fails if the file already exists.\n"
    "* Provide `old_str` and `new_str` to replace text in an existing file (`new_str=\"\"` deletes `old_str`).\n"
    "* `old_str` must match verbatim and uniquely unless `replace_all=true`.\n"
    "* Relative paths are resolved against `TENTACLE_HOME`."
)


def _resolve(path: str) -> Path:
    """解析路径：展开 ~，相对路径基于 TENTACLE_HOME，并规范化消解 .. 与 .。"""
    p = Path(path.strip()).expanduser()
    if not p.is_absolute():
        p = get_paths().state_root / p
    return Path(os.path.normpath(str(p)))


def _is_dotenv(path: Path) -> bool:
    return path.name.lower().startswith(".env") or path.resolve(strict=False).name.lower().startswith(".env")


def _forbidden_write_reason(p: Path) -> str | None:
    """检查敏感凭据文件及 data/self_backups 禁区，命中返回拒绝原因。"""
    if _is_dotenv(p):
        return f"Write rejected: {p} is a secret file (.env). Modifying it is forbidden."
    resolved = p.resolve(strict=False)
    try:
        check_file_access(p)
        check_file_access(resolved)
    except PermissionError:
        return f"Write rejected: {p} is a protected credential or system file. Modifying it is forbidden."
    state_root = get_paths().state_root
    for candidate in (p, resolved):
        try:
            rel = candidate.relative_to(state_root)
        except ValueError:
            continue
        if len(rel.parts) >= 2 and tuple(rel.parts[:2]) in _FORBIDDEN_DIR_PREFIXES:
            return (
                f"Write rejected: {p} is inside data/self_backups (audit & backup area). "
                "Tampering with it is forbidden."
            )
    return None


def _immutable_reason(p: Path) -> str | None:
    """检查自我修改安全网不可变清单（同时覆盖 state_root 与 app_root）。"""
    paths = get_paths()
    resolved = p.resolve(strict=False)
    for root in (paths.state_root, paths.app_root):
        for candidate in (p, resolved):
            try:
                rel = candidate.relative_to(root)
            except ValueError:
                continue
            if rel.as_posix() in _IMMUTABLE_FILES:
                return (
                    f"Write rejected: {p} is part of the self-modification safety net "
                    "(immutable list). It can only be changed manually."
                )
    return None


def _python_syntax_error(p: Path, content: str) -> str | None:
    """写前校验 .py 文件语法，避免语法错误直接落盘。"""
    if p.suffix != ".py":
        return None
    try:
        compile(content, str(p), "exec")
    except SyntaxError as e:
        snippet = (e.text or "").strip()
        suffix = f" -> {snippet}" if snippet else ""
        return (
            f"Write rejected: {p} contains invalid Python syntax at line {e.lineno}: "
            f"{e.msg}{suffix}. Fix the syntax error and retry."
        )
    return None


def _read_text_checked(p: Path) -> str:
    """带大小上限、二进制检测和 UTF-8 校验读取文本。"""
    size = p.stat().st_size
    if size > _MAX_FILE_SIZE:
        raise ValueError(
            f"File {p} is too large ({size / 1024 / 1024:.1f}MB, limit {_MAX_FILE_SIZE // 1024 // 1024}MB). "
            "Use the terminal tool instead."
        )
    raw = p.read_bytes()
    if b"\x00" in raw[:_BIN_CHECK_SIZE]:
        raise ValueError(f"{p} is a binary file. Use the terminal tool to handle it.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{p} is not UTF-8 encoded. Use the terminal tool (e.g. iconv).") from e


def _match_offsets(content: str, search: str) -> list[int]:
    """返回 search 在 content 中所有不重叠出现位置的偏移列表。"""
    offsets = []
    offset = 0
    while True:
        match = content.find(search, offset)
        if match < 0:
            return offsets
        offsets.append(match)
        offset = match + len(search)


def _line_numbers_at(content: str, offsets: list[int]) -> list[int]:
    """把字符偏移列表换算为 1-based 行号列表。"""
    line = 1
    cursor = 0
    result = []
    for offset in offsets:
        while cursor < offset:
            if content[cursor] == "\n":
                line += 1
            cursor += 1
        result.append(line)
    return result


def _backup_if_protected(p: Path, command: str) -> str:
    """TENTACLE_HOME 下排除目录之外的文件写入前自动备份并记录审计日志。"""
    try:
        rel = p.relative_to(get_paths().state_root)
    except ValueError:
        return ""
    if not rel.parts or rel.parts[0] in _BACKUP_EXCLUDES:
        return ""

    backup_path = None
    if p.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = state_path("data/self_backups") / rel.parent
        backup_path = backup_dir / f"{p.name}.{stamp}"
        if backup_path.exists():
            backup_path = backup_dir / f"{p.name}.{stamp}-{uuid.uuid4().hex[:4]}"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_bytes(p.read_bytes())

    backup_rel = backup_path.relative_to(get_paths().state_root).as_posix() if backup_path else ""
    audit = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": command,
        "path": rel.as_posix(),
        "backup": backup_rel,
    }
    audit_file = state_path("data/self_backups/audit.log")
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(audit, ensure_ascii=False) + "\n")

    logger.info(f"[self_modify_audit] {command} {rel} -> backup: {backup_path or '(new file)'}")
    if backup_rel:
        return f"\n[已备份原文件到 {backup_rel} 并记录审计日志]"
    return "\n[保护区新建文件，已记录审计日志]"


def _atomic_write(p: Path, content: str):
    """临时文件 + os.replace 原子写入 UTF-8 文本。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, p)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _create_file(p: Path, new_value: str) -> str:
    if p.is_dir():
        return f"The path {p} is a directory and cannot be used as a file."
    if p.exists():
        return f"File already exists at: {p}. Cannot overwrite existing files without `old_str`."
    syntax_err = _python_syntax_error(p, new_value)
    if syntax_err:
        return syntax_err
    notice = _backup_if_protected(p, "create")
    try:
        _atomic_write(p, new_value)
    except OSError as e:
        return f"Write failed: {e}"
    logger.info(f"[file_edit] create {p} ({len(new_value)} chars)")
    return f"New file created successfully at: {p}{notice}"


def _replace_in_file(p: Path, old_value: str, new_value: str, replace_all: bool) -> str:
    if not p.exists():
        return f"The path {p} does not exist. Omit `old_str` to create a new file."
    if p.is_dir():
        return f"The path {p} is a directory and cannot be edited as a file."
    try:
        before = _read_text_checked(p)
    except ValueError as e:
        return str(e)

    offsets = _match_offsets(before, old_value)
    if not offsets:
        snippet = old_value if len(old_value) <= _MAX_ERR_SNIPPET else old_value[:_MAX_ERR_SNIPPET] + "..."
        return f"No replacement was performed, old_str `{snippet}` did not appear verbatim in {p}."
    if len(offsets) > 1 and not replace_all:
        lines = _line_numbers_at(before, offsets)
        return (
            f"No replacement was performed. Multiple occurrences of old_str in lines "
            f"[{', '.join(map(str, lines))}]. Please include more context in `old_str` to make it unique "
            "or set `replace_all=true`."
        )

    if replace_all:
        after = before.replace(old_value, new_value)
    else:
        offset = offsets[0]
        after = before[:offset] + new_value + before[offset + len(old_value):]

    syntax_err = _python_syntax_error(p, after)
    if syntax_err:
        return syntax_err
    notice = _backup_if_protected(p, "replace")
    try:
        _atomic_write(p, after)
    except OSError as e:
        return f"Write failed: {e}"
    logger.info(f"[file_edit] replace {p}")
    return f"The file {p} has been edited successfully.{notice}"


def file_edit(args: dict) -> str:
    """file_edit 工具入口：新建文本文件或精确字符串替换。"""
    raw_path = args.get("path", "")
    if not isinstance(raw_path, str) or not raw_path.strip() or any(ord(c) < 32 for c in raw_path):
        return "path must be a non-empty string without control characters"

    new_str = args.get("new_str")
    if new_str is None:
        return "Parameter `new_str` is required."

    replace_all = args.get("replace_all", False)
    if not isinstance(replace_all, bool):
        return "Parameter `replace_all` must be a boolean."

    p = _resolve(raw_path)
    forbidden = _forbidden_write_reason(p)
    if forbidden:
        return forbidden
    immutable = _immutable_reason(p)
    if immutable:
        return immutable

    old_str = args.get("old_str")
    new_value = str(new_str)
    if old_str is None or str(old_str) == "":
        return _create_file(p, new_value)
    return _replace_in_file(p, str(old_str), new_value, replace_all)


def register_tools(registry):
    registry.register(
        name="file_edit",
        description=DEFAULT_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file. Relative paths are resolved against `TENTACLE_HOME`.",
                },
                "new_str": {
                    "type": "string",
                    "description": "File content when creating a new file, or replacement text when `old_str` is provided.",
                },
                "old_str": {
                    "type": "string",
                    "description": "Exact text to replace. Omit or pass empty string to create a new file.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "When true, replace all occurrences of `old_str` instead of requiring a unique match.",
                },
            },
            "required": ["path", "new_str"],
            "additionalProperties": False,
        },
        func=file_edit,
    )
