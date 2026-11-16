"""
发送消息工具

模型通过调用此工具向已接入的外部平台（由 channels:register 插件动态注册）发送消息。
工具等待通道确认发送结果后才返回；TOOL_RESULT 与归档只记录确认后的外发消息。
"""

import inspect
import json
import logging
from typing import Any, Awaitable, Callable

from runtime.tool_registry import registry as global_tool_registry
from tools._output import compact_json


logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS: set[str] = set()
_senders: dict[str, Callable[..., Awaitable[dict]]] = {}
_channel_meta: dict[str, dict[str, Any]] = {}


def callable_accepts_target(func: Callable[..., Any]) -> bool:
    """判断通道发送函数是否接受第二个位置参数或 `target` 关键字参数。"""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    params = list(sig.parameters.values())
    if any(p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD) for p in params):
        return True
    if "target" in sig.parameters:
        return True
    positional = [
        p for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


async def invoke_sender(
    func: Callable[..., Any],
    text: str,
    *,
    target: str | None = None,
    accepts_target: bool = False,
) -> dict[str, Any]:
    """统一调用通道发送函数：兼容单参数 `(text)` 与可选 `(text, target)` / `(text, target=...)`。"""
    if target is not None and accepts_target:
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            sig = None
        if sig is not None and (
            "target" in sig.parameters
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        ):
            res = func(text, target=target)
        else:
            res = func(text, target)
    else:
        res = func(text)
    if inspect.isawaitable(res):
        res = await res
    if isinstance(res, dict):
        return res
    return {"ok": True, "detail": res}


def has_active_channels() -> bool:
    """返回当前是否存在至少一个已启用的外部发送通道。"""
    return bool(_senders)


def active_platforms() -> list[str]:
    """返回当前已注册/已知外部平台名（有序）。"""
    return sorted(SUPPORTED_PLATFORMS | set(_senders))


def _refresh_send_message_schema(registry=None) -> None:
    target = registry or global_tool_registry
    tool = getattr(target, "_tools", {}).get("send_message")
    if not isinstance(tool, dict):
        return
    params = tool.get("parameters")
    if not isinstance(params, dict):
        return
    props = params.get("properties")
    if not isinstance(props, dict):
        return

    platforms = active_platforms()
    plat_prop = props.get("platform")
    if isinstance(plat_prop, dict):
        if platforms:
            plat_prop["enum"] = platforms
            details = []
            for p in platforms:
                desc = str((_channel_meta.get(p) or {}).get("description") or "").strip()
                details.append(f"{p}（{desc}）" if desc else p)
            plat_prop["description"] = "目标外部平台，当前已启用: " + "、".join(details) + "。"
        else:
            plat_prop.pop("enum", None)
            plat_prop["description"] = "目标外部平台（当前尚未启用外部通道插件）。"

    target_prop = props.get("target")
    if isinstance(target_prop, dict):
        target_hints = []
        for p in platforms:
            meta = _channel_meta.get(p) or {}
            if meta.get("supports_target"):
                tdesc = str(meta.get("target_description") or "").strip()
                target_hints.append(f"{p}: {tdesc}" if tdesc else p)
        if target_hints:
            target_prop["description"] = (
                "可选目标会话/频道/用户 ID；留空则由插件默认路由。支持 target 的平台 — "
                + "；".join(target_hints)
                + "。"
            )
        else:
            target_prop["description"] = "可选目标会话/频道/用户 ID；支持多目标路由的平台插件使用，默认留空。"


def register_sender(
    platform: str,
    sender: Callable[..., Awaitable[dict]],
    *,
    description: str = "",
    supports_target: bool | None = None,
    target_description: str = "",
    registry=None,
) -> None:
    if isinstance(platform, str) and platform.strip():
        cleaned = platform.strip()
        SUPPORTED_PLATFORMS.add(cleaned)
        _senders[cleaned] = sender
        resolved_supports_target = (
            callable_accepts_target(sender) if supports_target is None else bool(supports_target)
        )
        _channel_meta[cleaned] = {
            "description": str(description or "").strip(),
            "supports_target": resolved_supports_target,
            "target_description": str(target_description or "").strip(),
        }
    _refresh_send_message_schema(registry)


def unregister_sender(platform: str, sender: Callable[..., Awaitable[dict]], *, registry=None) -> None:
    if _senders.get(platform) == sender:
        del _senders[platform]
        _channel_meta.pop(platform, None)
    if platform not in _senders:
        SUPPORTED_PLATFORMS.discard(platform)
    _refresh_send_message_schema(registry)


async def send_message(args: dict) -> str:
    """
    向指定外部平台发送消息

    Args:
        args: 参数字典
            - text: 消息内容（必填）
            - platform: 目标平台（必填），由已启用通道插件注册
            - target: 目标会话/群/用户 ID（可选）

    Returns:
        发送状态字符串（成功以 "已发送" 开头，失败返回含 error 的 JSON）
    """
    text = args.get("text", "")
    platform = args.get("platform", "")
    target = args.get("target")

    if not isinstance(text, str) or not text.strip():
        return json.dumps({"error": "发送失败：text 不能为空"}, ensure_ascii=False)

    if not platform or not isinstance(platform, str) or not platform.strip():
        return json.dumps({"error": "发送失败：platform 不能为空"}, ensure_ascii=False)
    platform = platform.strip()

    known = SUPPORTED_PLATFORMS | set(_senders)
    if platform not in known:
        logger.warning(f"send_message 指定了未接入的外部平台: {platform}")
        return json.dumps({"error": f"发送失败：平台尚未接入（{platform}）"}, ensure_ascii=False)

    sender = _senders.get(platform)
    if sender is None:
        return json.dumps({"error": f"发送失败：{platform} 发送通道未启用"}, ensure_ascii=False)

    if target is not None:
        if not isinstance(target, str):
            return json.dumps({"error": "发送失败：target 必须是字符串"}, ensure_ascii=False)
        target = target.strip() or None

    meta = _channel_meta.get(platform) or {}
    accepts_target = bool(meta.get("supports_target")) or callable_accepts_target(sender)
    if target is not None and not accepts_target:
        return json.dumps({"error": f"发送失败：平台 {platform} 不支持指定 target"}, ensure_ascii=False)

    try:
        result = await invoke_sender(sender, text, target=target, accepts_target=accepts_target)
    except Exception as exc:
        logger.warning("[send_message] platform=%s 发送失败: %s", platform, exc)
        return json.dumps({"error": str(exc), "platform": platform}, ensure_ascii=False)
    if result.get("error"):
        return compact_json(result)
    if "total_chunks" in result and result.get("sent_chunks") != result["total_chunks"]:
        return compact_json({"error": "发送未全部确认，请勿整条重发", **result})
    receipt = "已发送"
    if "sent_chunks" in result:
        receipt += f":{result['sent_chunks']}段"
    if result.get("sent_files"):
        receipt += f" | 附件:{result['sent_files']}"
    extra = {key: value for key, value in result.items()
             if key not in ("sent_chunks", "total_chunks", "sent_files")}
    if extra:
        receipt += "\n" + compact_json(extra)
    return receipt


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="send_message",
        description="向已启用的外部消息平台通道发送消息。",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要发送的消息内容。",
                },
                "platform": {
                    "type": "string",
                    "description": "目标外部平台（由通道插件动态注册）。",
                },
                "target": {
                    "type": "string",
                    "description": "可选目标会话/频道/用户 ID；支持多目标路由的平台插件使用，默认留空。",
                },
            },
            "required": ["text", "platform"],
        },
        func=send_message,
        is_async=True,
    )
    _refresh_send_message_schema(registry)
