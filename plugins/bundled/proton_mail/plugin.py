"""Tentacle 内置 Proton 邮箱助手插件（基于 proton-cli，无需 Proton Bridge）。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
from typing import Any


INSTALL_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/roman-16/proton-cli/main/scripts/install.sh | sh"
)
_DEFAULT_LIST_LIMIT = 10
_MAX_LIST_LIMIT = 50
_DEFAULT_HEARTBEAT_LIMIT = 5
_MAX_HEARTBEAT_LIMIT = 20
_SYSTEM_PROTON_PATHS = ("/usr/local/bin/proton", "/usr/bin/proton")


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_proton(configured: str = "") -> str | None:
    """按配置、PATH 与常见安装目录依次探测 proton 可执行文件。"""
    configured = (configured or "").strip()
    candidates: list[str | None] = [configured]
    if configured:
        candidates.append(shutil.which(configured))
    candidates.extend([
        shutil.which("proton"),
        str(Path.home() / ".local" / "bin" / "proton"),
        *_SYSTEM_PROTON_PATHS,
    ])

    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        candidate = os.path.expanduser(str(raw).strip())
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_proton(executable: str, args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """执行 proton 命令并返回 `(returncode, stdout, stderr)`。"""
    cmd = [executable, "--no-input", "--no-color", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"命令执行超时（超过 {timeout} 秒）"
    except FileNotFoundError:
        return -1, "", "未找到 proton 可执行文件"
    except OSError as exc:
        return -1, "", f"执行失败: {exc}"


def _login_hint() -> str:
    return (
        "若尚未登录或登录已过期，请先在服务器终端执行：\n"
        "  proton account login\n"
        "按提示完成邮箱与密码（及二次验证）登录，会话会保存在 proton-cli 的配置目录中。"
    )


def _not_installed_message() -> str:
    return (
        "未检测到 proton CLI，Proton 邮箱功能暂不可用。\n"
        "请在服务器上安装 proton-cli（端到端加密，无需 Proton Bridge 或桌面环境）：\n"
        f"  {INSTALL_COMMAND}\n"
        "安装脚本默认写入 ~/.local/bin/proton；安装后确认 proton 在 PATH 中，"
        "或在 WebUI /plugins 为 proton_mail 配置 executable 完整路径。\n\n"
        + _login_hint()
    )


def _failure_message(action: str, stdout: str, stderr: str) -> str:
    detail = (stderr or "").strip() or (stdout or "").strip() or "未知错误"
    return f"{action}失败：{detail}\n\n{_login_hint()}"


def _format_time(timestamp: Any) -> str:
    """将时间戳转为易读的本地时间格式。"""
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(timestamp or "未知时间")


def _format_size(value: Any) -> str:
    try:
        return f"{float(value) / 1024 / 1024:.2f} MB"
    except (TypeError, ValueError):
        return "未知"


def _format_sender(message: dict[str, Any]) -> str:
    name = str(message.get("from_name") or "").strip()
    address = str(message.get("from_address") or "").strip()
    if name and address:
        return f"{name} <{address}>"
    return name or address or "未知发件人"


def _parse_messages(stdout: str) -> tuple[list[dict[str, Any]], int]:
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("返回值不是 JSON 对象")
    raw_messages = data.get("messages")
    messages = [item for item in raw_messages if isinstance(item, dict)] if isinstance(raw_messages, list) else []
    total = data.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        total = len(messages)
    return messages, total


def setup(ctx: Any) -> None:
    """注册 proton_mail 工具与心跳未读简报钩子。"""

    plugin_config = ctx.config if isinstance(ctx.config, dict) else {}
    configured_executable = str(plugin_config.get("executable") or "")
    heartbeat_limit = _coerce_int(
        plugin_config.get("heartbeat_unread_limit"),
        _DEFAULT_HEARTBEAT_LIMIT,
    )
    heartbeat_limit = max(0, min(heartbeat_limit, _MAX_HEARTBEAT_LIMIT))

    def mail_tool(args: dict) -> str:
        operation = str(args.get("operation") or "list").strip().lower()
        if operation not in {"status", "list", "read", "send"}:
            return f"不支持的操作: {operation}，请使用 status / list / read / send。"

        executable = _find_proton(configured_executable)
        if executable is None:
            return _not_installed_message()

        if operation == "status":
            code, stdout, stderr = _run_proton(executable, ["account", "get", "--output", "json"])
            if code != 0:
                return _failure_message("获取邮箱账户状态", stdout, stderr)
            try:
                data = json.loads(stdout)
                if not isinstance(data, dict):
                    raise ValueError("返回值不是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                return f"解析账户状态失败：{exc}\n原始输出：{stdout.strip()[:1000]}"
            unlocked = data.get("unlocked")
            session = data.get("session")
            session_text = "未知"
            if session is not None:
                session_text = str(session)
                if unlocked is not None:
                    session_text += f"（已解锁: {unlocked}）"
            return (
                "【Proton 账户状态】\n"
                f"• 邮箱: {data.get('email') or '未知'}\n"
                f"• 用户名: {data.get('username') or '未知'}\n"
                f"• 会话状态: {session_text}\n"
                f"• 容量使用: {_format_size(data.get('used_space'))} / {_format_size(data.get('max_space'))}"
            )

        if operation == "list":
            unread_only = bool(args.get("unread_only", False))
            limit = _coerce_int(args.get("limit"), _DEFAULT_LIST_LIMIT)
            if limit <= 0 or limit > _MAX_LIST_LIMIT:
                limit = _DEFAULT_LIST_LIMIT

            cmd = ["mail", "messages", "list", "--limit", str(limit), "--output", "json"]
            if unread_only:
                cmd.append("--unread")

            code, stdout, stderr = _run_proton(executable, cmd)
            if code != 0:
                return _failure_message("获取邮件列表", stdout, stderr)

            try:
                messages, total = _parse_messages(stdout)
            except (json.JSONDecodeError, ValueError) as exc:
                return f"解析邮件列表失败：{exc}\n原始输出：{stdout.strip()[:1000]}"

            title = "【未读邮件】" if unread_only else "【收件箱邮件】"
            if not messages:
                return f"{title} 暂无邮件（总数: {total}）。"

            lines = [f"{title}（共 {total} 封，展示前 {len(messages)} 封）："]
            for index, message in enumerate(messages, 1):
                subject = message.get("subject") or "（无主题）"
                unread_mark = "[未读] " if message.get("unread") else ""
                lines.append(
                    f"{index}. {unread_mark}{subject}\n"
                    f"   发件人: {_format_sender(message)} | 时间: {_format_time(message.get('time'))}\n"
                    f"   ID: {message.get('id', '')}"
                )
            return "\n".join(lines)

        if operation == "read":
            message_id = str(args.get("message_id") or "").strip()
            if not message_id:
                return "读取失败：请提供 message_id。"

            code, stdout, stderr = _run_proton(executable, ["mail", "messages", "get", message_id])
            if code != 0:
                return _failure_message("读取邮件", stdout, stderr)
            return stdout if stdout.strip() else "（邮件内容为空）"

        to = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "").strip()
        if not to:
            return "发送失败：收件人 to 不能为空。"
        if not subject:
            return "发送失败：主题 subject 不能为空。"
        if not body:
            return "发送失败：正文 body 不能为空。"

        code, stdout, stderr = _run_proton(
            executable,
            [
                "mail",
                "messages",
                "send",
                "--to",
                to,
                "--subject",
                subject,
                "--body",
                body,
                "--output",
                "json",
            ],
            timeout=45,
        )
        if code != 0:
            return _failure_message("发送邮件", stdout, stderr)
        return f"邮件已成功发送至 {to}！\n主题: {subject}"

    ctx.register_tool(
        name="proton_mail",
        description=(
            "管理与收发 Proton 加密邮箱（基于 proton-cli）：status 查看账户状态，"
            "list 查看收件箱/未读邮件，read 阅读邮件全文，send 发送邮件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["status", "list", "read", "send"],
                    "description": "操作类型：status（账户状态）、list（邮件列表）、read（读邮件）、send（发邮件）。",
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "仅 list 时有效：是否仅列出未读邮件（默认 false）。",
                },
                "limit": {
                    "type": "integer",
                    "description": "仅 list 时有效：返回邮件数量（默认 10，上限 50）。",
                },
                "message_id": {
                    "type": "string",
                    "description": "仅 read 时必填：邮件 ID。",
                },
                "to": {
                    "type": "string",
                    "description": "仅 send 时必填：收件人邮箱地址。",
                },
                "subject": {
                    "type": "string",
                    "description": "仅 send 时必填：邮件主题。",
                },
                "body": {
                    "type": "string",
                    "description": "仅 send 时必填：邮件正文内容。",
                },
            },
            "required": ["operation"],
        },
        func=mail_tool,
        is_async=False,
    )

    def on_before_heartbeat(_payload: dict) -> dict:
        """心跳前扫描未读邮件；有新邮件时注入未读简报。"""
        if heartbeat_limit <= 0:
            return {}
        executable = _find_proton(configured_executable)
        if executable is None:
            return {}

        code, stdout, stderr = _run_proton(
            executable,
            ["mail", "messages", "list", "--unread", "--limit", str(heartbeat_limit), "--output", "json"],
            timeout=10,
        )
        if code != 0:
            ctx.logger.debug("proton_mail 心跳检查跳过：%s", (stderr or stdout).strip()[:300])
            return {}
        try:
            messages, total = _parse_messages(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            ctx.logger.debug("proton_mail 心跳解析失败：%s", exc)
            return {}
        if not messages:
            return {}

        lines = [f"📬 Proton 邮箱当前有 {total} 封未读邮件："]
        for index, message in enumerate(messages, 1):
            sender = str(message.get("from_name") or message.get("from_address") or "未知发件人").strip()
            subject = message.get("subject") or "（无主题）"
            message_id = str(message.get("id") or "")[:8]
            lines.append(f"  {index}. [{sender}] {subject} (ID: {message_id}...)")
        return {"briefing": "\n".join(lines)}

    ctx.on("before_heartbeat", on_before_heartbeat)
