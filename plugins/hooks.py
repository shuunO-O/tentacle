"""插件生命周期事件总线（HookBus）：支持同步/异步处理器、观察/修改权限隔离与错误兜底。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import inspect
import logging
from typing import Any, Callable


logger = logging.getLogger(__name__)

HOOK_EVENTS = frozenset({
    "before_round",
    "after_round",
    "before_tool_call",
    "after_tool_result",
    "before_heartbeat",
    "on_session_switch",
    "on_daily_compress",
})


@dataclass
class _HookSubscription:
    event: str
    handler: Callable[[dict[str, Any]], Any]
    source: str
    can_mutate: bool
    priority: int = 100


class HookBus:
    """进程内轻量事件总线；单个钩子异常只记录日志并跳过，不中断主链路。"""

    def __init__(self):
        self._hooks: dict[str, list[_HookSubscription]] = {}

    def register(
        self,
        event: str,
        handler: Callable[[dict[str, Any]], Any],
        *,
        source: str,
        can_mutate: bool = False,
        priority: int = 100,
    ) -> None:
        if event not in HOOK_EVENTS:
            raise ValueError(f"不支持的生命周期钩子事件: {event!r}")
        if not callable(handler):
            raise ValueError(f"钩子处理函数不可调用: {event!r}")
        entries = self._hooks.setdefault(event, [])
        entries.append(_HookSubscription(
            event=event,
            handler=handler,
            source=source,
            can_mutate=bool(can_mutate),
            priority=int(priority),
        ))
        entries.sort(key=lambda item: item.priority)

    def unregister_by_source(self, source: str) -> int:
        removed = 0
        for event, entries in list(self._hooks.items()):
            kept = [item for item in entries if item.source != source]
            removed += len(entries) - len(kept)
            if kept:
                self._hooks[event] = kept
            else:
                self._hooks.pop(event, None)
        return removed

    def has_hooks(self, event: str) -> bool:
        return bool(self._hooks.get(event))

    async def _invoke(self, sub: _HookSubscription, payload: dict[str, Any]) -> Any:
        try:
            res = sub.handler(payload)
            if inspect.isawaitable(res):
                res = await res
            return res
        except Exception:
            logger.exception("插件钩子执行异常 [event=%s, source=%s]", sub.event, sub.source)
            return None

    async def emit_observe(self, event: str, payload: dict[str, Any]) -> None:
        """触发只读观察类钩子（忽略任何返回值）。"""
        entries = list(self._hooks.get(event) or ())
        for sub in entries:
            await self._invoke(sub, deepcopy(payload))

    async def emit_before_round(self, payload: dict[str, Any]) -> list[str]:
        """收集 before_round 额外上下文片段（仅 hooks:mutate 可注入）。"""
        fragments: list[str] = []
        for sub in list(self._hooks.get("before_round") or ()):
            res = await self._invoke(sub, deepcopy(payload))
            if not isinstance(res, dict):
                continue
            extra = res.get("extra_context")
            if isinstance(extra, str) and extra.strip():
                if sub.can_mutate:
                    fragments.append(extra.strip())
                else:
                    logger.warning("插件钩子 %s 仅有 hooks:observe 权限，忽略 extra_context 注入", sub.source)
        return fragments

    async def emit_before_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """触发 before_heartbeat；允许 hooks:mutate 附加简报或跳过本次心跳。"""
        briefings: list[str] = []
        skip = False
        for sub in list(self._hooks.get("before_heartbeat") or ()):
            res = await self._invoke(sub, {**deepcopy(payload), "briefings": list(briefings)})
            if not isinstance(res, dict):
                continue
            if not sub.can_mutate:
                if res.get("skip") or res.get("briefing"):
                    logger.warning("插件钩子 %s 仅有 hooks:observe 权限，忽略 before_heartbeat 修改", sub.source)
                continue
            if res.get("skip") is True:
                skip = True
            briefing = res.get("briefing")
            if isinstance(briefing, str) and briefing.strip():
                briefings.append(briefing.strip())
        return {"skip": skip, "briefings": briefings}

    async def emit_before_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str,
        context: Any = None,
    ) -> tuple[bool, dict[str, Any], str | None]:
        """依次执行 before_tool_call；返回 `(allowed, effective_args, block_reason)`。"""
        current_args = deepcopy(arguments)
        for sub in list(self._hooks.get("before_tool_call") or ()):
            payload = {
                "tool_name": tool_name,
                "arguments": deepcopy(current_args),
                "tool_call_id": tool_call_id,
                "platform": getattr(context, "platform", None),
                "session_id": getattr(context, "session_id", None),
                "round_id": getattr(context, "round_id", None),
            }
            res = await self._invoke(sub, payload)
            if not isinstance(res, dict):
                continue
            if not sub.can_mutate:
                if res.get("block") or "arguments" in res:
                    logger.warning("插件钩子 %s 仅有 hooks:observe 权限，忽略工具调用拦截/改写", sub.source)
                continue
            if res.get("block") is True:
                reason = str(res.get("reason") or f"工具 {tool_name} 被插件阻止执行")
                return False, current_args, reason
            if isinstance(res.get("arguments"), dict):
                current_args = deepcopy(res["arguments"])
        return True, current_args, None

    async def emit_after_tool_result(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        tool_call_id: str,
        context: Any = None,
    ) -> Any:
        """依次执行 after_tool_result；允许 hooks:mutate 修改返回内容。"""
        current_result = result
        for sub in list(self._hooks.get("after_tool_result") or ()):
            payload = {
                "tool_name": tool_name,
                "arguments": deepcopy(arguments),
                "result": deepcopy(current_result),
                "tool_call_id": tool_call_id,
                "platform": getattr(context, "platform", None),
                "session_id": getattr(context, "session_id", None),
                "round_id": getattr(context, "round_id", None),
            }
            res = await self._invoke(sub, payload)
            if not isinstance(res, dict) or "result" not in res:
                continue
            if not sub.can_mutate:
                logger.warning("插件钩子 %s 仅有 hooks:observe 权限，忽略 after_tool_result 修改", sub.source)
                continue
            current_result = res["result"]
        return current_result


hook_bus = HookBus()
