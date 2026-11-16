#!/usr/bin/env python3
"""TENTACLE_HOME 离线备份与迁移导入/导出命令行工具（请在停服状态下执行导入）。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.home_backup import (
    BackupError,
    HomeBackupManager,
    apply_pending_restore,
    is_encrypted_archive,
)
from runtime.paths import get_paths


def _resolve_state_root(explicit_home: Path | None) -> Path:
    if explicit_home is not None:
        root = explicit_home.expanduser().resolve()
        if root == Path(root.anchor):
            raise BackupError("TENTACLE_HOME 不能是磁盘根目录")
        return root
    return get_paths().state_root


def _read_passphrase(passphrase_file: Path | None, *, prompt_text: str, confirm: bool = False) -> str:
    if passphrase_file is not None:
        return passphrase_file.read_text(encoding="utf-8").rstrip("\r\n")
    first = getpass.getpass(prompt_text)
    if confirm:
        second = getpass.getpass("请再次输入加密口令: ")
        if first != second:
            raise BackupError("两次输入的口令不一致")
    return first


async def _run(args) -> int:
    state_root = _resolve_state_root(args.home)
    mgr = HomeBackupManager(state_root)

    if args.action == "list":
        snap = mgr.status_snapshot()
        print(json.dumps({
            "policy": snap["policy"],
            "data_epoch": snap["data_epoch"],
            "backups": snap["backups"],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.action == "backup":
        meta = await mgr.create_backup(
            kind="manual",
            include_files=not args.no_files,
            scopes=args.scopes or None,
            workspace_paths=args.workspace_paths or None,
        )
        print(json.dumps({"ok": True, "backup": meta}, ensure_ascii=False, indent=2))
        return 0

    if args.action == "export":
        passphrase = None
        if args.include_env or args.encrypt:
            passphrase = _read_passphrase(
                args.passphrase_file,
                prompt_text="请输入迁移包加密口令（至少 8 位）: ",
                confirm=args.passphrase_file is None,
            )
        tmp, src_path, default_name = await mgr.create_ephemeral_export(
            include_files=not args.no_files,
            include_env=args.include_env,
            passphrase=passphrase,
            scopes=args.scopes or None,
            workspace_paths=args.workspace_paths or None,
        )
        try:
            output = Path(args.output or default_name).expanduser().resolve()
            if output.is_dir():
                output = output / default_name
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_path, output)
        finally:
            tmp.cleanup()
        print(json.dumps({
            "ok": True,
            "output": str(output),
            "includes_files": not args.no_files,
            "includes_env": args.include_env,
            "encrypted": bool(passphrase),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.action in {"inspect", "import"}:
        archive = Path(args.archive).expanduser().resolve()
        if not archive.is_file():
            raise BackupError(f"找不到备份文件：{archive}")
        passphrase = None
        if is_encrypted_archive(archive):
            passphrase = _read_passphrase(args.passphrase_file, prompt_text="请输入迁移包解密口令: ")
        preview = await mgr.inspect_import(archive_path=archive, passphrase=passphrase)
        if args.action == "inspect" or not args.apply:
            print(json.dumps({
                "preview": True,
                **preview,
                "hint": "添加 --apply 参数后将实际写入目标 Home（请确保应用进程已停止）。"
                if args.action == "import"
                else None,
            }, ensure_ascii=False, indent=2))
            return 0
        await mgr.prepare_apply_import(
            preview["import_token"],
            apply_files=not args.no_files,
            apply_env=args.include_env and preview["includes_env"],
            apply_workspace=not args.no_workspace,
            create_pre_backup=not args.no_pre_backup,
        )
        result = apply_pending_restore(state_root)
        print(json.dumps({"ok": (result or {}).get("status") == "succeeded", "result": result}, ensure_ascii=False, indent=2))
        return 0 if (result or {}).get("status") == "succeeded" else 1

    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="显式指定目标 TENTACLE_HOME（默认读取环境变量）")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="列出本机保留的备份快照与当前定期策略")

    bk = sub.add_parser("backup", help="立即创建一条本地手动备份（不含 .env）")
    bk.add_argument("--no-files", action="store_true", help="不包含 data/files/ 对话附件")
    bk.add_argument("--scope", action="append", dest="scopes", choices=("config", "data", "prompt", "skills", "plugins"), help="只包含指定模块（可多次传入）")
    bk.add_argument("--workspace-path", action="append", dest="workspace_paths", help="附带 workspace 下的文件或目录（相对路径，可多次传入）")

    exp = sub.add_parser("export", help="导出一次性迁移归档（可选带加密 .env）")
    exp.add_argument("-o", "--output", help="输出文件路径或目录")
    exp.add_argument("--no-files", action="store_true", help="不包含 data/files/ 对话附件")
    exp.add_argument("--include-env", action="store_true", help="包含 config/.env 凭据（将强制加密）")
    exp.add_argument("--encrypt", action="store_true", help="强制使用口令加密导出包")
    exp.add_argument("--passphrase-file", type=Path, help="从文件读取加密口令（避免交互输入）")
    exp.add_argument("--scope", action="append", dest="scopes", choices=("config", "data", "prompt", "skills", "plugins"), help="只包含指定模块（可多次传入）")
    exp.add_argument("--workspace-path", action="append", dest="workspace_paths", help="附带 workspace 下的文件或目录（相对路径，可多次传入）")

    insp = sub.add_parser("inspect", help="校验并预览迁移包内容")
    insp.add_argument("archive", help="迁移包路径（.tar.gz 或 .tbackup）")
    insp.add_argument("--passphrase-file", type=Path, help="从文件读取解密口令")

    imp = sub.add_parser("import", help="校验迁移包并在停服状态下导入目标 TENTACLE_HOME")
    imp.add_argument("archive", help="迁移包路径（.tar.gz 或 .tbackup）")
    imp.add_argument("--no-files", action="store_true", help="不恢复 data/files/ 对话附件")
    imp.add_argument("--include-env", action="store_true", help="当迁移包内含加密 .env 时一并覆盖目标 .env")
    imp.add_argument("--no-workspace", action="store_true", help="跳过恢复归档内的 workspace/ 产出文件")
    imp.add_argument("--no-pre-backup", action="store_true", help="导入前不自动拍当前快照")
    imp.add_argument("--passphrase-file", type=Path, help="从文件读取解密口令")
    imp.add_argument("--apply", action="store_true", help="实际执行替换恢复（缺少则仅预览）")

    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except BackupError as exc:
        print(f"备份操作失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"备份操作异常：{type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
