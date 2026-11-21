#!/usr/bin/env python3
"""Serve working-tree frontend with synthetic HTTP/WS data, without importing the app."""

import argparse
import asyncio
from copy import deepcopy
from ipaddress import ip_address, ip_network
from pathlib import Path
import sys
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCENARIOS = {"default", "empty", "error", "login", "enroll"}
TOKEN = "preview-token"
SETTINGS = {"agent_name": "Tentacle · 开发预览", "show_reasoning": False,
            "split_messages": True, "has_password": False}


class PreviewAccess:
    """Apply the same peer allowlist to HTTP and WS; ignore forwarded headers."""

    def __init__(self, app, allowed):
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            peer = (scope.get("client") or ("", 0))[0]
            if peer not in self.allowed:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    await JSONResponse({"detail": "Preview peer not allowed"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def history(empty=False):
    if empty:
        return []
    return [
        {"id": 1, "round_id": "preview-1", "role": "user", "platform": "harmony", "created_time": 1792022399,
         "content": "这是一条来自 Harmony 的合成消息。", "images": []},
        {"id": 2, "round_id": "preview-2", "role": "user", "platform": "web", "created_time": 1792022400,
         "content": "这是一条合成测试消息，请展示分段和 Markdown。", "images": []},
        {"id": 3, "round_id": "preview-2", "role": "assistant", "platform": "web", "created_time": 1792022401,
         "content": "这是开发预览，所有数据都是合成的。\n\n**第二段**，用于检查独立气泡。\n\n```python\nprint('preview')\n```",
         "segments": ["这是开发预览，所有数据都是合成的。", "**第二段**，用于检查独立气泡。\n\n```python\nprint('preview')\n```"]},
    ]


def create_preview(*, scenario="default", allowed_clients=(), transport="websocket"):
    from runtime.timezone import timezone_name
    if scenario not in SCENARIOS:
        raise ValueError("Unknown preview scenario")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(PreviewAccess, allowed={"127.0.0.1", "::1", *allowed_clients})
    app.state.scenario = scenario
    app.state.settings = deepcopy(SETTINGS)
    app.state.sockets = set()
    app.state.lock = asyncio.Lock()
    app.state.events = []
    from web.chat_state import ChatState
    app.state.chat_state = ChatState()
    app.state.chat_rounds = {}
    app.state.chat_history = []

    def settings():
        return {**app.state.settings, "has_password": app.state.scenario == "login", "timezone": timezone_name()}

    def authenticated(conn):
        if app.state.scenario == "login":
            return (conn.headers.get("authorization") == f"Bearer {TOKEN}"
                    or conn.query_params.get("token") == TOKEN)
        if app.state.scenario == "enroll":
            return conn.cookies.get("preview_device") == "bound"
        return True

    if __package__:
        from .configuration_preview import attach_configuration
    else:
        from configuration_preview import attach_configuration
    attach_configuration(app, authenticated)
    if __package__:
        from .subagent_preview import attach_subagents
    else:
        from subagent_preview import attach_subagents
    attach_subagents(app, authenticated)
    if __package__:
        from .concept_dedup_preview import attach_concept_dedup
    else:
        from concept_dedup_preview import attach_concept_dedup
    attach_concept_dedup(app, authenticated)
    if __package__:
        from .plugins_preview import attach_plugins
    else:
        from plugins_preview import attach_plugins
    attach_plugins(app, authenticated)
    # A separate temporary usage DB exercises the real aggregation code with synthetic data.
    import tempfile
    from runtime.usage import UsageTracker

    app.state.usage_temporary = tempfile.TemporaryDirectory(prefix="tentacle-usage-preview-")
    usage = object.__new__(UsageTracker)
    usage._initialized = False
    usage.__init__(str(Path(app.state.usage_temporary.name) / "usage.db"))
    usage_config = {
        "config_version": 2,
        "providers": {"provider-a": {"label": "示例供应商 A"}, "provider-b": {"label": "示例供应商 B"}},
        "models": {"a-shared": {"label": "日常模型", "provider": "provider-a"}, "b-shared": {"label": "备用模型", "provider": "provider-b"}},
        "roles": {"chat": {"label": "主对话"}, "compress": {"label": "压缩"}},
    }
    if scenario != "empty":
        for days in range(7):
            for provider, model_id, role, tokens in [
                ("provider-a", "a-shared", "chat", 1000),
                ("provider-b", "b-shared", "chat", 2000),
                ("provider-a", "a-shared", "compress", 500),
                ("", "", "", 100),
            ]:
                usage._db_insert(time.time() - days * 86400, "synthetic", "shared", tokens, 100,
                                 tokens + 100, tokens // 2, 0, 0, provider, model_id, role)

    if scenario != "empty":
        usage._db_insert(time.time() - 60 * 86400, "synthetic", "shared", 9000, 100, 9100,
                         4500, 0, 0, "provider-a", "a-shared", "chat")

    @app.get("/api/usage")
    async def usage_stats(request: Request, granularity: str = "day", window: int | None = None,
                          start_date: str | None = None, end_date: str | None = None,
                          provider_id: str | None = None, model_id: str | None = None,
                          role: str | None = None):
        if not authenticated(request):
            raise HTTPException(401, "预览身份验证")
        try:
            return await usage.query_usage(granularity, window, start_date=start_date,
                                           end_date=end_date, provider_id=provider_id,
                                           model_id=model_id, role=role, config=usage_config)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Tentacle-Preview"] = "synthetic"
        return response

    spa_routes = (
        "/",
        "/chat",
        "/graph",
        "/config",
        "/plugins",
        "/subagents",
        "/dedup",
        "/admin",
        "/usage",
        "/status",
        "/terminal",
        "/settings",
    )

    async def index():
        index_file = ROOT / "web/static/index.html"
        if not index_file.is_file():
            raise HTTPException(503, "前端静态资源尚未构建，请先运行 npm --prefix frontend run build")
        if transport == "polling":
            from fastapi.responses import HTMLResponse

            html = index_file.read_text(encoding="utf-8")
            return HTMLResponse(html.replace("<head>", '<head><script src="/__preview__/websocket.js"></script>', 1))
        return FileResponse(index_file)

    for route in spa_routes:
        app.add_api_route(route, index, methods=["GET"], include_in_schema=False)

    @app.get("/plugin/{plugin_id}", include_in_schema=False)
    async def spa_plugin_ui(plugin_id: str):
        return await index()

    @app.get("/favicon.ico")
    async def favicon():
        icon = ROOT / "web/static/favicon.svg"
        if not icon.is_file():
            raise HTTPException(404, "favicon 尚未构建")
        return FileResponse(icon)

    @app.get("/__preview__/state")
    async def preview_state():
        return {"preview": True, "scenario": app.state.scenario, "synthetic": True, "transport": transport}

    @app.get("/__preview__/websocket.js")
    async def websocket_shim():
        from fastapi.responses import Response

        return Response((Path(__file__).parent / "preview-websocket.js").read_text(encoding="utf-8"),
                        media_type="text/javascript")

    @app.get("/__preview__/events")
    async def events(request: Request, since: int = -1):
        if not authenticated(request):
            raise HTTPException(401, "预览身份验证")
        if since < 0:
            return {"cursor": len(app.state.events), "events": [{"type": "hello", "data": settings()}]}
        return {"cursor": len(app.state.events), "events": app.state.events[since:]}

    @app.post("/__preview__/scenario")
    async def set_scenario(request: Request):
        body = await request.json()
        if body.get("scenario") not in SCENARIOS:
            raise HTTPException(400, "Unknown scenario")
        app.state.scenario = body["scenario"]
        return {"ok": True, "scenario": app.state.scenario}

    @app.get("/api/auth/state")
    async def auth_state(request: Request):
        return {"has_password": app.state.scenario == "login", "device_auth": app.state.scenario == "enroll",
                "device_ok": request.cookies.get("preview_device") == "bound", "freeze": False}

    @app.post("/api/auth/login")
    async def login(request: Request):
        if (await request.json()).get("password") != "preview":
            raise HTTPException(401, "预览密码为 preview")
        return {"token": TOKEN}

    @app.post("/api/auth/enroll")
    async def enroll(request: Request):
        if (await request.json()).get("code") != "preview":
            raise HTTPException(403, "预览绑定码为 preview")
        response = JSONResponse({"ok": True})
        response.set_cookie("preview_device", "bound", httponly=True, samesite="strict")
        return response

    async def broadcast(kind, data, round_meta=None):
        payload = {"type": kind, "data": data}
        if round_meta:
            payload["round"] = round_meta
        payload = app.state.chat_state.record(payload)
        app.state.events.append(payload)
        for socket in tuple(app.state.sockets):
            try:
                await socket.send_json(payload)
            except (WebSocketDisconnect, RuntimeError, OSError):
                app.state.sockets.discard(socket)

    @app.websocket("/ws/chat")
    async def ws_chat(socket: WebSocket):
        if not authenticated(socket):
            await socket.close(code=4401)
            return
        await socket.accept()
        app.state.sockets.add(socket)
        try:
            await socket.send_json({"type": "hello", "data": settings()})
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            app.state.sockets.discard(socket)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def api(path: str, request: Request):
        if not authenticated(request):
            raise HTTPException(401, "预览身份验证")
        method = request.method
        empty = app.state.scenario == "empty"
        if path == "settings" and method in {"GET", "PUT"}:
            if method == "PUT":
                body = await request.json()
                for key in ("show_reasoning", "split_messages"):
                    if key in body:
                        app.state.settings[key] = body[key]
                await broadcast("settings", settings())
            return settings()
        if path == "chat/history" and method == "GET":
            return {"messages": history(empty) + app.state.chat_history, "has_more": False}
        if path == "chat/sync" and method == "GET":
            rounds = [{k: v for k, v in r.items() if k != "stop_event"} for r in app.state.chat_rounds.values()]
            return {"messages": history(empty) + app.state.chat_history, "has_more": False,
                    **app.state.chat_state.snapshot(rounds)}
        if path == "chat/stop" and method == "POST":
            body = await request.json()
            if not body.get("round_id") and not body.get("request_id"):
                raise HTTPException(400, "停止请求必须指定回合")
            matches = [r for r in app.state.chat_rounds.values()
                       if (not body.get("round_id") or body["round_id"] == r["round_id"])
                       and (not body.get("request_id") or body["request_id"] == r["request_id"])]
            if len(matches) != 1:
                return {"accepted": False, "state": "ambiguous" if matches else "not_found"}
            r = matches[0]
            accepted = r["phase"] != "finishing"
            if accepted:
                r["stop_event"].set()
                r["stopping"] = True
            return {"accepted": accepted, "state": "stopping" if accepted else "finishing",
                    "round_id": r["round_id"], "request_id": r["request_id"]}
        if path == "chat/commands" and method == "GET":
            from agent.commands import command_catalog
            return {"commands": command_catalog()}
        if path == "chat/command" and method == "POST":
            return {"text": "开发预览：仅模拟命令响应，不执行实际命令。"}
        if path == "chat/send" and method == "POST":
            from agent.commands import parse_command
            body = await request.json()
            round_meta = {"round_id": uuid.uuid4().hex, "request_id": body.get("request_id"),
                          "source": "user", "platform": body.get("platform") or "web"}
            terminal = {**round_meta, "status": "completed", "stop_reason": "complete", "error": None}
            async def emit(kind, data):
                await broadcast(kind, data, round_meta)
            if parse_command(body.get("text", "")) is not None:
                command = {
                    "text": "开发预览：仅模拟命令响应，不执行实际命令。", "refresh_history": True,
                }
                terminal.update(stop_reason="command", command=command)
                await emit("round_finished", terminal)
                return {"ok": True, "total_tokens": 0, "command": command, "round": terminal}
            if app.state.scenario == "error":
                raise HTTPException(503, "合成测试：发送失败")
            control = {**round_meta, "phase": "queued", "stopping": False, "stop_event": asyncio.Event(),
                       "created_time": time.time(), "input": {"content": body.get("text", ""), "platform": "web"}}
            app.state.chat_rounds[round_meta["round_id"]] = control
            output = []
            try:
                async with app.state.lock:
                    control["phase"] = "running"
                    await emit("thinking_delta", {"content": "这是合成的思考事件。"})
                    for index, text in enumerate(("模拟流式回复的第一段。", "模拟流式回复的第二段。")):
                        if control["stopping"]:
                            break
                        if index:
                            output.append("\n\n")
                        for chunk in (text[:5], text[5:]):
                            if control["stopping"]:
                                break
                            output.append(chunk)
                            await emit("message_delta", {"text": chunk, "platform": "web"})
                            await asyncio.sleep(0.06)
                        if index == 0 and app.state.settings["split_messages"]:
                            await emit("message_seg_end", {})
                    if control["stopping"]:
                        terminal.update(status="cancelled", stop_reason="cancelled")
                    else:
                        await emit("usage", {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
                    control["phase"] = "finishing"
                    for role, content in (("user", body.get("text", "")), ("assistant", "".join(output))):
                        if content:
                            item = {"id": 1000 + len(app.state.chat_history), "role": role, "content": content,
                                    "round_id": round_meta["round_id"], "platform": "web", "created_time": time.time()}
                            if role == "assistant":
                                item.update(segments=content.split("\n\n"), interrupted=control["stopping"])
                            app.state.chat_history.append(item)
                    await emit("final", {})
                    await emit("round_finished", terminal)
                return {
                    "ok": terminal["status"] == "completed",
                    "total_tokens": 0 if control["stopping"] else 120,
                    "round": terminal,
                }
            finally:
                app.state.chat_rounds.pop(round_meta["round_id"], None)
        if method != "GET":
            # Never proxy unknown actions to production or silently claim success.
            raise HTTPException(501, f"预览尚未模拟此操作：{method} /api/{path}")
        if path == "status":
            if app.state.scenario == "error":
                raise HTTPException(503, "合成测试：状态读取失败")
            return {"time": time.time(), "connections": len(app.state.sockets),
                    "session": {"session_id": "synthetic-preview", "start_time": 1792022400},
                    "tokens": {"accumulated": 120}, "db": {"raw_messages": len(history(empty))},
                    "heartbeat": {}, "usage": {}}
        if path == "balance":
            selected = request.query_params.get("provider_id")
            items = [
                {"provider_id": "provider-a", "label": "示例供应商 A", "ok": True,
                 "is_available": True, "balances": [{"currency": "CNY", "total_balance": "128.50"}],
                 "fetched_at": time.time()},
                {"provider_id": "provider-b", "label": "示例供应商 B", "ok": False,
                 "error": "该供应商暂不支持余额查询"},
            ] if not empty else []
            if selected:
                items = [item for item in items if item["provider_id"] == selected] or [
                    {"provider_id": selected, "label": "未记录", "ok": False,
                     "error": "该供应商未配置，无法查询余额"}]
            return {"items": items, "synthetic": True}
        if path == "terminal/history":
            return {"ok": True, "entries": [] if empty else [{"ts": 1792022400, "content": "Synthetic terminal output\r\n"}]}
        if path == "files":
            return {"files": []}
        if path == "auth/devices":
            return {"device_auth": False, "device_bound": False, "allow_new_devices": True,
                    "devices": [], "freeze": None, "pending_code": None}
        if path == "admin/backups":
            return {"items": []}
        if path == "backups/workspace":
            cur = request.query_params.get("path", "").strip("/")
            return {
                "exists": True,
                "current_path": cur,
                "parent_path": None if not cur else "",
                "entries": [] if empty or cur else [
                    {"path": "reports", "name": "reports", "is_dir": True, "size": 0, "mtime": 1792022400},
                    {"path": "summary.md", "name": "summary.md", "is_dir": False, "size": 2048, "mtime": 1792022400},
                ],
                "truncated": False,
            }
        if path == "backups":
            return {
                "policy": {
                    "enabled": True,
                    "interval_hours": 24,
                    "retention_count": 7,
                    "include_files": True,
                    "last_auto_backup_at": 1792022400 if not empty else None,
                    "last_status": "succeeded" if not empty else None,
                    "last_error": None,
                    "last_backup_id": "20261115T030000Z-a1b2c3d4" if not empty else None,
                },
                "next_run_at": 1792108800,
                "running": False,
                "running_op": None,
                "backups": [] if empty else [
                    {
                        "backup_id": "20261115T030000Z-a1b2c3d4",
                        "kind": "auto",
                        "created_at": "2026-11-15T03:00:00+00:00",
                        "created_ts": 1792022400,
                        "data_epoch": 1,
                        "includes_files": True,
                        "includes_env": False,
                        "file_count": 12,
                        "payload_bytes": 245760,
                        "archive_bytes": 98304,
                        "archive_sha256": "0" * 64,
                        "required_env_vars": ["DEEPSEEK_API_KEY"],
                        "configured_env_vars": ["DEEPSEEK_API_KEY"],
                    }
                ],
                "total_bytes": 0 if empty else 98304,
                "data_epoch": 1,
                "required_env_vars": ["DEEPSEEK_API_KEY"],
                "missing_env_vars": [],
                "last_restore": None,
                "env_export_allowed": app.state.scenario in {"login", "enroll"},
                "restart_available": False,
                "instance_id": "preview-instance",
                "restarting": False,
            }
        if path == "memory/dedup":
            return {"pairs": [], "counts": {}, "run": None}
        if path == "memory/graph":
            nodes = [] if empty else [
                {"id": "preview-person", "labels": ["Person"], "properties": {"name": "测试角色"}},
                {"id": "preview-concept", "labels": ["Concept"], "properties": {"name": "前端验证"}},
            ]
            return {"ok": True, "data": {
                "nodes": nodes,
                "relationships": [] if empty else [
                    {"source": "preview-person", "target": "preview-concept", "type": "MENTIONS", "properties": {}}
                ],
            }}
        if path in {"admin/messages", "admin/sessions", "admin/summaries", "admin/goals", "admin/tasks"}:
            return {"columns": ["id", "content"], "rows": [] if empty else [[1, "合成测试记录"]],
                    "total": 0 if empty else 1}
        raise HTTPException(404, f"预览尚未模拟此接口：/api/{path}")

    app.mount("/static", StaticFiles(directory=ROOT / "web/static", follow_symlink=False, check_dir=False),
              name="static")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8421)
    parser.add_argument("--allow-client", action="append", default=[])
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="default")
    parser.add_argument("--transport", choices=["websocket", "polling"], default="websocket")
    args = parser.parse_args()
    address = ip_address(args.host)
    if not address.is_loopback:
        if address not in ip_network("100.64.0.0/10") or not args.allow_client:
            parser.error("Non-loopback preview requires a Tailscale IPv4 address and --allow-client")
    for peer in args.allow_client:
        ip_address(peer)
    import uvicorn

    uvicorn.run(create_preview(scenario=args.scenario, allowed_clients=[args.host, *args.allow_client],
                               transport=args.transport),
                host=args.host, port=args.port, proxy_headers=False, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
