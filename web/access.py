"""新设备入口的 ASGI 外层门禁，在路由、上传解析及静态文件读取之前校验。"""

import logging

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from web.settings import DEVICE_COOKIE, WebSettings

logger = logging.getLogger(__name__)


class DeviceAccessGate:
    def __init__(self, app, settings: WebSettings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        closed = not self.settings.allow_new_devices
        if closed:
            connection = HTTPConnection(scope)
            try:
                allowed = self.settings.valid_device(connection.cookies.get(DEVICE_COOKIE)) is not None
            except Exception:
                logger.exception("设备入口校验失败，拒绝请求")
                allowed = False
            if not allowed:
                logger.warning("[auth] 新设备入口已关闭，拒绝 %s %s", scope["type"], scope["path"])
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4401, "reason": "设备未授权"})
                else:
                    response = JSONResponse(
                        status_code=403, content={"detail": "新设备入口已关闭"},
                        headers={"Cache-Control": "private, no-store"},
                    )
                    await response(scope, receive, send)
                return

        async def send_with_cache_policy(message):
            if message["type"] == "http.response.start" and (closed or scope["path"].startswith("/api/")):
                MutableHeaders(scope=message)["Cache-Control"] = "private, no-store"
            await send(message)

        await self.app(scope, receive, send_with_cache_policy)
