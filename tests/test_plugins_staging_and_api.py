"""Phase 5 主 Agent 暂存工具、ConfigStore schema 合并与 /api/plugins/* 接口测试。"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.message import SessionMessage

from agent.commands import CommandRegistry
from config.files import ConfigFiles
from config.service import ConfigurationService
from config.store import ConfigStore, fields_for
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from runtime.control import RestartController
from runtime.tool_registry import ToolRegistry
from tools import plugins as plugins_tool
from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


def _fresh_registry() -> ToolRegistry:
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    return reg


def _make_config_env(tmp_path: Path):
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text(
        "config_version: 2\nproviders: {}\nmodels: {}\nroles:\n  chat:\n    model: null\n"
        "heartbeat:\n  enabled: true\n  interval_minutes: 30\nplugins:\n  items: {}\n  mcp: {}\n",
        encoding="utf-8",
    )
    user_cfg = tmp_path / "config.yaml"
    user_cfg.write_text("# user override comment\nplugins:\n  items: {}\n  mcp: {}\n", encoding="utf-8")
    dotenv = tmp_path / ".env"
    dotenv.write_text("", encoding="utf-8")
    files = ConfigFiles(user_cfg)
    store = ConfigStore(files, backup_dir=tmp_path / "cfg_backups")
    return defaults, user_cfg, dotenv, store


def test_config_store_merges_plugin_schema_and_preserves_comments(tmp_path):
    _, user_cfg, _, store = _make_config_env(tmp_path)
    plugins_dir = tmp_path / "plugins"
    pdir = plugins_dir / "weather"
    pdir.mkdir(parents=True)
    (pdir / "manifest.yaml").write_text(
        "id: weather\nname: Weather\nversion: '1.0'\n"
        "config_schema:\n"
        "  timeout_sec:\n"
        "    type: integer\n"
        "    label: 超时\n"
        "    default: 10\n"
        "    minimum: 1\n"
        "    maximum: 60\n",
        encoding="utf-8",
    )

    store.upsert_plugin_entry("items", "weather", {"enabled": False, "trusted": False, "config": {"timeout_sec": 15}})
    assert "# user override comment" in user_cfg.read_text(encoding="utf-8")

    # 动态 schema 字段合并到 fields_for
    from config.store import _append_plugin_fields

    collected = []
    _append_plugin_fields(lambda *a, **kw: collected.append((a, kw)), plugins_dir=plugins_dir)
    paths = [a[0] for a, _ in collected]
    assert "plugins.items.weather.config.timeout_sec" in paths
    # 默认 fields_for 可安全调用
    assert "heartbeat.enabled" in fields_for(store.read()[2])


def test_agent_staging_tools_always_write_pending_and_never_enable(tmp_path, monkeypatch):
    _, _, _, store = _make_config_env(tmp_path)
    plugins_dir = tmp_path / "plugins"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(plugins_tool, "_config_store", store)
    monkeypatch.setattr(plugins_tool, "_plugins_dir", plugins_dir)
    monkeypatch.setattr(plugins_tool, "_plugin_manager", None)
    monkeypatch.setattr(plugins_tool, "get_paths", lambda: SimpleNamespace(workspace=workspace))
    monkeypatch.delenv("DEMO_API_TOKEN", raising=False)

    res = json.loads(plugins_tool.plugin_install_local({
        "plugin_id": "staged",
        "files": {
            "manifest.yaml": (
                "id: staged\nname: Staged\nversion: '1.0'\n"
                "requires_env:\n  - DEMO_API_TOKEN\n"
                "permissions:\n  - tools:register\n  - network:outbound\n"
            ),
            "plugin.py": "def setup(ctx): pass\n",
        },
    }))
    assert res["status"] == "pending"
    assert res["missing_env"] == ["DEMO_API_TOKEN"]
    assert len(res["permission_details"]) == 2
    saved = store.read()[2]
    assert saved["plugins"]["items"]["staged"]["enabled"] is False
    assert saved["plugins"]["items"]["staged"]["trusted"] is False

    # 拒绝包含明文密钥的 MCP 暂存
    bad_mcp = json.loads(plugins_tool.mcp_stage_server({
        "server_id": "gh",
        "transport": "stdio",
        "command": "npx",
        "env_secrets": {"TOKEN": "sk-plaintext-secret-123"},
    }))
    assert "error" in bad_mcp

    ok_mcp = json.loads(plugins_tool.mcp_stage_server({
        "server_id": "gh",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "demo-mcp"],
        "env_secrets": {"GITHUB_TOKEN": "DEMO_API_TOKEN"},
    }))
    assert ok_mcp["status"] == "pending"
    assert ok_mcp["missing_env"] == ["DEMO_API_TOKEN"]
    assert store.read()[2]["plugins"]["mcp"]["gh"]["enabled"] is False

    listing = json.loads(plugins_tool.plugins_list({}))
    assert listing["local_plugins"][0]["id"] == "staged"
    assert listing["mcp_servers"][0]["id"] == "gh"


async def test_web_plugins_endpoints_approval_env_mcp_reconnect_and_restart(tmp_path, monkeypatch):
    _, user_cfg, dotenv, store = _make_config_env(tmp_path)
    plugins_dir = tmp_path / "plugins"
    monkeypatch.delenv("PLUGIN_EXT_TOKEN", raising=False)

    # 先暂存一个带 config_schema + requires_env 的本地插件
    staged_dir = plugins_dir / "weather"
    staged_dir.mkdir(parents=True)
    (staged_dir / "manifest.yaml").write_text(
        "id: weather\nname: Weather\nversion: '1.0'\n"
        "requires_env:\n  - PLUGIN_EXT_TOKEN\n"
        "permissions:\n  - tools:register\n"
        "config_schema:\n"
        "  max_days:\n"
        "    type: integer\n"
        "    label: 预报天数\n"
        "    default: 3\n"
        "    minimum: 1\n"
        "    maximum: 14\n",
        encoding="utf-8",
    )

    @contextlib.asynccontextmanager
    async def mock_mcp_transport(cfg, extra_env, extra_headers):
        async def on_list_tools(ctx, params):
            return types.ListToolsResult(tools=[
                types.Tool(name="echo", description="echo", inputSchema={"type": "object", "properties": {}}),
            ])

        async def on_call_tool(ctx, params):
            return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])

        server = Server(f"srv-{cfg.id}", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        c2s_send, c2s_recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        import asyncio
        t = asyncio.create_task(server.run(c2s_recv, s2c_send, server.create_initialization_options()))
        try:
            yield s2c_recv, c2s_send
        finally:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t

    reg = _fresh_registry()
    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
        mcp_transport_factory=mock_mcp_transport,
    )
    fake_session = SimpleNamespace(
        _lock=__import__("asyncio").Lock(),
        role="chat",
        model_id="",
        _loop=None,
        _pending_user_rounds=0,
        chronicle=SimpleNamespace(db_path=tmp_path / "unused.db"),
        manager=SimpleNamespace(snapshot=lambda: store.read()[2], config_path=user_cfg, files=store.files),
        session_manager=SimpleNamespace(set_model_role=lambda r: None),
        prepare_restart=lambda: None,
        _on_event=AsyncMock(),
    )
    restart = RestartController(fake_session, None)
    restart._exit = lambda: None
    cfg_svc = ConfigurationService(
        fake_session.manager,
        fake_session,
        SimpleNamespace(interval_seconds=1800, reset_timer=lambda: None),
        restart=restart,
    )
    cfg_svc.store = store

    settings = WebSettings(str(tmp_path / "web-settings.json"))
    cookie = settings.create_device("admin")
    settings.set_device_auth(True)
    settings.set_password("pw")

    await pm.startup(store.read()[2])
    app = create_app(
        ConnectionManager(),
        None,
        fake_session,
        cfg_svc.heartbeat,
        None,
        fake_session.chronicle,
        settings,
        None,
        configuration_service=cfg_svc,
        plugin_manager=pm,
    )

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            assert (await client.get("/api/plugins")).status_code == 401
            client.cookies.set(DEVICE_COOKIE, cookie)
            client.headers["Authorization"] = "Bearer " + settings.issue_token()

            snap = (await client.get("/api/plugins")).json()
            assert len(snap["plugins"]) == 1
            assert snap["plugins"][0]["status"] == "pending"
            assert snap["plugins"][0]["missing_env"] == ["PLUGIN_EXT_TOKEN"]
            rev = snap["revision"]

            # 1. 禁止写入未声明环境变量
            bad_env = await client.post("/api/plugins/env", json={"name": "UNDECLARED_VAR", "value": "x"})
            assert bad_env.status_code == 422

            # 2. 写入声明环境变量到 .env（响应绝不回显明文值）
            ok_env = await client.post("/api/plugins/env", json={"name": "PLUGIN_EXT_TOKEN", "value": "secret-abc"})
            assert ok_env.status_code == 200
            assert "secret-abc" not in ok_env.text
            assert ok_env.json() == {"ok": True, "name": "PLUGIN_EXT_TOKEN", "configured": True}
            assert os.getenv("PLUGIN_EXT_TOKEN") == "secret-abc"
            assert "PLUGIN_EXT_TOKEN='secret-abc'" in dotenv.read_text(encoding="utf-8")

            # 3. 审核启用插件并校验 config_schema 类型限制
            bad_dec = await client.post(
                "/api/plugins/local/weather/decision",
                json={"revision": rev, "enabled": True, "config": {"max_days": 999}},
            )
            assert bad_dec.status_code == 422

            ok_dec = await client.post(
                "/api/plugins/local/weather/decision",
                json={"revision": rev, "enabled": True, "config": {"max_days": 7}, "disabled_tools": []},
            )
            assert ok_dec.status_code == 200
            snap2 = ok_dec.json()
            assert snap2["restart_required"] is True
            assert snap2["plugins"][0]["saved_enabled"] is True
            assert snap2["plugins"][0]["missing_env"] == []
            rev = snap2["revision"]

            # 4. 添加 MCP 服务并热重连
            mcp_add = await client.post(
                "/api/plugins/mcp/echo_srv",
                json={
                    "revision": rev,
                    "config": {
                        "enabled": True,
                        "transport": "streamable_http",
                        "url": "https://mock.local/mcp",
                    },
                },
            )
            assert mcp_add.status_code == 200
            rev = mcp_add.json()["revision"]

            reconn = await client.post("/api/plugins/mcp/echo_srv/reconnect")
            assert reconn.status_code == 200
            assert reconn.json()["mcp_servers"][0]["status"] == "active"
            assert reg.has("mcp_echo_srv__echo")

            # 5. 删除本地插件并清理文件
            deleted = await client.delete(f"/api/plugins/local/weather?revision={rev}&delete_files=true")
            assert deleted.status_code == 200
            assert not staged_dir.exists()
            assert deleted.json()["plugins"] == []
    finally:
        await pm.shutdown()
        await cfg_svc.close()
        monkeypatch.delenv("PLUGIN_EXT_TOKEN", raising=False)


async def test_bundled_plugin_api_and_delete_protection(tmp_path: Path):
    from plugins.manifest import bundled_plugins_dir

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir(parents=True)
    defaults, user_cfg, dotenv, store = _make_config_env(tmp_path)

    reg = _fresh_registry()
    pm = PluginManager(
        plugins_dir=plugins_dir,
        bundled_dir=bundled_plugins_dir(),
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    fake_session = SimpleNamespace(
        _lock=__import__("asyncio").Lock(),
        role="chat",
        model_id="",
        _loop=None,
        _pending_user_rounds=0,
        chronicle=SimpleNamespace(db_path=tmp_path / "unused.db"),
        manager=SimpleNamespace(snapshot=lambda: store.read()[2], config_path=user_cfg, files=store.files),
        session_manager=SimpleNamespace(set_model_role=lambda r: None),
        prepare_restart=lambda: None,
        _on_event=AsyncMock(),
    )
    restart = RestartController(fake_session, None)
    restart._exit = lambda: None
    cfg_svc = ConfigurationService(
        fake_session.manager,
        fake_session,
        SimpleNamespace(interval_seconds=1800, reset_timer=lambda: None),
        restart=restart,
    )
    cfg_svc.store = store
    settings = WebSettings(str(tmp_path / "web-settings.json"))
    cookie = settings.create_device("admin")
    settings.set_device_auth(True)
    settings.set_password("pw")

    await pm.startup(store.read()[2])
    app = create_app(
        ConnectionManager(),
        None,
        fake_session,
        cfg_svc.heartbeat,
        None,
        fake_session.chronicle,
        settings,
        None,
        configuration_service=cfg_svc,
        plugin_manager=pm,
    )

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            client.cookies.set(DEVICE_COOKIE, cookie)
            client.headers["Authorization"] = "Bearer " + settings.issue_token()

            snap = (await client.get("/api/plugins")).json()
            browser_item = next(p for p in snap["plugins"] if p["id"] == "browser")
            assert browser_item["bundled"] is True
            rev = snap["revision"]

            # 禁止删除内置插件源码目录
            refused = await client.delete(f"/api/plugins/local/browser?revision={rev}&delete_files=true")
            assert refused.status_code == 422
            assert bundled_plugins_dir().joinpath("browser", "manifest.yaml").exists()

            # 可以通过 decision 禁用或保存 string_list 配置
            updated = await client.post(
                "/api/plugins/local/browser/decision",
                json={
                    "revision": rev,
                    "enabled": False,
                    "trusted": True,
                    "config": {"allowed_domains": ["example.com", "*.example.org"]},
                },
            )
            assert updated.status_code == 200
            item2 = next(p for p in updated.json()["plugins"] if p["id"] == "browser")
            assert item2["saved_enabled"] is False
            assert item2["saved_config"]["allowed_domains"] == ["example.com", "*.example.org"]
    finally:
        await pm.shutdown()
        await cfg_svc.close()
