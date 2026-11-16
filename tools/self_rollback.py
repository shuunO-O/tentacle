"""
self_rollback 工具

列出自我修改备份并恢复指定版本。核心逻辑在 self_backup.py（三通道共享），
本文件只是 agent 工具壳，强制 allow_immutable=False ——
agent 不得通过回滚绕过不可变清单（安全网文件只能手动恢复）。
"""

import json
import logging

import runtime.self_backup as self_backup

logger = logging.getLogger(__name__)


def self_rollback(args: dict) -> str:
    """
    self_rollback 工具入口。

    Args:
        args:
            - command: list / restore（必填）
            - path: 源文件的项目相对路径（restore 必填；list 可选过滤）
            - timestamp: 备份时间戳 YYYYmmdd-HHMMSS（可选，缺省最新）

    Returns:
        JSON 字符串（结果或错误信息）。
    """
    command = args.get("command", "")
    path = args.get("path")

    if command == "list":
        try:
            items = self_backup.list_backups(path)
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        if not items:
            target = f" for {path}" if path else ""
            return json.dumps(
                {
                    "count": 0,
                    "backups": [],
                    "note": f"No backups{target}. Backups are created automatically "
                    "whenever file_edit writes a project file.",
                },
                ensure_ascii=False,
            )
        return json.dumps({"count": len(items), "backups": items}, ensure_ascii=False)

    if command == "restore":
        if not path:
            return json.dumps({"error": "Parameter `path` is required for command: restore"}, ensure_ascii=False)
        result = self_backup.restore(path, args.get("timestamp"), allow_immutable=False)
        return json.dumps(result, ensure_ascii=False)

    return json.dumps(
        {"error": f"Unknown command: {command}. Allowed options are: `list`, `restore`."},
        ensure_ascii=False,
    )


def register_tools(registry):
    registry.register(
        name="self_rollback",
        description=(
            "List or restore automatic backups under data/self_backups created by file_edit on protected files. "
            "`list` supports optional `path` filter; `restore` requires `path` and optional `timestamp` (defaults to newest), "
            "snapshotting the current version first. Immutable safety-net files can only be restored manually."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["list", "restore"],
                    "description": "The command to run: `list` or `restore`.",
                },
                "path": {
                    "type": "string",
                    "description": "Project-relative source file path, e.g. `prompt/soul.md`. Required for `restore`; optional filter for `list`.",
                },
                "timestamp": {
                    "type": "string",
                    "description": "Backup version stamp `YYYYmmdd-HHMMSS` as returned by `list`. Defaults to the newest version.",
                },
            },
            "required": ["command"],
        },
        func=self_rollback,
    )
