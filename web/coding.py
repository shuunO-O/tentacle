"""Expose the local DSH Web Host through Tentacle's login and device gate."""

import asyncio
import json
from contextlib import suppress
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from subagents.dsh import DshError
from web.schemas import DshAccessResponse
from web.settings import TOKEN_TTL_SECONDS


_MOUNT = "/coding/"
_COOKIE = "tentacle_coding"
_MUX = "api/remote.mux"
_GUARDED_RPC = {
    "api/workspace/create", "api/workspace/initializeDefault", "api/session/create",
    "api/directoryPicker/list", "api/directoryPicker/createDirectory", "api/directoryPicker/pick",
}
_REQUEST_HEADERS = {"accept", "accept-language", "content-type", "content-length", "range", "if-range"}
_RESPONSE_HEADERS = {
    "content-type", "content-length", "content-encoding", "content-range", "content-disposition",
    "accept-ranges", "content-security-policy", "x-content-type-options", "cross-origin-resource-policy",
}


def _same_origin(connection):
    if connection.headers.get("sec-fetch-site") == "cross-site":
        return False
    value = connection.headers.get("origin")
    if not value:
        return True
    try:
        origin = urlsplit(value)
    except ValueError:
        return False
    return (origin.scheme in {"http", "https"} and origin.netloc
            and origin.netloc.lower() == connection.headers.get("host", "").lower())


async def _guarded_body(request, path, manager):
    data = bytearray()
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > 64 * 1024:
                raise HTTPException(413, "DSH 请求过大")
            data.extend(chunk)
        packet = json.loads(data)
        args = packet["payload"]["args"]
        if packet["method"] != path.removeprefix("api/") or not isinstance(args, dict):
            raise ValueError
        if path == "api/workspace/initializeDefault":
            return JSONResponse({"type": "server-response", "rpcId": packet["rpcId"], "result": {"ok": True}})
        if path == "api/directoryPicker/pick":
            raise HTTPException(403, "请使用浏览器目录选择器")
        if path == "api/directoryPicker/list" and not args.get("path"):
            args["path"] = str(manager.config.workspace_root)
        if path.startswith("api/directoryPicker/"):
            if args["path"] != str(manager.config.workspace_root):
                args["path"] = str(manager.resolve_workspace(args["path"]))
        else:
            body = args["request"]
            if path == "api/workspace/create":
                body["path"] = str(manager.resolve_workspace(body["path"]))
            elif "workspaceId" in body:
                info = await manager.runner.bridge({"workspaceId": body["workspaceId"]})
                manager.resolve_workspace(info["path"])
            else:
                body["cwd"] = str(manager.resolve_workspace(body.get("cwd")))
        return json.dumps(packet, ensure_ascii=False).encode()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, "DSH 工作区必须位于 workspace 下的现有目录") from None


def coding_router(manager, auth_dep, settings, device_ok):
    router = APIRouter()

    def available():
        return manager is not None and manager.config.enabled and manager.coding_available

    def authorized(connection):
        return (available() and _same_origin(connection) and device_ok(connection)
                and _COOKIE in connection.cookies
                and settings.valid_token(connection.cookies.get(_COOKIE)))

    @router.post(
        "/api/subagents/dsh/access",
        dependencies=[auth_dep],
        response_model=DshAccessResponse,
        response_model_exclude_unset=True,
    )
    async def authorize(request: Request, response: Response):
        if not available():
            raise HTTPException(404, "DSH 未启用")
        if not _same_origin(request):
            raise HTTPException(403, "DSH 入口需要同源请求")
        try:
            await manager.runner.client()
        except DshError as error:
            raise HTTPException(503, str(error)) from None
        token = request.headers.get("authorization", "").removeprefix("Bearer ") if settings.has_password else "-"
        response.set_cookie(_COOKIE, token, max_age=TOKEN_TTL_SECONDS, path=_MOUNT, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")
        response.headers["Cache-Control"] = "private, no-store"
        return {"url": _MOUNT}

    @router.get("/coding", include_in_schema=False)
    async def slash():
        return RedirectResponse(_MOUNT)

    @router.api_route("/coding/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def proxy(request: Request, path: str):
        if not authorized(request):
            raise HTTPException(401, "请从 Tentacle 的编程入口打开 DSH")
        if (path.startswith(("/", "api/tentacle-bridge")) or "\\" in path or "%" in path
                or any(part in {".", ".."} for part in path.split("/"))
                or path.split("/", 1)[0] in {"acp", "connect"} or "token" in request.query_params):
            raise HTTPException(404)
        if path.startswith("api/credentials/") and path != "api/credentials/describe":
            raise HTTPException(403, "请通过 Tentacle 服务环境配置 DSH 凭据")
        try:
            client = await manager.runner.client()
            headers = {key: value for key, value in request.headers.items() if key in _REQUEST_HEADERS}
            content = None
            if path in _GUARDED_RPC and request.method == "POST":
                content = await _guarded_body(request, path, manager)
                if isinstance(content, Response):
                    return content
                headers.pop("content-length", None)
            elif request.method not in {"GET", "HEAD"}:
                content = request.stream()
            if path == "api/file":
                manager.resolve_media_path(request.query_params.get("path"))
            url = client.base_url.copy_with(path="/" + path, query=request.scope["query_string"] or None)
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=content),
                                         stream=True)
        except (ValueError, PermissionError):
            raise HTTPException(403, "DSH 路径不在工作区") from None
        except (DshError, httpx.HTTPError):
            raise HTTPException(503, "DSH Web Host 暂不可用") from None

        headers = {key: value for key, value in upstream.headers.items() if key in _RESPONSE_HEADERS}
        headers["Cache-Control"] = "private, no-store"
        headers["Referrer-Policy"] = "no-referrer"
        if location := upstream.headers.get("location"):
            parsed = urlsplit(location)
            if not parsed.scheme and not parsed.netloc and not location.startswith("//"):
                headers["Location"] = _MOUNT + location.lstrip("/") if location.startswith("/") else location

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
        return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)

    @router.websocket("/coding/api/remote.mux")
    async def websocket(socket: WebSocket):
        if not authorized(socket):
            await socket.close(4401)
            return
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        revision = settings.auth_revision

        async def to_host(upstream):
            while authorized(socket) and revision == settings.auth_revision:
                try:
                    frame = await asyncio.wait_for(socket.receive(), timeout=2)
                except TimeoutError:
                    continue
                if frame["type"] == "websocket.disconnect":
                    return
                await upstream.send(frame["text"] if frame.get("text") is not None else frame["bytes"])

        async def from_host(upstream):
            async for frame in upstream:
                if not authorized(socket) or revision != settings.auth_revision:
                    return
                if isinstance(frame, str):
                    await socket.send_text(frame)
                else:
                    await socket.send_bytes(frame)

        code = 1011
        try:
            client = await manager.runner.client()
            cookie = client.build_request("GET", "/" + _MUX).headers["cookie"]
            url = str(client.base_url.copy_with(scheme="ws", path="/" + _MUX))
            async with connect(url, additional_headers={"Cookie": cookie}, proxy=None, max_size=32 * 1024 * 1024) as host:
                await socket.accept()
                workers = [asyncio.create_task(to_host(host)), asyncio.create_task(from_host(host))]
                try:
                    await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                    code = 1000 if authorized(socket) and revision == settings.auth_revision else 4401
                finally:
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
        except (DshError, OSError, TimeoutError, WebSocketException, WebSocketDisconnect):
            pass
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close(code)

    return router
