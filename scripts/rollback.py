#!/usr/bin/env python3
"""
人工回滚 CLI

与 agent 工具（tools/self_rollback.py）、Web 面板共享 self_backup.py 核心逻辑。
人工通道 allow_immutable=True：允许恢复安全网文件（agent 通道禁止）。

用法：
    .venv/bin/python scripts/rollback.py list [path]
    .venv/bin/python scripts/rollback.py restore <path> [timestamp]
    .venv/bin/python scripts/rollback.py restore prompt/soul.md          # 恢复到最新版
    .venv/bin/python scripts/rollback.py restore web/server.py 20260824-153000
    .venv/bin/python scripts/rollback.py --json list                     # 原始 JSON 输出
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runtime.self_backup as self_backup


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def cmd_list(args) -> int:
    try:
        items = self_backup.list_backups(args.path)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    if not items:
        target = f" {args.path}" if args.path else ""
        print(f"备份区中没有{target}的任何版本。" if args.path else "备份区中还没有任何版本。")
        print("提示：file_edit 每次写入受保护项目文件都会自动生成备份。")
        return 0

    current_source = None
    for it in items:
        if it["source"] != current_source:
            current_source = it["source"]
            print(f"\n{current_source}")
        marker = "  "
        if args.path:
            marker = "* " if it["timestamp"] == items[-1]["timestamp"] else "  "
        print(
            f"{marker}{it['timestamp']}  {_fmt_size(it['size_bytes']).rjust(9)}  {it['backup_path']}"
        )
    print()
    return 0


def cmd_restore(args) -> int:
    result = self_backup.restore(args.path, args.timestamp, allow_immutable=True)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not result.get("ok"):
            print(f"恢复失败：{result.get('error')}", file=sys.stderr)
            return 1
        print(f"已恢复 {result['path']}")
        print(f"  版本来源: {result['restored_from']}")
        if result.get("previous_version_snapshot"):
            print(f"  恢复前快照: {result['previous_version_snapshot']}（如需撤销，可再恢复此快照）")
    return 0 if result.get("ok") else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rollback", description="自我修改备份回滚工具（人工通道）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON（便于脚本处理）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出备份版本（缺省全部）")
    p_list.add_argument("path", nargs="?", default=None, help="可选：只列某个源文件")

    p_restore = sub.add_parser("restore", help="恢复源文件到指定版本（缺省最新）")
    p_restore.add_argument("path", help="源文件的项目相对路径")
    p_restore.add_argument("timestamp", nargs="?", default=None, help="版本时间戳 YYYYmmdd-HHMMSS")

    args = parser.parse_args(argv)
    return cmd_list(args) if args.command == "list" else cmd_restore(args)


if __name__ == "__main__":
    sys.exit(main())
