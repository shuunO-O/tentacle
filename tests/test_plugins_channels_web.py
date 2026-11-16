"""Phase 4 动态消息通道、后台任务与受 DeviceAccessGate 保护的扩展路由测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.commands import CommandRegistry
from agent.events import EventFanout
from agent.loop import resolve_model_tools
from agent.protocol import Event, EventType, ModelConfig
from plugins.context import PluginContext
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from plugins.manifest import parse_manifest
from plugins.permissions import PermissionDeniedError
from runtime.tool_registry import ToolRegistry
from tools import send_message as send_message_tool
from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


def _fresh_registry() -> ToolRegistry:
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    return reg


def _write_plugin(root: Path, plugin_id: str, manifest_yaml: str, code: str) -> Path:
    pdir = root / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
    (pdir / "plugin.py").write_text(code, encoding="utf-8")
    return pdir


async def test_channel_plugin_dynamic_send_message_fanout_and_inbound(tmp_path):
    reg = _fresh_registry()
    send_message_tool.register_tools(reg)
    fanout_received: list[str] = []
    async def base_web_cb(event):
        fanout_received.append(f"web:{event.type.value}")

    fanout = EventFanout(base_web_cb)
    inbound_calls: list[tuple[str, str, str | None]] = []

    class FakeAgentSession:
        async def run_round(self, user_input, *, platform="web", user_id=None, images=None):
            inbound_calls.append((user_input, platform, user_id))
            return SimpleNamespace(final_answer="routed", stop_reason="complete")

    plugins_dir = tmp_path / "plugins"
    _write_plugin(
        plugins_dir,
        "tg",
        "id: tg\nname: Telegram\nversion: '1.0'\nentrypoint: plugin.py:setup\npermissions:\n  - channels:register\n",
        (
            "sent_log = []\n"
            "events_log = []\n"
            "async def _send(text, target=None):\n"
            "    sent_log.append((text, target))\n"
            "    return {'ok': True, 'msg_id': len(sent_log)}\n"
            "async def _on_event(event):\n"
            "    events_log.append((event.type.value, event.data))\n"
            "async def setup(ctx):\n"
            "    ctx.register_channel(\n"
            "        'telegram',\n"
            "        sender=_send,\n"
            "        event_callback=_on_event,\n"
            "        description='Telegram Bot',\n"
            "        supports_target=True,\n"
            "        target_description='chat_id 或 @username',\n"
            "    )\n"
            "    await ctx.emit_user_message('from tg', platform='telegram', user_id='tg-42')\n"
        ),
    )

    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
        services={"agent_session": FakeAgentSession(), "event_fanout": fanout},
    )
    try:
        states = await pm.startup({"plugins": {"items": {"tg": {"enabled": True, "trusted": True}}}})
        assert states["tg"].status == "active"
        assert states["tg"].channels == ["telegram"]
        assert "telegram" in send_message_tool.SUPPORTED_PLATFORMS
        schema_enum = reg._tools["send_message"]["parameters"]["properties"]["platform"]["enum"]
        assert schema_enum == ["telegram"]
        assert inbound_calls == [("from tg", "telegram", "tg-42")]

        raw_out = await reg.execute("send_message", {"platform": "telegram", "text": "hello tg", "target": "chat-9"})
        assert raw_out.startswith("已发送\n")
        res = json.loads(raw_out.split("\n", 1)[1])
        assert res["ok"] is True
        assert pm._active_runtimes["tg"]["module"].sent_log == [("hello tg", "chat-9")]

        await fanout(Event(EventType.FINAL_ANSWER, {"content": "done"}))
        assert "web:final_answer" in fanout_received
        assert len(pm._active_runtimes["tg"]["module"].events_log) == 1
    finally:
        await pm.shutdown()

    assert send_message_tool.SUPPORTED_PLATFORMS == set()
    assert "enum" not in reg._tools["send_message"]["parameters"]["properties"]["platform"]


async def test_channel_hides_send_message_when_empty_and_rejects_unsupported_target(tmp_path, monkeypatch):
    from runtime import tool_registry as tr_mod

    reg = _fresh_registry()
    send_message_tool.register_tools(reg)
    reg.register("file_read", "read", {"type": "object", "properties": {}}, lambda args: "ok")
    monkeypatch.setattr(tr_mod, "registry", reg)
    monkeypatch.setattr("agent.loop.registry", reg)
    monkeypatch.setattr(send_message_tool, "_senders", {})
    monkeypatch.setattr(send_message_tool, "SUPPORTED_PLATFORMS", set())
    monkeypatch.setattr(send_message_tool, "_channel_meta", {})

    cfg = ModelConfig(name="synthetic", base_url="https://example.invalid", role="chat", tool_names=["send_message", "file_read"])
    before = [t["function"]["name"] for t in (resolve_model_tools(cfg) or [])]
    assert before == ["file_read"]

    async def single_target_sender(text: str):
        return {"sent_chunks": 1, "total_chunks": 1}

    plugins_dir = tmp_path / "plugins"
    _write_plugin(
        plugins_dir,
        "qqfake",
        "id: qqfake\nname: QQ\nversion: '1.0'\nentrypoint: plugin.py:setup\npermissions:\n  - channels:register\n",
        (
            "async def _send(text):\n"
            "    return {'sent_chunks': 1, 'total_chunks': 1}\n"
            "def setup(ctx):\n"
            "    ctx.register_channel('qq', sender=_send, description='QQ C2C')\n"
        ),
    )
    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    try:
        await pm.startup({"plugins": {"items": {"qqfake": {"enabled": True, "trusted": True}}}})
        during = [t["function"]["name"] for t in (resolve_model_tools(cfg) or [])]
        assert during == ["send_message", "file_read"]
        bad = json.loads(await reg.execute("send_message", {"platform": "qq", "text": "hi", "target": "other"}))
        assert "不支持指定 target" in bad["error"]
    finally:
        await pm.shutdown()

    after = [t["function"]["name"] for t in (resolve_model_tools(cfg) or [])]
    assert after == ["file_read"]


async def test_channel_and_background_task_permissions_and_cleanup(tmp_path):
    plugins_dir = tmp_path / "plugins"
    pdir = _write_plugin(
        plugins_dir,
        "noreg",
        "id: noreg\nname: NoReg\nversion: '1.0'\n",
        "",
    )
    ctx_no_perm = PluginContext(
        manifest=parse_manifest(pdir),
        plugin_dir=pdir,
        data_dir=tmp_path / "data" / "noreg",
        config={},
        disabled_tools=set(),
        tool_registry=_fresh_registry(),
        command_registry=CommandRegistry(),
    )
    with pytest.raises(PermissionDeniedError, match="channels:register"):
        ctx_no_perm.register_channel("discord", sender=lambda t: {})
    with pytest.raises(PermissionDeniedError, match="background:task"):
        coro = asyncio.sleep(0)
        try:
            ctx_no_perm.create_background_task(coro)
        finally:
            coro.close()

    _write_plugin(
        plugins_dir,
        "worker",
        "id: worker\nname: Worker\nversion: '1.0'\nentrypoint: plugin.py:setup\n"
        "permissions:\n  - background:task\n",
        (
            "import asyncio\n"
            "ticks = []\n"
            "async def _bg():\n"
            "    try:\n"
            "        while True:\n"
            "            ticks.append(1)\n"
            "            await asyncio.sleep(0.01)\n"
            "    except asyncio.CancelledError:\n"
            "        ticks.append(-1)\n"
            "        raise\n"
            "def setup(ctx):\n"
            "    ctx.create_background_task(_bg(), name='ticker')\n"
        ),
    )

    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=_fresh_registry(),
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    try:
        states = await pm.startup({"plugins": {"items": {"worker": {"enabled": True, "trusted": True}}}})
        assert states["worker"].status == "active"
        assert states["worker"].background_tasks == 1
        await asyncio.sleep(0.03)
        mod = pm._active_runtimes["worker"]["module"]
        assert len(mod.ticks) >= 1
    finally:
        await pm.shutdown()
    assert -1 in mod.ticks


async def test_extension_web_routes_enforce_device_gate_and_auth(tmp_path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin(
        plugins_dir,
        "webext",
        "id: webext\nname: WebExt\nversion: '1.0'\nentrypoint: plugin.py:setup\n"
        "permissions:\n  - web:routes\n",
        (
            "from fastapi import APIRouter\n"
            "def setup(ctx):\n"
            "    router = APIRouter()\n"
            "    @router.get('/ping')\n"
            "    async def ping():\n"
            "        return {'plugin': ctx.plugin_id, 'ok': True}\n"
            "    ctx.register_router(router)\n"
        ),
    )

    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=_fresh_registry(),
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    settings = WebSettings(str(tmp_path / "web-settings.json"))
    cookie = settings.create_device("trusted-device")
    settings.set_device_auth(True)
    settings.set_password("pw")

    fake_session = SimpleNamespace(
        role="chat",
        model_id="sample",
        chronicle=SimpleNamespace(db_path=tmp_path / "unused.db"),
        manager=SimpleNamespace(snapshot=lambda: {}, config_path=tmp_path / "config.yaml"),
    )
    fake_cfg_svc = SimpleNamespace(
        snapshot=lambda: {},
        close=AsyncMock(),
    )
    try:
        states = await pm.startup({"plugins": {"items": {"webext": {"enabled": True, "trusted": True}}}})
        assert states["webext"].status == "active"
        assert "/api/plugins/ext/webext/ping" in states["webext"].routes

        app = create_app(
            ConnectionManager(),
            None,
            fake_session,
            SimpleNamespace(reset_timer=lambda: None),
            None,
            fake_session.chronicle,
            settings,
            None,
            configuration_service=fake_cfg_svc,
            plugin_manager=pm,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            # 1. 无设备 Cookie、无 Bearer -> 被 DeviceAccessGate / auth_dep 拦截
            assert (await client.get("/api/plugins/ext/webext/ping")).status_code == 401
            # 2. 仅有设备 Cookie，无登录令牌 -> 401
            client.cookies.set(DEVICE_COOKIE, cookie)
            assert (await client.get("/api/plugins/ext/webext/ping")).status_code == 401
            # 3. 完整双因素认证通过 -> 200
            client.headers["Authorization"] = "Bearer " + settings.issue_token()
            resp = await client.get("/api/plugins/ext/webext/ping")
            assert resp.status_code == 200
            assert resp.json() == {"plugin": "webext", "ok": True}
            # 4. 吊销设备后立即失效
            dev_id = next(iter(settings.list_devices()))["id"]
            settings.revoke_device(dev_id)
            assert (await client.get("/api/plugins/ext/webext/ping")).status_code == 401
    finally:
        await pm.shutdown()


async def test_optional_env_and_http_chat_adapter_primitives(tmp_path):
    plugins_dir = tmp_path / "plugins"
    # 1. optional_env 不阻断启动，允许 ctx.get_env 读取，但未声明 env 仍拒绝
    _write_plugin(
        plugins_dir,
        "clientext",
        "id: clientext\nname: ClientExt\nversion: '1.0'\nentrypoint: plugin.py:setup\n"
        "optional_env:\n  - CLIENTEXT_OPT_KEY\n"
        "permissions:\n  - web:routes\n  - hooks:mutate\n",
        (
            "from contextvars import ContextVar\n"
            "from plugins.permissions import PermissionDeniedError\n"
            "current_tag = ContextVar('clientext_tag', default=None)\n"
            "seen_events = []\n"
            "class Turn:\n"
            "    shield_disconnect = True\n"
            "    def __init__(self, tag): self.tag = tag; self.tok = None\n"
            "    def enter(self): self.tok = current_tag.set(self.tag)\n"
            "    def finish(self, status): seen_events.append((self.tag, status))\n"
            "    def exit(self): current_tag.reset(self.tok)\n"
            "class Adapter:\n"
            "    def prepare(self, *, request, platform, request_id, client_meta):\n"
            "        if isinstance(client_meta, dict) and client_meta.get('client_tag'):\n"
            "            return Turn(client_meta['client_tag'])\n"
            "        return None\n"
            "def setup(ctx):\n"
            "    ctx.opt_read = ctx.get_env('CLIENTEXT_OPT_KEY', 'fallback')\n"
            "    try:\n"
            "        ctx.get_env('UNDECLARED_SECRET')\n"
            "    except PermissionDeniedError:\n"
            "        ctx.blocked_undeclared = True\n"
            "    ctx.register_http_chat_adapter(Adapter())\n"
        ),
    )

    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=_fresh_registry(),
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    settings = WebSettings(str(tmp_path / "web-settings.json"))
    observed_tags = []

    async def run_round(text, platform, **kwargs):
        mod = pm._active_runtimes["clientext"]["module"]
        observed_tags.append(mod.current_tag.get())
        return SimpleNamespace(round_status={"status": "completed"}, stop_reason="complete", total_usage={})

    try:
        states = await pm.startup({"plugins": {"items": {"clientext": {"enabled": True, "trusted": True}}}})
        assert states["clientext"].status == "active"
        assert states["clientext"].missing_env == []
        assert states["clientext"].env_vars == ["CLIENTEXT_OPT_KEY"]
        ctx = pm._active_runtimes["clientext"]["ctx"]
        assert ctx.opt_read == "fallback"
        assert ctx.blocked_undeclared is True

        app = create_app(
            ConnectionManager(),
            None,
            SimpleNamespace(run_round=run_round),
            SimpleNamespace(reset_timer=lambda: None),
            None,
            SimpleNamespace(db_path=tmp_path / "unused.db"),
            settings,
            None,
            plugin_manager=pm,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            resp = await client.post(
                "/api/chat/send",
                json={"text": "hello", "platform": "desktop", "client_meta": {"client_tag": "mac-tray"}},
            )
            assert resp.status_code == 200
            resp2 = await client.post("/api/chat/send", json={"text": "plain", "platform": "web"})
            assert resp2.status_code == 200

        mod = pm._active_runtimes["clientext"]["module"]
        assert observed_tags == ["mac-tray", None]
        assert mod.seen_events == [("mac-tray", "completed")]
        assert mod.current_tag.get() is None
    finally:
        await pm.shutdown()
    assert pm.get_http_chat_adapters() == []
