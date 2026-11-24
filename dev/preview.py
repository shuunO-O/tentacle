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
SETTINGS = {"agent_name": "Tentacle", "show_reasoning": False,
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
    base = 1795401600  # 2026-11-24 10:40:00+08:00 (合成预览时间)
    return [
        {
            "id": 1, "round_id": "preview-1", "role": "user", "platform": "harmony", "created_time": base,
            "content": "周末想再去一趟莫干山，上次和周默走完那条环线后顺路去的那家手冲店叫什么来着？",
            "images": [],
        },
        {
            "id": 2, "round_id": "preview-1", "role": "assistant", "platform": "harmony", "created_time": base + 4,
            "content": (
                "从图谱记忆里翻到了上次十月中旬莫干山环线的记录：你们下山后在「拾光」旁边的手冲吧坐了一会儿，主理人是沈青，那天点了水洗耶加雪菲。\n\n"
                "另外你当时记了一条偏好：**以后轻徒步尽量带 28L 小包，不背三脚架**。这次周末需要我提前帮你查一下莫干山这周的降雨概率吗？"
            ),
            "segments": [
                "从图谱记忆里翻到了上次十月中旬莫干山环线的记录：你们下山后在「拾光」旁边的手冲吧坐了一会儿，主理人是沈青，那天点了水洗耶加雪菲。",
                "另外你当时记了一条偏好：**以后轻徒步尽量带 28L 小包，不背三脚架**。这次周末需要我提前帮你查一下莫干山这周的降雨概率吗？",
            ],
            "usage": {"prompt_tokens": 2480, "completion_tokens": 146, "total_tokens": 2626},
        },
        {
            "id": 3, "round_id": "preview-2", "role": "user", "platform": "web", "created_time": base + 900,
            "content": "顺便帮我整理一下把服务迁到新 1C2G VPS 的核心检查项，代码改动我晚点直接丢给 dsh 跑。",
            "images": [],
        },
        {
            "id": 4, "round_id": "preview-2", "role": "assistant", "platform": "web", "created_time": base + 906,
            "content": (
                "整理好了，按顺序走完大概十分钟：\n\n"
                "1. 执行 `sudo python3 scripts/install_linux.py --apply` 初始化隔离目录与 `systemd` 服务\n"
                "2. 配好 Nginx / Caddy HTTPS 反代（后端保持监听 `127.0.0.1:8420`），或直接走 Tailscale 私有组网\n"
                "3. 在 WebUI 开启登录密码与设备验证，主机侧跑 `tentacle device gen-code` 绑定常用设备\n"
                "4. 常用设备绑完后锁定入口，防止未授权扫描：\n\n"
                "```bash\n"
                "tentacle device deny-new\n"
                "tentacle status\n"
                "```\n\n"
                "等会儿代码重构的部分你直接在「编程子代理」面板拉起 `dsh` 会话就行，不占用主对话上下文。"
            ),
            "segments": [
                "整理好了，按顺序走完大概十分钟：\n\n"
                "1. 执行 `sudo python3 scripts/install_linux.py --apply` 初始化隔离目录与 `systemd` 服务\n"
                "2. 配好 Nginx / Caddy HTTPS 反代（后端保持监听 `127.0.0.1:8420`），或直接走 Tailscale 私有组网\n"
                "3. 在 WebUI 开启登录密码与设备验证，主机侧跑 `tentacle device gen-code` 绑定常用设备\n"
                "4. 常用设备绑完后锁定入口，防止未授权扫描：\n\n"
                "```bash\n"
                "tentacle device deny-new\n"
                "tentacle status\n"
                "```",
                "等会儿代码重构的部分你直接在「编程子代理」面板拉起 `dsh` 会话就行，不占用主对话上下文。",
            ],
            "usage": {"prompt_tokens": 3120, "completion_tokens": 238, "total_tokens": 3358},
        },
        {
            "id": 5, "round_id": "preview-3", "role": "user", "platform": "qq", "created_time": base + 3600,
            "content": "明早八点提醒我看一下莫干山天气，如果没雨就订车票。",
            "images": [],
        },
        {
            "id": 6, "round_id": "preview-3", "role": "assistant", "platform": "qq", "created_time": base + 3603,
            "content": "已经建好明早 `08:00` 的定时唤醒任务，并顺手写进今晚的会话交接近况里，明早到点直接发给你。",
            "segments": [
                "已经建好明早 `08:00` 的定时唤醒任务，并顺手写进今晚的会话交接近况里，明早到点直接发给你。",
            ],
            "usage": {"prompt_tokens": 3490, "completion_tokens": 62, "total_tokens": 3552},
        },
    ]


def _synthetic_graph():
    nodes = [
        # 人物
        {"id": "person:linlan", "labels": ["Person"],
         "properties": {"name": "林岚", "role": "主角", "summary": "独立开发者，偏好轻量自托管工具、手冲咖啡与周末山野徒步。"}},
        {"id": "person:zhoumo", "labels": ["Person"],
         "properties": {"name": "周默", "role": "徒步搭子", "summary": "经常一起走江浙周边轻徒步线路，擅长规划轨迹与天气研判。"}},
        {"id": "person:shenqing", "labels": ["Person"],
         "properties": {"name": "沈青", "role": "咖啡师", "summary": "拾光手冲吧主理人，擅长浅烘埃塞豆冲煮。"}},
        {"id": "person:chenxu", "labels": ["Person"],
         "properties": {"name": "陈序", "role": "开源协作者", "summary": "共同讨论 Tentacle 插件权限沙盒与前端架构。"}},
        # 地点
        {"id": "loc:moganshan", "labels": ["Location"],
         "properties": {"name": "莫干山步道", "region": "浙江湖州", "summary": "竹林环线约 12 公里，秋季体感舒适。"}},
        {"id": "loc:longjing", "labels": ["Location"],
         "properties": {"name": "西湖龙井村", "region": "浙江杭州", "summary": "雨后茶山视野开阔，适合半日散步。"}},
        {"id": "loc:pingjiang", "labels": ["Location"],
         "properties": {"name": "苏州平江路", "region": "江苏苏州", "summary": "临河老街与独立书店聚集地。"}},
        {"id": "loc:shiguang", "labels": ["Location"],
         "properties": {"name": "拾光手冲吧", "region": "杭州", "summary": "安静的社区咖啡馆，常备水洗耶加雪菲。"}},
        {"id": "loc:studio", "labels": ["Location"],
         "properties": {"name": "居家书房", "region": "杭州", "summary": "日常开发、部署与夜间阅读工作台。"}},
        # 事件（覆盖 milestone / travel / study / interaction 四种类型）
        {"id": "evt:vps-live", "labels": ["Event"],
         "properties": {"name": "单进程服务正式上线", "type": "milestone", "date": "2026-09-18",
                        "summary": "将图谱记忆从外置 Neo4j 迁入内嵌 LadybugDB，主进程常驻稳定在 300 MiB 左右。",
                        "source_session": "session-20260918"}},
        {"id": "evt:moganshan-hike", "labels": ["Event"],
         "properties": {"name": "莫干山秋季环线徒步", "type": "travel", "date": "2026-10-19",
                        "summary": "与周默完成 12 公里竹林环线，下山后确认轻装 28L 背包体验最佳。",
                        "source_session": "session-20261019"}},
        {"id": "evt:suzhou-weekend", "labels": ["Event"],
         "properties": {"name": "苏州平江路周末短途", "type": "travel", "date": "2026-10-04",
                        "summary": "带胶片机扫街并逛了三家旧书店，傍晚在河边喝茶记录笔记。",
                        "source_session": "session-20261004"}},
        {"id": "evt:longjing-walk", "labels": ["Event"],
         "properties": {"name": "雨后龙井茶山散步", "type": "travel", "date": "2026-11-02",
                        "summary": "午后放晴时独自走过十里琅珰南段，用鸿蒙端语音记下几条灵感。",
                        "source_session": "session-20261102"}},
        {"id": "evt:memory-bench", "labels": ["Event"],
         "properties": {"name": "内嵌图谱检索与内存压测", "type": "study", "date": "2026-10-27",
                        "summary": "对比每日会话交接与 500k 长上下文成本，验证双路检索召回效果。",
                        "source_session": "session-20261027"}},
        {"id": "evt:systems-notes", "labels": ["Event"],
         "properties": {"name": "Linux systemd 安全加固笔记", "type": "study", "date": "2026-11-08",
                        "summary": "整理非 root 运行、只读源码与运行时目录隔离以及回退校验流程。",
                        "source_session": "session-20261108"}},
        {"id": "evt:coffee-tasting", "labels": ["Event"],
         "properties": {"name": "手冲耶加雪菲萃取交流", "type": "interaction", "date": "2026-10-20",
                        "summary": "在拾光与沈青试了 91℃ 三段注水方案，柑橘与白花风味更干净。",
                        "source_session": "session-20261020"}},
        {"id": "evt:plugin-review", "labels": ["Event"],
         "properties": {"name": "插件权限沙盒评审", "type": "interaction", "date": "2026-11-12",
                        "summary": "与陈序梳理 PluginContext 最小权限门控和扩展路由继承鉴权细节。",
                        "source_session": "session-20261112"}},
        # 概念（含一组等价概念，展示图谱等价组与去重联动）
        {"id": "concept:pourover", "labels": ["Concept"], "equivalence_group": "eq-pourover",
         "properties": {"name": "手冲咖啡", "equivalence_group": "eq-pourover",
                        "summary": "偏好浅中烘水洗豆，常用 V60 与三段式注水。"}},
        {"id": "concept:pourover-en", "labels": ["Concept"], "equivalence_group": "eq-pourover",
         "properties": {"name": "Pour-over Coffee", "equivalence_group": "eq-pourover",
                        "summary": "手冲咖啡英文表述，与「手冲咖啡」属于同一等价概念组。"}},
        {"id": "concept:yirgacheffe", "labels": ["Concept"],
         "properties": {"name": "耶加雪菲", "summary": "埃塞俄比亚产区，偏爱水洗处理的花香与柠檬酸质。"}},
        {"id": "concept:selfhost", "labels": ["Concept"],
         "properties": {"name": "轻量自托管", "summary": "偏好单进程、低内存、零外置数据库与可回退升级的个人服务。"}},
        {"id": "concept:graph-memory", "labels": ["Concept"],
         "properties": {"name": "内嵌图谱记忆", "summary": "基于 LadybugDB 持久化实体关系，支持向量与原文双路溯源。"}},
        {"id": "concept:daily-handoff", "labels": ["Concept"],
         "properties": {"name": "每日会话交接", "summary": "在固定时间窗口整理近况并开启新会话，控制上下文开销。"}},
        {"id": "concept:heavy-stack", "labels": ["Concept"],
         "properties": {"name": "重型多容器架构", "summary": "依赖多套外置数据库与编排组件，在低配 VPS 上维护负担偏重。"}},
        {"id": "concept:hiking", "labels": ["Concept"],
         "properties": {"name": "轻量徒步", "summary": "单日 10–15 公里山野环线，控制负重在 28L 背包以内。"}},
        {"id": "concept:film", "labels": ["Concept"],
         "properties": {"name": "胶片摄影", "summary": "周末出行偶尔携带便携 35mm 相机记录街景。"}},
    ]
    relationships = [
        # 人物参与事件
        {"source": "person:linlan", "target": "evt:vps-live", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:linlan", "target": "evt:moganshan-hike", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:zhoumo", "target": "evt:moganshan-hike", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:linlan", "target": "evt:suzhou-weekend", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:linlan", "target": "evt:longjing-walk", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:linlan", "target": "evt:memory-bench", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:linlan", "target": "evt:systems-notes", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:linlan", "target": "evt:coffee-tasting", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:shenqing", "target": "evt:coffee-tasting", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:linlan", "target": "evt:plugin-review", "type": "PARTICIPATED_IN", "properties": {}},
        {"source": "person:chenxu", "target": "evt:plugin-review", "type": "PARTICIPATED_IN", "properties": {}},
        # 事件发生地点
        {"source": "evt:moganshan-hike", "target": "loc:moganshan", "type": "OCCURRED_AT", "properties": {}},
        {"source": "evt:suzhou-weekend", "target": "loc:pingjiang", "type": "OCCURRED_AT", "properties": {}},
        {"source": "evt:longjing-walk", "target": "loc:longjing", "type": "OCCURRED_AT", "properties": {}},
        {"source": "evt:coffee-tasting", "target": "loc:shiguang", "type": "OCCURRED_AT", "properties": {}},
        {"source": "evt:vps-live", "target": "loc:studio", "type": "OCCURRED_AT", "properties": {}},
        {"source": "evt:memory-bench", "target": "loc:studio", "type": "OCCURRED_AT", "properties": {}},
        {"source": "evt:systems-notes", "target": "loc:studio", "type": "OCCURRED_AT", "properties": {}},
        {"source": "evt:plugin-review", "target": "loc:studio", "type": "OCCURRED_AT", "properties": {}},
        # 事件提及概念
        {"source": "evt:vps-live", "target": "concept:selfhost", "type": "MENTIONS", "properties": {}},
        {"source": "evt:vps-live", "target": "concept:graph-memory", "type": "MENTIONS", "properties": {}},
        {"source": "evt:vps-live", "target": "concept:heavy-stack", "type": "MENTIONS", "properties": {}},
        {"source": "evt:memory-bench", "target": "concept:graph-memory", "type": "MENTIONS", "properties": {}},
        {"source": "evt:memory-bench", "target": "concept:daily-handoff", "type": "MENTIONS", "properties": {}},
        {"source": "evt:systems-notes", "target": "concept:selfhost", "type": "MENTIONS", "properties": {}},
        {"source": "evt:plugin-review", "target": "concept:selfhost", "type": "MENTIONS", "properties": {}},
        {"source": "evt:moganshan-hike", "target": "concept:hiking", "type": "MENTIONS", "properties": {}},
        {"source": "evt:longjing-walk", "target": "concept:hiking", "type": "MENTIONS", "properties": {}},
        {"source": "evt:suzhou-weekend", "target": "concept:film", "type": "MENTIONS", "properties": {}},
        {"source": "evt:coffee-tasting", "target": "concept:pourover", "type": "MENTIONS", "properties": {}},
        {"source": "evt:coffee-tasting", "target": "concept:pourover-en", "type": "MENTIONS", "properties": {}},
        {"source": "evt:coffee-tasting", "target": "concept:yirgacheffe", "type": "MENTIONS", "properties": {}},
        # 人物态度（展示情感倾向标签）
        {"source": "person:linlan", "target": "concept:selfhost", "type": "HAS_ATTITUDE", "properties": {"sentiment": "喜欢"}},
        {"source": "person:linlan", "target": "concept:pourover", "type": "HAS_ATTITUDE", "properties": {"sentiment": "喜欢"}},
        {"source": "person:linlan", "target": "concept:yirgacheffe", "type": "HAS_ATTITUDE", "properties": {"sentiment": "喜欢"}},
        {"source": "person:linlan", "target": "concept:hiking", "type": "HAS_ATTITUDE", "properties": {"sentiment": "喜欢"}},
        {"source": "person:linlan", "target": "concept:film", "type": "HAS_ATTITUDE", "properties": {"sentiment": "中立"}},
        {"source": "person:linlan", "target": "concept:heavy-stack", "type": "HAS_ATTITUDE", "properties": {"sentiment": "讨厌"}},
        {"source": "person:zhoumo", "target": "concept:hiking", "type": "HAS_ATTITUDE", "properties": {"sentiment": "喜欢"}},
        {"source": "person:shenqing", "target": "concept:yirgacheffe", "type": "HAS_ATTITUDE", "properties": {"sentiment": "喜欢"}},
    ]
    return {"nodes": nodes, "relationships": relationships}


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
        "providers": {"provider-a": {"label": "DeepSeek"}, "provider-b": {"label": "Gemini"}},
        "models": {"a-shared": {"label": "日常主对话", "provider": "provider-a"}, "b-shared": {"label": "多模态 / 备用", "provider": "provider-b"}},
        "roles": {"chat": {"label": "主对话"}, "compress": {"label": "会话交接与压缩"}, "memory": {"label": "图谱记忆提取"}},
    }
    if scenario != "empty":
        daily_scale = [1.15, 0.88, 1.32, 0.96, 1.48, 1.12, 0.76, 1.24, 1.05, 1.41, 0.92, 1.18, 0.85, 1.09]
        for days, factor in enumerate(daily_scale):
            for provider, model_id, role, base_prompt, base_comp in [
                ("provider-a", "a-shared", "chat", 14800, 2100),
                ("provider-b", "b-shared", "chat", 4200, 680),
                ("provider-a", "a-shared", "compress", 3100, 520),
                ("provider-a", "a-shared", "memory", 2400, 390),
            ]:
                pt = int(base_prompt * factor)
                ct = int(base_comp * factor)
                cached = int(pt * 0.62)
                usage._db_insert(time.time() - days * 86400, "synthetic", model_id, pt, ct,
                                 pt + ct, cached, 0, 0, provider, model_id, role)

    if scenario != "empty":
        usage._db_insert(time.time() - 60 * 86400, "synthetic", "a-shared", 18000, 2400, 20400,
                         11200, 0, 0, "provider-a", "a-shared", "chat")

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
        "/browser",
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
            now = time.time()
            return {
                "agent_name": app.state.settings["agent_name"],
                "time": now,
                "connections": max(1, len(app.state.sockets)),
                "session": {
                    "session_id": "session-20261124-01",
                    "start_time": now - 14400,
                    "turn_count": 0 if empty else 12,
                },
                "tokens": {"accumulated": 0 if empty else 18420},
                "db": {
                    "raw_messages": 0 if empty else 1284,
                    "sessions": 0 if empty else 42,
                    "summaries": 0 if empty else 38,
                    "goals": 0 if empty else 3,
                    "tasks": 0 if empty else 7,
                },
                "heartbeat": {
                    "enabled": True,
                    "interval_seconds": 1800,
                    "next_beat_at": now + 1140,
                },
                "usage": {
                    "today_requests": 0 if empty else 19,
                    "today_total_tokens": 0 if empty else 24680,
                },
            }
        if path == "maintenance":
            now = time.time()
            return {
                "available": True,
                "can_update": False,
                "web_allowed": True,
                "version": {"version": "v2.2.3", "commit": "a1b2c3d", "built_at": "2026-11-20T08:00:00Z"},
                "check": {
                    "ok": True,
                    "available": False,
                    "current": "v2.2.3",
                    "latest": "v2.2.3",
                    "message": "当前已是最新正式版本",
                    "data_compatible": True,
                    "checked_at": now - 1800,
                },
                "operation": None,
            }
        if path == "balance":
            selected = request.query_params.get("provider_id")
            items = [
                {"provider_id": "provider-a", "label": "DeepSeek", "ok": True,
                 "is_available": True, "balances": [{"currency": "CNY", "total_balance": "128.50"}],
                 "fetched_at": time.time()},
                {"provider_id": "provider-b", "label": "Gemini", "ok": False,
                 "error": "该供应商暂不支持余额查询"},
            ] if not empty else []
            if selected:
                items = [item for item in items if item["provider_id"] == selected] or [
                    {"provider_id": selected, "label": "未记录", "ok": False,
                     "error": "该供应商未配置，无法查询余额"}]
            return {"items": items, "synthetic": True}
        if path == "terminal/history":
            return {"ok": True, "entries": [] if empty else [
                {"ts": 1795401600, "content": "$ tentacle status\r\n● tentacle.service - Tentacle Personal AI Agent\r\n   Active: active (running) since Tue 2026-11-24 06:00:12 CST\r\n   Memory: 312.4M (max: 1.5G)\r\n\r\n$ tentacle device status\r\n设备验证开关: 开启\r\n新设备入口: 关闭（仅有效已绑定设备可访问）\r\n已绑定设备: 2 台\r\n"},
            ]}
        if path == "files":
            return {"files": []}
        if path == "auth/devices":
            now = time.time()
            return {
                "device_auth": not empty,
                "device_bound": not empty,
                "allow_new_devices": empty,
                "devices": [] if empty else [
                    {"id": "dev-macbook-01", "name": "MacBook Pro · Chrome", "created": now - 86400 * 14, "last_seen": now - 45},
                    {"id": "dev-harmony-02", "name": "Mate 70 · HarmonyOS", "created": now - 86400 * 9, "last_seen": now - 1200},
                ],
                "freeze": None,
                "pending_code": None,
            }
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
            return {"ok": True, "data": {"nodes": [], "relationships": []} if empty else _synthetic_graph()}
        if path == "admin/messages":
            rows = [] if empty else [
                [1284, "session-20261124-01", "assistant", "qq", "已经建好明早 08:00 的定时唤醒任务，并顺手写进今晚的会话交接近况里。"],
                [1283, "session-20261124-01", "user", "qq", "明早八点提醒我看一下莫干山天气，如果没雨就订车票。"],
                [1282, "session-20261124-01", "assistant", "web", "整理好了把服务迁到新 1C2G VPS 的核心检查项……"],
                [1281, "session-20261124-01", "user", "web", "顺便帮我整理一下把服务迁到新 1C2G VPS 的核心检查项。"],
                [1280, "session-20261124-01", "assistant", "harmony", "从图谱记忆里翻到了上次十月中旬莫干山环线的记录……"],
            ]
            return {"columns": ["id", "session_id", "role", "platform", "content"], "rows": rows, "total": len(rows)}
        if path == "admin/sessions":
            rows = [] if empty else [
                ["session-20261124-01", "2026-11-24 06:00", "active", 12, 18420],
                ["session-20261123-01", "2026-11-23 06:00", "handed_off", 26, 31240],
                ["session-20261122-01", "2026-11-22 06:00", "handed_off", 19, 24180],
            ]
            return {"columns": ["session_id", "started_at", "status", "turns", "tokens"], "rows": rows, "total": len(rows)}
        if path == "admin/summaries":
            rows = [] if empty else [
                [38, "2026-11-23", "确认新 VPS 迁移清单与设备鉴权 CLI 方案；规划周末莫干山轻徒步。"],
                [37, "2026-11-22", "完成插件权限沙盒评审并整理每日会话交接近况。"],
            ]
            return {"columns": ["id", "date", "summary"], "rows": rows, "total": len(rows)}
        if path == "admin/goals":
            rows = [] if empty else [
                [1, "完善 Tentacle 文档与开源展示", "active", "2026-11-30"],
                [2, "周末莫干山秋季环线复走", "active", "2026-11-29"],
                [3, "完成低配 VPS 安全迁移与回退演练", "completed", "2026-11-18"],
            ]
            return {"columns": ["id", "title", "status", "target_date"], "rows": rows, "total": len(rows)}
        if path == "admin/tasks":
            rows = [] if empty else [
                [1, 1, "补充脱敏界面截图并更新 README", "in_progress"],
                [2, 1, "补齐 tentacle device 命令行入口", "completed"],
                [3, 2, "周六早八点查看莫干山天气并决定车票", "pending"],
            ]
            return {"columns": ["id", "goal_id", "title", "status"], "rows": rows, "total": len(rows)}
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
