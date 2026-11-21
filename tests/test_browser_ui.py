"""内置 browser 插件实时视口（agent-browser stream 桥接 + 沙箱 UI）测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.commands import CommandRegistry
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from plugins.manifest import bundled_plugins_dir
from runtime.tool_registry import ToolRegistry
from scripts.deploy import managed as deploy_managed
from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


def _load_stream_bridge():
    path = bundled_plugins_dir() / "browser" / "stream_bridge.py"
    spec = importlib.util.spec_from_file_location("test_browser_stream_bridge", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stream_mod = _load_stream_bridge()


class FakeStreamWS:
    """模拟本机 agent-browser stream WebSocket。"""

    def __init__(self, incoming: list[str] | None = None):
        self.sent: list[dict] = []
        self.closed = False
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        for item in incoming or []:
            self._queue.put_nowait(item)

    def push(self, payload: dict) -> None:
        self._queue.put_nowait(json.dumps(payload))

    async def send(self, text: str) -> None:
        if self.closed:
            raise RuntimeError("socket closed")
        self.sent.append(json.loads(text))

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def test_browser_ui_files_are_in_deploy_whitelist():
    assert deploy_managed("plugins/bundled/browser/manifest.yaml")
    assert deploy_managed("plugins/bundled/browser/plugin.py")
    assert deploy_managed("plugins/bundled/browser/stream_bridge.py")
    assert deploy_managed("plugins/bundled/browser/ui/index.html")


def test_normalize_navigation_url_and_sanitize_inputs():
    assert stream_mod.normalize_navigation_url("example.com/login") == "https://example.com/login"
    assert stream_mod.normalize_navigation_url("http://127.0.0.1:8080/a") == "http://127.0.0.1:8080/a"
    assert stream_mod.normalize_navigation_url("about:blank") == "about:blank"

    for bad in ("", "javascript:alert(1)", "file:///etc/passwd", "chrome://settings", "https://a\nb"):
        with pytest.raises(ValueError):
            stream_mod.normalize_navigation_url(bad)

    mouse_msgs, delay = stream_mod.sanitize_input_event({
        "type": "input_mouse",
        "eventType": "mouseWheel",
        "x": 120.5,
        "y": 300,
        "deltaX": 0,
        "deltaY": 240,
        "delayMs": 999,
    })
    assert delay == 80
    assert mouse_msgs == [{
        "type": "input_mouse",
        "eventType": "mouseWheel",
        "x": 120.5,
        "y": 300.0,
        "deltaX": 0.0,
        "deltaY": 240.0,
    }]

    text_msgs, _ = stream_mod.sanitize_input_event({"type": "input_text", "text": "中文验证码123"})
    assert text_msgs == [{"type": "input_keyboard", "eventType": "char", "text": "中文验证码123"}]

    with pytest.raises(ValueError):
        stream_mod.sanitize_input_event({"type": "eval", "code": "alert(1)"})

    assert stream_mod.normalize_tab_ref("t1") == "t1"
    assert stream_mod.normalize_tab_ref("docs-tab_2") == "docs-tab_2"
    assert stream_mod.normalize_tab_ref("0123456789ABCDEF") == "0123456789ABCDEF"
    for bad_ref in ("", "0", "1", "12", "t1; rm -rf", "-bad"):
        with pytest.raises(ValueError):
            stream_mod.normalize_tab_ref(bad_ref)


async def test_stream_bridge_connect_frame_ack_input_and_disconnect():
    cli_calls: list[str] = []
    emitted: list[tuple[str, dict]] = []
    fake_ws = FakeStreamWS()
    connected_urls: list[str] = []

    def fake_cli(args: dict, _cfg: dict | None = None) -> str:
        cmd = args["command"]
        cli_calls.append(cmd)
        if cmd == "stream status --json":
            return json.dumps({"success": True, "data": {"enabled": False}})
        if cmd == "stream enable --json":
            return json.dumps({"success": True, "data": {"enabled": True, "port": 19333}})
        if cmd == "tab --json":
            return json.dumps({
                "success": True,
                "data": {
                    "tabs": [
                        {"tabId": "t1", "label": None, "title": "Login", "url": "https://example.com", "active": True},
                        {"tabId": "t2", "label": "docs", "title": "Docs", "url": "https://docs.example.com", "active": False},
                    ]
                },
            })
        if cmd == "stream disable":
            return "disabled"
        if cmd.startswith("open "):
            return "opened"
        return "ok"

    async def fake_emit(event: str, payload: dict) -> None:
        emitted.append((event, payload))

    async def fake_ws_connect(url: str):
        connected_urls.append(url)
        return fake_ws

    bridge = stream_mod.BrowserStreamBridge(
        cfg={"session": "s-demo", "headed": False},
        run_cli_fn=fake_cli,
        emit_ui_event_fn=fake_emit,
        ws_connect_fn=fake_ws_connect,
    )

    try:
        snap = await bridge.connect(max_fps=8)
        assert snap["connected"] is True
        assert snap["port"] == 19333
        assert snap["mode"] == "local"
        assert snap["headed"] is False
        assert snap["url"] == "https://example.com"
        assert snap["tabs"][0]["tab_id"] == "t1"
        assert snap["tabs"][1]["tab_id"] == "t2"
        assert snap["tabs"][1]["label"] == "docs"
        assert connected_urls == ["ws://127.0.0.1:19333/?pacing=ack&maxFps=8"]
        # 连接后自动对活动标签执行一次 tab t1 以同步真实视口
        assert "tab t1" in cli_calls

        fake_ws.push({
            "type": "status",
            "connected": True,
            "screencasting": True,
            "viewportWidth": 1536,
            "viewportHeight": 824,
        })
        await asyncio.sleep(0.05)
        assert bridge.snapshot()["viewport"] == {"width": 1536, "height": 824}

        fake_ws.push({
            "type": "frame",
            "seq": 7,
            "data": "ZmFrZS1qcGVn",
            "metadata": {"deviceWidth": 1536, "deviceHeight": 824, "offsetTop": 0, "timestamp": 123456},
        })
        await asyncio.sleep(0.05)

        assert any(ev == "stream.frame" and pl["seq"] == 7 for ev, pl in emitted)
        assert {"type": "ack", "seq": 7} in fake_ws.sent
        snap_with_frame = bridge.snapshot(include_frame=True)
        assert snap_with_frame["frame"]["data"] == "ZmFrZS1qcGVn"
        assert snap_with_frame["viewport"] == {"width": 1536, "height": 824}

        sent = await bridge.send_inputs([
            {"type": "input_mouse", "eventType": "mousePressed", "x": 100, "y": 200, "button": "left", "clickCount": 1},
            {"type": "input_text", "text": "你好"},
        ])
        assert sent == 2
        assert fake_ws.sent[-2]["eventType"] == "mousePressed"
        assert fake_ws.sent[-1] == {"type": "input_keyboard", "eventType": "char", "text": "你好"}

        await bridge.perform_action(stream_mod.BrowserActionBody(action="open", url="example.org/a b"))
        assert any(c.startswith("open ") and "https://example.org/a b" in c for c in cli_calls)

        await bridge.perform_action(stream_mod.BrowserActionBody(action="tab_select", tab_id="t2"))
        assert "tab t2" in cli_calls

        await bridge.perform_action(stream_mod.BrowserActionBody(action="tab_close", tab_id="t2"))
        assert "tab close t2" in cli_calls

        # 兼容只传 index 时从当前 tabs 反查稳定 tabId，绝不传递纯数字给 agent-browser
        await bridge.perform_action(stream_mod.BrowserActionBody(action="tab_select", index=1))
        assert cli_calls[-2] == "tab t2"

        await bridge.perform_action(stream_mod.BrowserActionBody(action="sync_viewport"))
        assert cli_calls[-2] == "tab t1"

        with pytest.raises(ValueError):
            await bridge.perform_action(stream_mod.BrowserActionBody(action="tab_select", tab_id="1"))
    finally:
        await bridge.disconnect()

    assert fake_ws.closed is True
    assert "stream disable" in cli_calls


async def test_stream_bridge_connect_reports_error_without_fallback():
    def failing_cli(args: dict, _cfg: dict | None = None) -> str:
        cmd = args["command"]
        if cmd == "stream status --json":
            return json.dumps({"success": False, "error": "stream not supported"})
        if cmd == "stream enable --json":
            return json.dumps({"success": False, "error": "stream not supported"})
        return ""

    async def noop_emit(_event: str, _payload: dict) -> None:
        return None

    bridge = stream_mod.BrowserStreamBridge(
        cfg={"session": "s-err"},
        run_cli_fn=failing_cli,
        emit_ui_event_fn=noop_emit,
    )
    with pytest.raises(RuntimeError, match="stream not supported"):
        await bridge.connect()
    assert bridge.snapshot()["connected"] is False


async def test_bundled_browser_ui_and_ext_routes_behind_auth(tmp_path):
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    pm = PluginManager(
        plugins_dir=tmp_path / "user-plugins",
        bundled_dir=bundled_plugins_dir(),
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )

    await pm.startup({
        "plugins": {
            "items": {
                "browser": {
                    "enabled": True,
                    "trusted": True,
                    "config": {"session": "ui-test", "headed": False},
                }
            },
            "mcp": {},
        }
    })

    try:
        settings = WebSettings(str(tmp_path / "webui_settings.json"))
        settings.set_password("pw123456")
        token = settings.issue_token()
        dev_cookie = settings.create_device("test-browser")
        settings.set_device_auth(True)
        settings.deny_new_devices()

        conn = ConnectionManager()
        conn.broadcast = AsyncMock()
        app = create_app(
            conn_manager=conn,
            web_adapter=SimpleNamespace(),
            agent_session=SimpleNamespace(),
            heartbeat=SimpleNamespace(),
            session_manager=SimpleNamespace(),
            chronicle=SimpleNamespace(db_path=str(tmp_path / "chronicle.db")),
            settings=settings,
            goal_manager=SimpleNamespace(),
            plugin_manager=pm,
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            # 未携带设备 Cookie 必须被 DeviceAccessGate 拦截
            assert (await client.get("/api/plugins/browser/ui/document")).status_code == 403
            assert (await client.get("/api/plugins/ext/browser/status")).status_code == 403

            # 仅有设备 Cookie 但无 Bearer Token 必须被 auth_dep 拦截
            client.cookies.set(DEVICE_COOKIE, dev_cookie)
            assert (await client.get("/api/plugins/browser/ui/document")).status_code == 401
            assert (await client.get("/api/plugins/ext/browser/status")).status_code == 401

            headers = {"Authorization": f"Bearer {token}"}
            ui_list = await client.get("/api/plugins/ui", headers=headers)
            assert ui_list.status_code == 200
            items = ui_list.json()["items"]
            browser_entry = next(i for i in items if i["plugin_id"] == "browser")
            assert browser_entry["title"] == "浏览器视口"
            assert browser_entry["placement"] == "sidebar"
            assert browser_entry["has_routes"] is True

            doc_res = await client.get("/api/plugins/browser/ui/document", headers=headers)
            assert doc_res.status_code == 200
            html = doc_res.json()["html"]
            assert "Content-Security-Policy" in html
            assert "回到会话" in html
            assert "openChat" in html

            status_res = await client.get("/api/plugins/ext/browser/status", headers=headers)
            assert status_res.status_code == 200
            body = status_res.json()
            assert body["connected"] is False
            assert body["session"] == "ui-test"
            assert body["mode"] == "local"
            assert body["headed"] is False
    finally:
        await pm.shutdown()


def test_preview_browser_ui_document_and_synthetic_stream():
    from starlette.testclient import TestClient

    from dev.preview import create_preview

    app = create_preview()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        doc = client.get("/api/plugins/browser/ui/document")
        assert doc.status_code == 200
        assert "浏览器实时视口" in doc.json()["html"]

        conn = client.post("/api/plugins/ext/browser/stream/connect", json={"max_fps": 10})
        assert conn.status_code == 200
        payload = conn.json()
        assert payload["connected"] is True
        assert payload["frame"]["data"]

        inp = client.post(
            "/api/plugins/ext/browser/input",
            json={"events": [{"type": "input_text", "text": "hi"}]},
        )
        assert inp.status_code == 200
        assert inp.json()["ok"] is True

        sel = client.post("/api/plugins/ext/browser/action", json={"action": "tab_select", "tab_id": "t2"})
        assert sel.status_code == 200
        assert any(t["tab_id"] == "t2" and t["active"] for t in sel.json()["tabs"])

        sync = client.post("/api/plugins/ext/browser/action", json={"action": "sync_viewport"})
        assert sync.status_code == 200

        close = client.post("/api/plugins/ext/browser/action", json={"action": "tab_close", "tab_id": "t2"})
        assert close.status_code == 200
        assert len(close.json()["tabs"]) == 1

        disc = client.post("/api/plugins/ext/browser/stream/disconnect", json={})
        assert disc.status_code == 200
        assert disc.json()["connected"] is False
