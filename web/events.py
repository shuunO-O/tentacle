"""统一 WebSocket 事件推送中心（将轮询类域事件汇聚到 /ws/chat）。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)


class WebEventHub:
    """将 Subagents / ConceptDedup / Maintenance / Status 状态推送给已鉴权客户端。"""

    def __init__(
        self,
        conn_manager: Any,
        *,
        status_provider: Callable[[], dict[str, Any]] | None = None,
        subagent_manager: Any = None,
        maintenance_controller: Any = None,
        settings: Any = None,
    ):
        self._conn = conn_manager
        self._status_provider = status_provider
        self._subagents = subagent_manager
        self._maintenance = maintenance_controller
        self._settings = settings
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._status_task: asyncio.Task[None] | None = None
        self._attached_ids: set[int] = set()
        self._closed = False

    def _track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return task

    def _has_listeners(self) -> bool:
        return bool(
            not self._closed
            and self._conn is not None
            and hasattr(self._conn, "broadcast")
            and getattr(self._conn, "count", lambda: 1)() > 0
        )

    async def broadcast(self, event_type: str, data: Any) -> None:
        if self._closed or self._conn is None or not hasattr(self._conn, "broadcast"):
            return
        if not self._has_listeners():
            return
        payload = {"type": event_type, "data": data, "ts": time.time()}
        try:
            await self._conn.broadcast(payload)
        except Exception:
            logger.debug("WS 事件广播失败 type=%s", event_type, exc_info=True)

    def _schedule_debounced(self, key: str, delay: float, factory: Callable[[], Awaitable[None]]) -> None:
        if self._closed or not self._has_listeners():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if key in self._pending and not self._pending[key].done():
            return

        async def run() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                if self._has_listeners():
                    await factory()
            finally:
                if self._pending.get(key) is asyncio.current_task():
                    self._pending.pop(key, None)

        task = loop.create_task(run())
        self._pending[key] = self._track(task)

    def _schedule_now(self, factory: Callable[[], Awaitable[None]]) -> None:
        if self._closed or not self._has_listeners():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._track(loop.create_task(factory()))

    # ---------- 订阅各领域模块 ----------

    def attach_subagents(self, manager: Any) -> None:
        if manager is None or id(manager) in self._attached_ids or not hasattr(manager, "add_observer"):
            return
        self._subagents = manager
        self._attached_ids.add(id(manager))
        manager.add_observer(self._on_subagent_event)

    def attach_maintenance(self, controller: Any) -> None:
        if controller is None or id(controller) in self._attached_ids or not hasattr(controller, "add_observer"):
            return
        self._maintenance = controller
        self._attached_ids.add(id(controller))
        controller.add_observer(self._on_maintenance_event)

    def attach_dedup(self, manager: Any) -> None:
        if manager is None or id(manager) in self._attached_ids or not hasattr(manager, "add_observer"):
            return
        self._attached_ids.add(id(manager))
        manager.add_observer(self._on_dedup_event)

    def _on_subagent_event(self, event: str, payload: dict[str, Any]) -> None:
        async def emit() -> None:
            data: dict[str, Any] = {"event": event, **payload}
            if self._subagents is not None and hasattr(self._subagents, "presentation"):
                try:
                    summary = await self._subagents.presentation(limit=20)
                    data.update(summary)
                except Exception:
                    logger.debug("生成 subagent.updated 快照失败", exc_info=True)
            await self.broadcast("subagent.updated", data)

        self._schedule_debounced("subagent.updated", 0.08, emit)

    def _on_maintenance_event(self, snapshot: dict[str, Any]) -> None:
        allowed = bool(self._settings and (self._settings.has_password or self._settings.requires_device))
        data = {**snapshot, "web_allowed": allowed}
        self._schedule_now(lambda: self.broadcast("maintenance.updated", data))

    def _on_dedup_event(self, event: str, payload: dict[str, Any]) -> None:
        if event in {"progress", "scan_progress"}:
            self._schedule_debounced(
                "dedup.progress",
                0.15,
                lambda: self.broadcast("dedup.progress", {"event": event, **payload}),
            )
            return
        # 终态或人工审核结果变更立刻取消防抖并实时推送。
        pending = self._pending.pop("dedup.progress", None)
        if pending is not None and not pending.done():
            pending.cancel()
        self._schedule_now(lambda: self.broadcast("dedup.progress", {"event": event, **payload}))

    def start_status_loop(self, interval_seconds: float = 5.0) -> None:
        if self._closed or self._status_provider is None or self._status_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def poll() -> None:
            last_signature: tuple[Any, ...] | None = None
            while not self._closed:
                await asyncio.sleep(interval_seconds)
                if not self._has_listeners() or self._status_provider is None:
                    continue
                try:
                    snapshot = self._status_provider()
                    sig = (
                        (snapshot.get("session") or {}).get("session_id"),
                        (snapshot.get("tokens") or {}).get("accumulated"),
                        (snapshot.get("usage") or {}).get("total"),
                        (snapshot.get("heartbeat") or {}).get("next_beat_at"),
                        snapshot.get("connections"),
                    )
                    if sig != last_signature:
                        last_signature = sig
                        await self.broadcast("status.snapshot", snapshot)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("WS status.snapshot 采集失败", exc_info=True)

        self._status_task = loop.create_task(poll())

    async def stop(self) -> None:
        self._closed = True
        tasks: list[asyncio.Task[Any]] = []
        if self._status_task is not None:
            self._status_task.cancel()
            tasks.append(self._status_task)
            self._status_task = None
        for task in list(self._pending.values()) + list(self._tasks):
            if not task.done():
                task.cancel()
            tasks.append(task)
        self._pending.clear()
        if tasks:
            with suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
