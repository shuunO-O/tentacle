"""统一 WebSocket 实时通道（/ws/chat）。"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from web.deps import AppContext

logger = logging.getLogger(__name__)


def ws_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    settings = ctx.settings

    @router.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket, token: str = ""):
        revision = settings.auth_revision

        def authorized() -> bool:
            return (
                revision == settings.auth_revision
                and ctx.device_ok(websocket)
                and settings.valid_token(token)
            )

        if not authorized():
            logger.warning("[auth] WS 拒绝连接: 凭证无效")
            await websocket.close(code=4401)
            return
        await websocket.accept()
        if ctx.conn_manager is not None:
            await ctx.conn_manager.connect(websocket, authorized)
        if ctx.event_hub is not None:
            ctx.event_hub.start_status_loop()
        try:
            if not authorized():
                await websocket.close(code=4401)
                return
            await websocket.send_text(
                json.dumps({"type": "hello", "data": settings.get_public()}, ensure_ascii=False)
            )
            while True:
                # 空闲连接定期检查带外冻结/TTL；广播前另行复查避免窗口内泄露。
                if not authorized():
                    await websocket.close(code=4401)
                    break
                try:
                    await ctx.wait_for_fn(websocket.receive_text(), timeout=5)
                except TimeoutError:
                    continue
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.debug(f"WS 连接异常: {exc}")
        finally:
            if ctx.conn_manager is not None:
                await ctx.conn_manager.disconnect(websocket)

    return router
