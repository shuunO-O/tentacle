"""Web 应用上下文与共享鉴权/运行时依赖注入。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, Response

from memory.concept_dedup import ConceptDedupManager
from web.settings import DEVICE_COOKIE, DEVICE_COOKIE_MAX_AGE, WebSettings

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    conn_manager: Any
    web_adapter: Any
    agent_session: Any
    heartbeat: Any
    session_manager: Any
    chronicle: Any
    settings: WebSettings
    goal_manager: Any
    configuration_service: Any = None
    subagent_manager: Any = None
    maintenance_controller: Any = None
    plugin_manager: Any = None
    restart_controller: Any = None
    backup_manager: Any = None
    event_hub: Any = None
    app: Any = None
    get_file_manager_fn: Callable[[], Any] | None = None
    new_ladybug_store_fn: Callable[[], Any] | None = None
    wait_for_fn: Callable[..., Awaitable[Any]] = asyncio.wait_for
    client_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    concept_dedup: ConceptDedupManager | None = None

    @property
    def db_path(self) -> str:
        return str(self.chronicle.db_path)

    def has_device_cookie(self, conn: Any) -> bool:
        """当前连接是否携带有效设备 cookie（无论门开关，做实名检查）。"""
        return self.settings.valid_device(conn.cookies.get(DEVICE_COOKIE)) is not None

    def device_ok(self, conn: Any) -> bool:
        """设备门与主机冻结任一生效，都要求有效设备 cookie。"""
        if not self.settings.requires_device:
            return True
        return self.has_device_cookie(conn)

    @staticmethod
    def set_device_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            DEVICE_COOKIE,
            token,
            max_age=DEVICE_COOKIE_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )

    async def authenticate(self, request: Request) -> None:
        """REST 鉴权依赖：设备门 + 密码两层独立，任一层开启即强制。"""
        if not self.device_ok(request):
            logger.warning(f"[auth] 拒绝 {request.method} {request.url.path}: 设备未绑定")
            raise HTTPException(status_code=401, detail="设备未绑定")
        if not self.settings.has_password:
            return
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not self.settings.valid_token(token):
            logger.warning(f"[auth] 拒绝 {request.method} {request.url.path}: 无有效 token")
            raise HTTPException(status_code=401, detail="未授权或登录已过期")
        logger.debug(f"[auth] 放行 {request.method} {request.url.path}")

    def auth_dependency(self):
        async def _check(request: Request) -> None:
            await self.authenticate(request)

        return Depends(_check)

    def dedup_manager(self) -> Any:
        existing = getattr(self.app.state, "concept_dedup", None) if self.app is not None else self.concept_dedup
        if existing is not None:
            return existing
        manager = ConceptDedupManager(Path(self.db_path).parent / "concept_dedup.db")
        if self.event_hub is not None:
            self.event_hub.attach_dedup(manager)
        self.concept_dedup = manager
        if self.app is not None:
            self.app.state.concept_dedup = manager
        return manager

    def retain_client_task(self, coro) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.client_tasks.add(task)
        task.add_done_callback(self.client_tasks.discard)
        # 即使 HTTP 请求连接先断开，也检索异常防止静默未处理。
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    def get_file_manager(self):
        assert self.get_file_manager_fn is not None
        return self.get_file_manager_fn()

    def new_ladybug_store(self):
        assert self.new_ladybug_store_fn is not None
        return self.new_ladybug_store_fn()
