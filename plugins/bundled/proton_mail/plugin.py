"""Tentacle 内置 Proton 邮箱助手插件（基于 proton-cli，无需 Proton Bridge）。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any


INSTALL_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/roman-16/proton-cli/main/scripts/install.sh | sh"
)
_DEFAULT_LIST_LIMIT = 10
_MAX_LIST_LIMIT = 50
_MAX_MARK_BATCH = 50
_DEFAULT_HEARTBEAT_LIMIT = 5
_MAX_HEARTBEAT_LIMIT = 20
_MAX_READ_OUTPUT = 20000
_MAX_ERROR_OUTPUT = 2000
_MAX_INLINE_TEXT = 200
_SYSTEM_PROTON_PATHS = ("/usr/local/bin/proton", "/usr/bin/proton")
_CONTROL_WS_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")


def _validate_message_id(raw: Any) -> tuple[str | None, str | None]:
    message_id = str(raw or "").strip()
    if not message_id:
        return None, "请提供 message_id。"
    if message_id.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in message_id):
        return None, "message_id 格式无效（不能以 '-' 开头或包含空白/控制字符）。"
    return message_id, None


def _collect_message_ids(args: dict) -> tuple[list[str], str | None]:
    raw_items: list[Any] = []
    if args.get("message_id") is not None and str(args.get("message_id")).strip():
        raw_items.append(args.get("message_id"))
    raw_list = args.get("message_ids")
    if raw_list is not None:
        if not isinstance(raw_list, list):
            return [], "message_ids 必须为邮件 ID 字符串数组。"
        raw_items.extend(raw_list)
    if not raw_items:
        return [], "请提供 message_id 或 message_ids。"

    ids: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        mid, err = _validate_message_id(item)
        if err or not mid:
            return [], err or "message_id 格式无效。"
        if mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if len(ids) > _MAX_MARK_BATCH:
        return [], f"单次最多批量标记 {_MAX_MARK_BATCH} 封邮件。"
    return ids, None


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truncate(text: str, limit: int = _MAX_READ_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.3) :]
    return f"{head}\n...[输出过长，已截断，共 {len(text)} 字符]...\n{tail}"


def _clean_inline(value: Any, max_len: int = _MAX_INLINE_TEXT) -> str:
    """将邮件主题/发件人等外部字段压成单行并限长，防止换行注入破坏列表或心跳格式。"""
    text = _CONTROL_WS_RE.sub(" ", str(value or "")).strip()
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _is_executable_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _find_proton(configured: str = "") -> str | None:
    """按配置、PATH 与常见安装目录探测 proton 可执行文件。

    若显式配置了 `executable`，仅校验该配置，不静默回退到其他路径。
    """
    configured = (configured or "").strip()
    if configured:
        candidates: list[str | None] = [configured, shutil.which(configured)]
    else:
        home_cand: str | None = None
        try:
            home_cand = str(Path.home() / ".local" / "bin" / "proton")
        except RuntimeError:
            home_cand = None
        candidates = [
            shutil.which("proton"),
            home_cand,
            *_SYSTEM_PROTON_PATHS,
        ]

    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        candidate = os.path.expanduser(str(raw).strip())
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _is_executable_file(candidate):
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
            encoding="utf-8",
            errors="replace",
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


def _not_installed_message(configured: str = "") -> str:
    configured = (configured or "").strip()
    if configured:
        return (
            f"配置的 proton 可执行文件不可用或无执行权限：{configured}\n"
            "请检查路径是否正确并具备执行权限，或留空 executable 以启用自动探测。\n\n"
            + _login_hint()
        )
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
    return f"{action}失败：{_truncate(detail, _MAX_ERROR_OUTPUT)}\n\n{_login_hint()}"


def _format_time(timestamp: Any) -> str:
    """将时间戳转为易读的本地时间格式。"""
    try:
        if isinstance(timestamp, bool):
            raise ValueError("bool timestamp")
        return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return _clean_inline(timestamp) or "未知时间"


def _format_size(value: Any) -> str:
    try:
        if isinstance(value, bool):
            raise ValueError("bool size")
        return f"{float(value) / 1024 / 1024:.2f} MB"
    except (TypeError, ValueError):
        return "未知"


def _format_sender(message: dict[str, Any]) -> str:
    name = _clean_inline(message.get("from_name"), 120)
    address = _clean_inline(message.get("from_address"), 120)
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
    """注册 proton_mail 异步工具与心跳未读简报钩子。"""

    plugin_config = ctx.config if isinstance(ctx.config, dict) else {}
    configured_executable = str(plugin_config.get("executable") or "").strip()
    heartbeat_limit = _coerce_int(
        plugin_config.get("heartbeat_unread_limit"),
        _DEFAULT_HEARTBEAT_LIMIT,
    )
    heartbeat_limit = max(0, min(heartbeat_limit, _MAX_HEARTBEAT_LIMIT))
    allow_send = bool(plugin_config.get("allow_send", False))

    async def mail_tool(args: dict) -> str:
        operation = str(args.get("operation") or "list").strip().lower()
        if operation not in {"status", "list", "read", "mark_read", "mark_unread", "send"}:
            return f"不支持的操作: {operation}，请使用 status / list / read / mark_read / mark_unread / send。"

        executable = _find_proton(configured_executable)
        if executable is None:
            return _not_installed_message(configured_executable)

        if operation == "status":
            code, stdout, stderr = await asyncio.to_thread(
                _run_proton, executable, ["account", "get", "--output", "json"]
            )
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
                session_text = _clean_inline(session, 120) or "未知"
                if unlocked is not None:
                    session_text += f"（已解锁: {unlocked}）"
            return (
                "【Proton 账户状态】\n"
                f"• 邮箱: {_clean_inline(data.get('email'), 120) or '未知'}\n"
                f"• 用户名: {_clean_inline(data.get('username'), 120) or '未知'}\n"
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

            code, stdout, stderr = await asyncio.to_thread(_run_proton, executable, cmd)
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
                subject = _clean_inline(message.get("subject")) or "（无主题）"
                unread_mark = "[未读] " if message.get("unread") else ""
                message_id = _clean_inline(message.get("id"), 160)
                lines.append(
                    f"{index}. {unread_mark}{subject}\n"
                    f"   发件人: {_format_sender(message)} | 时间: {_format_time(message.get('time'))}\n"
                    f"   ID: {message_id}"
                )
            return "\n".join(lines)

        if operation == "read":
            message_id, err = _validate_message_id(args.get("message_id"))
            if err or not message_id:
                return f"读取失败：{err}"

            code, stdout, stderr = await asyncio.to_thread(
                _run_proton, executable, ["mail", "messages", "get", message_id]
            )
            if code != 0:
                return _failure_message("读取邮件", stdout, stderr)

            body_text = _truncate(stdout.strip()) if stdout.strip() else "（邮件内容为空）"
            should_mark_read = True if args.get("mark_read") is None else bool(args.get("mark_read"))
            if should_mark_read:
                mark_code, mark_out, mark_err = await asyncio.to_thread(
                    _run_proton, executable, ["mail", "messages", "mark", "read", message_id]
                )
                if mark_code != 0:
                    detail = _truncate((mark_err or mark_out).strip() or "未知错误", 300)
                    body_text += f"\n\n[提示：邮件已读取，但自动标记已读失败：{detail}]"
            return body_text

        if operation in {"mark_read", "mark_unread"}:
            ids, err = _collect_message_ids(args)
            if err:
                action_name = "标记已读" if operation == "mark_read" else "标记未读"
                return f"{action_name}失败：{err}"
            target_state = "read" if operation == "mark_read" else "unread"
            state_label = "已读" if operation == "mark_read" else "未读"
            code, stdout, stderr = await asyncio.to_thread(
                _run_proton, executable, ["mail", "messages", "mark", target_state, *ids]
            )
            if code != 0:
                return _failure_message(f"标记邮件为{state_label}", stdout, stderr)
            return f"已将 {len(ids)} 封邮件标记为{state_label}：{', '.join(ids)}"

        if not allow_send:
            return (
                "发送失败：当前 proton_mail 插件处于只读模式（allow_send=false）。"
                "如需发送邮件，请在 WebUI /plugins 中为 proton_mail 开启 allow_send 并重启应用。"
            )

        to = str(args.get("to") or "").strip()
        subject = _clean_inline(args.get("subject"), 300)
        body = str(args.get("body") or "").strip()
        if not to:
            return "发送失败：收件人 to 不能为空。"
        if to.startswith("-") or any(ch in to for ch in "\r\n\x00"):
            return "发送失败：收件人 to 格式无效。"
        if not subject:
            return "发送失败：主题 subject 不能为空。"
        if not body:
            return "发送失败：正文 body 不能为空。"

        code, stdout, stderr = await asyncio.to_thread(
            _run_proton,
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
            "list 查看收件箱/未读邮件，read 阅读邮件全文（默认自动标记已读），"
            "mark_read / mark_unread 标记已读或未读，send 发送邮件（需配置开启 allow_send）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["status", "list", "read", "mark_read", "mark_unread", "send"],
                    "description": (
                        "操作类型：status（账户状态）、list（邮件列表）、read（读邮件，默认标为已读）、"
                        "mark_read（标记已读）、mark_unread（标记未读）、send（发邮件）。"
                    ),
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
                    "description": "read 时必填；mark_read / mark_unread 时可传单个邮件 ID。",
                },
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "仅 mark_read / mark_unread 时有效：批量标记的邮件 ID 列表（单次最多 50 封）。",
                },
                "mark_read": {
                    "type": "boolean",
                    "description": "仅 read 时有效：读取后是否自动标记为已读（默认 true）。",
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
        is_async=True,
    )

    async def on_before_heartbeat(_payload: dict) -> dict:
        """心跳前异步扫描未读邮件；有新邮件时注入未读简报。"""
        if heartbeat_limit <= 0:
            return {}
        executable = _find_proton(configured_executable)
        if executable is None:
            return {}

        code, stdout, stderr = await asyncio.to_thread(
            _run_proton,
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
            sender = _clean_inline(
                message.get("from_name") or message.get("from_address") or "未知发件人", 120
            ) or "未知发件人"
            subject = _clean_inline(message.get("subject")) or "（无主题）"
            message_id = _clean_inline(message.get("id"), 160)
            id_suffix = f" (ID: {message_id})" if message_id else ""
            lines.append(f"  {index}. [{sender}] {subject}{id_suffix}")
        return {"briefing": "\n".join(lines)}

    ctx.on("before_heartbeat", on_before_heartbeat)
