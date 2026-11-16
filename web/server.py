"""
FastAPI 后端装配入口。

职责已按领域拆分至：
- web/schemas.py（Pydantic 请求/响应模型与 WS 信封）
- web/deps.py（AppContext 运行时依赖注入与鉴权检查）
- web/events.py（统一 WebSocket 实时事件总线）
- web/repositories/*（Chronicle SQLite / Ladybug / FileManager 仓储）
- web/routers/*（auth / chat / memory / files / admin / ws）
- web/configuration.py / web/subagents.py / web/maintenance.py / web/coding.py / web/plugins.py

create_app(...) 由 run_web.py 调用，传入共享的运行时实例。
"""

import asyncio
from contextlib import asynccontextmanager
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from attachments.manager import get_file_manager
from memory.ladybug_store import LadybugStore
from web.access import DeviceAccessGate
from web.coding import coding_router
from web.configuration import configuration_router
from web.deps import AppContext
from web.events import WebEventHub
from web.maintenance import maintenance_router
from web.plugins import plugins_router
from web.repositories.chronicle_repo import ChronicleRepository
from web.routers.admin import admin_router, build_status_snapshot
from web.routers.auth import auth_router
from web.routers.backups import backups_router
from web.routers.chat import chat_router
from web.routers.files import files_router
from web.routers.memory import memory_router
from web.routers.ws import ws_router
from web.settings import SettingsWriteError, WebSettings
from web.subagents import subagent_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    conn_manager,
    web_adapter,
    agent_session,
    heartbeat,
    session_manager,
    chronicle,
    settings: WebSettings,
    goal_manager,
    configuration_service=None,
    subagent_manager=None,
    maintenance_controller=None,
    plugin_manager=None,
    restart_controller=None,
    backup_manager=None,
) -> FastAPI:
    db_path = str(chronicle.db_path)
    repo = ChronicleRepository(db_path, goal_manager)

    # 通过闭包引用 web.server 模块属性，保持既有测试对 web.server.* monkeypatch 的兼容性。
    ctx = AppContext(
        conn_manager=conn_manager,
        web_adapter=web_adapter,
        agent_session=agent_session,
        heartbeat=heartbeat,
        session_manager=session_manager,
        chronicle=chronicle,
        settings=settings,
        goal_manager=goal_manager,
        configuration_service=configuration_service,
        subagent_manager=subagent_manager,
        maintenance_controller=maintenance_controller,
        plugin_manager=plugin_manager,
        restart_controller=restart_controller if restart_controller is not None else getattr(configuration_service, "restart", None),
        backup_manager=backup_manager,
        get_file_manager_fn=lambda: get_file_manager(),
        new_ladybug_store_fn=lambda: LadybugStore(),
        wait_for_fn=lambda aw, timeout: asyncio.wait_for(aw, timeout),
    )

    if plugin_manager is not None and hasattr(plugin_manager, "bind_web_runtime"):
        plugin_manager.bind_web_runtime(
            settings=settings,
            conn_manager=conn_manager,
            retain_task=ctx.retain_client_task,
        )

    event_hub = WebEventHub(
        conn_manager,
        status_provider=lambda: build_status_snapshot(ctx, repo),
        subagent_manager=subagent_manager,
        maintenance_controller=maintenance_controller,
        settings=settings,
    )
    event_hub.attach_subagents(subagent_manager)
    event_hub.attach_maintenance(maintenance_controller)
    ctx.event_hub = event_hub

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await event_hub.stop()
            tasks = list(ctx.client_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            dedup = getattr(_app.state, "concept_dedup", None)
            stop_dedup = getattr(dedup, "stop", None)
            if callable(stop_dedup):
                await stop_dedup()

    app = FastAPI(title="Tentacle WebUI", docs_url=None, redoc_url=None, lifespan=lifespan)
    ctx.app = app
    app.state.client_tasks = ctx.client_tasks
    app.state.concept_dedup = None
    app.state.event_hub = event_hub

    auth_dep = ctx.auth_dependency()
    app.include_router(configuration_router(configuration_service, auth_dep))
    app.include_router(plugins_router(plugin_manager, auth_dep, config_service=configuration_service))
    if maintenance_controller is not None:
        app.include_router(maintenance_router(maintenance_controller, auth_dep, settings, configuration_service))
    app.include_router(subagent_router(subagent_manager, auth_dep))
    app.include_router(coding_router(subagent_manager, auth_dep, settings, ctx.device_ok))
    app.include_router(auth_router(ctx, auth_dep))
    app.include_router(chat_router(ctx, auth_dep))
    app.include_router(memory_router(ctx, auth_dep))
    app.include_router(files_router(ctx, auth_dep))
    app.include_router(admin_router(ctx, repo, auth_dep))
    app.include_router(backups_router(ctx, auth_dep))
    app.include_router(ws_router(ctx))
    if plugin_manager is not None:
        for ext_plugin_id, ext_router in plugin_manager.get_extension_routers():
            app.include_router(
                ext_router,
                prefix=f"/api/plugins/ext/{ext_plugin_id}",
                dependencies=[auth_dep],
                tags=[f"plugin:{ext_plugin_id}"],
            )

    @app.exception_handler(SettingsWriteError)
    async def settings_write_failed(request: Request, error: SettingsWriteError):
        return JSONResponse(status_code=503, content={"detail": "设置保存失败，请稍后重试或检查主机存储"})

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """诊断用 HTTP 访问日志。"""
        start = time.time()
        response = await call_next(request)
        client = request.client.host if request.client else "?"
        logger.info(
            f"[http] {client} {request.method} {request.url.path} -> {response.status_code} "
            f"({int((time.time() - start) * 1000)}ms)"
        )
        return response

    # 位于路由/表单解析和静态托管之外，无公开端点例外。
    app.add_middleware(DeviceAccessGate, settings=settings)

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir), check_dir=False), name="static")

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

    async def spa_index():
        return FileResponse(str(static_dir / "index.html"))

    for route in spa_routes:
        app.add_api_route(route, spa_index, methods=["GET"], include_in_schema=False)

    @app.get("/favicon.ico")
    async def favicon():
        return FileResponse(str(static_dir / "favicon.svg"))

    return app
