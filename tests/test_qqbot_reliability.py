"""QQ 连接恢复与发送反馈回归；全部使用 fake 网络和合成消息。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage

import plugins.bundled.qqbot.adapter as qq_mod
import tools.send_message as send_mod
from agent.commands import CommandRegistry
from agent.loop import AgentLoop
from agent.protocol import Context, Event, EventType, ModelConfig
from plugins.bundled.qqbot.adapter import QQAdapter, QQCloseError
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from plugins.manifest import bundled_plugins_dir
from runtime.tool_registry import ToolRegistry, registry
from web.adapter import WebAdapter


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(QQAdapter, "_load_latest_chat", lambda self: None)
    monkeypatch.setattr(send_mod, "_senders", {})
    monkeypatch.setattr(send_mod, "SUPPORTED_PLATFORMS", set())
    monkeypatch.setattr(send_mod, "_channel_meta", {})
    result = QQAdapter(session=None, default_to="synthetic-user")
    result._send_interval = 0
    return result


class FakeWS:
    def __init__(self, on_send=None):
        self.closed = False
        self.close_code = None
        self.sent = []
        self.on_send = on_send

    async def send_json(self, payload):
        self.sent.append(payload)
        if self.on_send:
            await self.on_send(payload)

    async def close(self):
        self.closed = True

    async def receive(self):
        await asyncio.Future()


async def test_missing_heartbeat_ack_closes_connection(adapter):
    adapter._running = True
    adapter._heartbeat_interval = 0.001
    adapter._ws = FakeWS()
    await asyncio.wait_for(adapter._heartbeat_loop(), timeout=1)
    assert adapter._ws.closed
    assert adapter._ws.sent == [{"op": 1, "d": None}]


async def test_ack_keeps_connection_alive(adapter):
    async def ack(payload):
        await adapter._dispatch_payload({"op": 11})
        if len(adapter._ws.sent) == 3:
            adapter._running = False

    adapter._running = True
    adapter._heartbeat_interval = 0.001
    adapter._ws = FakeWS(ack)
    await asyncio.wait_for(adapter._heartbeat_loop(), timeout=1)
    assert len(adapter._ws.sent) == 3
    assert not adapter._ws.closed


@pytest.mark.parametrize("blocked", [False, True])
async def test_heartbeat_send_failure_reconnects(adapter, blocked):
    async def fail(payload):
        if blocked:
            await asyncio.Future()
        raise OSError("synthetic network failure")

    adapter._running = True
    adapter._heartbeat_interval = 0.001
    adapter._ws = FakeWS(fail)
    await asyncio.wait_for(adapter._heartbeat_loop(), timeout=1)
    assert adapter._ws.closed


async def test_handshake_timeout(adapter, monkeypatch):
    monkeypatch.setattr(qq_mod, "CONNECT_TIMEOUT_SECONDS", 0.001)
    adapter._running = True
    adapter._ws = FakeWS()
    with pytest.raises(TimeoutError):
        await adapter._listen_forever()


async def test_hello_starts_heartbeat_and_resumes(adapter):
    adapter._session_id = "synthetic-session"
    adapter._last_seq = 42
    adapter._send_resume = AsyncMock()
    adapter._heartbeat_loop = AsyncMock()
    await adapter._dispatch_payload({"op": 10, "d": {"heartbeat_interval": 30000}})
    await adapter._heartbeat_task
    adapter._send_resume.assert_awaited_once()
    await adapter._dispatch_payload({"op": 0, "t": "RESUMED", "d": {}})
    assert adapter._ready_at is not None


async def test_resume_send_failure_reaches_reconnect_loop(adapter):
    adapter._ws = FakeWS(AsyncMock(side_effect=OSError("synthetic failure")))
    adapter._session_id = "synthetic-session"
    adapter._last_seq = 42
    adapter._ensure_token = AsyncMock(return_value="synthetic-token")
    with pytest.raises(OSError):
        await adapter._send_resume()
    assert adapter._session_id is None
    assert adapter._last_seq is None


def fake_clock(monkeypatch):
    clock = [100.0]
    delays = []
    monkeypatch.setattr(qq_mod, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(qq_mod.asyncio, "sleep", sleep)
    return clock, delays


async def test_rapid_disconnects_accumulate_after_ws_connect(adapter, monkeypatch):
    _, delays = fake_clock(monkeypatch)
    adapter._connect = AsyncMock()
    adapter._listen_forever = AsyncMock(side_effect=QQCloseError(1000))
    adapter._teardown_ws = AsyncMock()
    await adapter.run()
    assert adapter._connect.await_count == 3
    assert delays == [2, 5]
    assert not adapter._running


async def test_attempt_limit_independent_of_backoff_index(adapter, monkeypatch):
    clock, delays = fake_clock(monkeypatch)
    monkeypatch.setattr(qq_mod, "MAX_RECONNECT_ATTEMPTS", 7)

    async def fail_connect():
        clock[0] += 6
        raise OSError("synthetic connect failure")

    adapter._connect = AsyncMock(side_effect=fail_connect)
    adapter._teardown_ws = AsyncMock()
    await adapter.run()
    assert adapter._connect.await_count == 7
    assert delays == [2, 5, 10, 30, 60, 60]


async def test_stable_ready_resets_failures(adapter, monkeypatch):
    clock, delays = fake_clock(monkeypatch)
    rounds = []

    async def listen():
        rounds.append(1)
        if len(rounds) == 2:
            await adapter._dispatch_payload({"op": 0, "t": "READY", "d": {"session_id": "s"}})
            clock[0] += 6
        raise QQCloseError(1000)

    adapter._connect = AsyncMock()
    adapter._listen_forever = listen
    adapter._teardown_ws = AsyncMock()
    await adapter.run()
    assert len(rounds) == 5
    assert delays == [2, 2, 5, 10]


async def test_tool_waits_for_actual_delivery(adapter):
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(openid, content):
        entered.set()
        await release.wait()

    adapter._send_c2c_text = AsyncMock(side_effect=send)
    send_mod.register_sender("qq", adapter._deliver)
    task = asyncio.create_task(send_mod.send_message({"platform": "qq", "text": "synthetic message"}))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not task.done()
    release.set()
    assert await task == "已发送:1段"
    adapter._send_c2c_text.assert_awaited_once_with("synthetic-user", "synthetic message")


async def test_partial_delivery_reports_failure_and_does_not_retry(adapter):
    adapter._send_c2c_text = AsyncMock(side_effect=[{}, TimeoutError("synthetic timeout")])
    send_mod.register_sender("qq", adapter._deliver)
    result = json.loads(await send_mod.send_message({"platform": "qq", "text": "first\n\nsecond\n\nthird"}))
    assert "error" in result
    assert "1/3" in result["error"]
    assert "状态未知" in result["error"]
    assert adapter._send_c2c_text.await_count == 2


async def test_no_channel_or_target_reports_failure(adapter):
    args = {"platform": "qq", "text": "synthetic message"}
    assert "error" in json.loads(await send_mod.send_message(args))
    adapter._default_to = ""
    send_mod.register_sender("qq", adapter._deliver)
    assert "无可用会话目标" in json.loads(await send_mod.send_message(args))["error"]


async def test_bundled_qqbot_plugin_registers_channel_and_cleans_up(tmp_path, monkeypatch):
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    send_mod.register_tools(reg)

    pm = PluginManager(
        plugins_dir=tmp_path / "plugins",
        bundled_dir=bundled_plugins_dir(),
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )

    monkeypatch.delenv("QQ_APP_ID", raising=False)
    monkeypatch.delenv("QQ_CLIENT_SECRET", raising=False)
    states_missing = await pm.startup({"plugins": {"items": {"qqbot": {"enabled": True, "trusted": True}}}})
    assert states_missing["qqbot"].status == "degraded"
    assert set(states_missing["qqbot"].missing_env) == {"QQ_APP_ID", "QQ_CLIENT_SECRET"}
    assert "qq" not in send_mod._senders

    monkeypatch.setenv("QQ_APP_ID", "synthetic-id")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "synthetic-secret")

    import aiohttp

    class FakeSession:
        async def post(self, *args, **kwargs):
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

        async def close(self):
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    states = await pm.startup({
        "plugins": {
            "items": {
                "qqbot": {
                    "enabled": True,
                    "trusted": True,
                    "config": {"default_to": "synthetic-user"},
                }
            }
        }
    })
    try:
        assert states["qqbot"].status == "active"
        assert states["qqbot"].channels == ["qq"]
        assert states["qqbot"].background_tasks == 1
        created = pm._active_runtimes["qqbot"]["ctx"].qqbot_adapter
        created._send_interval = 0
        created._send_c2c_text = AsyncMock(return_value={})
        args = {"platform": "qq", "text": "synthetic message"}
        assert await send_mod.send_message(args) == "已发送:1段"
    finally:
        await pm.shutdown()
    assert "error" in json.loads(await send_mod.send_message({"platform": "qq", "text": "synthetic message"}))
    assert "qq" not in send_mod._senders


@pytest.mark.parametrize("failed", [False, True])
async def test_model_and_web_receive_actual_delivery_result(adapter, failed, monkeypatch):
    adapter._send_c2c_text = AsyncMock(side_effect=OSError("synthetic failure") if failed else None,
                                     return_value={})
    monkeypatch.setattr(registry, "_tools", dict(registry._tools))
    registry._tools.pop("send_message", None)
    send_mod.register_tools(registry)
    send_mod.register_sender("qq", adapter._deliver)
    conn = SimpleNamespace(broadcast=AsyncMock())
    web = WebAdapter(None, conn, settings=SimpleNamespace(split_messages=True))
    call = ChatCompletionMessage.model_validate({
        "role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function",
        "function": {"name": "send_message", "arguments": json.dumps({"platform": "qq", "text": "hello"})}}],
    })
    loop = object.__new__(AgentLoop)
    loop.model_config = ModelConfig(name="synthetic", base_url="https://model.invalid",
                                    tool_names=["send_message"], max_iterations=2, stream=False)
    loop.callback = web._callback
    loop.client = None
    loop.dialect = SimpleNamespace(call=AsyncMock(side_effect=[
        SimpleNamespace(message=call, usage=None, web_search_calls=[]),
        SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="done"),
                        usage=None, web_search_calls=[]),
    ]))
    context = Context(current_message="synthetic request", history_messages=[], system_prompt="", platform="web")
    result = await loop.run([{"role": "user", "content": "synthetic request"}], context)
    tool_result = next(msg for msg in result.full_messages if msg.get("role") == "tool")
    if failed:
        assert json.loads(tool_result["content"])["error"]
    else:
        assert tool_result["content"] == "已发送:1段"
    payloads = [call.args[0] for call in conn.broadcast.await_args_list]
    qq_messages = [p for p in payloads if p["type"] == "message" and p["data"].get("platform") == "qq"]
    assert len(qq_messages) == (0 if failed else 1)
    assert adapter._send_c2c_text.await_count == 1


@pytest.mark.parametrize("result, included", [('{"error":"synthetic failure"}', False), (None, False),
                                           ('{"status":"sent"}', True), ('已发送:1段', True)])
def test_archive_only_includes_confirmed_sends(result, included):
    from agent.visible import VisibleMessages
    visible = VisibleMessages("web")
    visible.consume(Event(EventType.TOOL_CALL_START, {
        "name": "send_message", "tool_call_id": "call1",
        "arguments": json.dumps({"platform": "qq", "text": "synthetic message"}),
    }))
    if result is not None:
        visible.consume(Event(EventType.TOOL_RESULT, {
            "name": "send_message", "tool_call_id": "call1", "result": result,
        }))
    visible.finish()
    assert bool(visible.messages) is included
