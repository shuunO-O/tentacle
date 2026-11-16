"""回合终态：合成模型、真实临时归档，不访问私人数据或外部服务。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent.session import AgentSession
from agent.loop import AgentLoop
from openai.types.chat import ChatCompletionMessage
from agent.chronicle import ChronicleManager
from agent.protocol import Event, EventType, LoopResult, ModelConfig
from agent.state import Session
from agent.session_manager import SessionManager
from web.adapter import ConnectionManager, WebAdapter
from web.server import create_app
from web.settings import WebSettings
from runtime.maintenance import MaintenanceController
from runtime.maintenance_protocol import OPERATIONS
from tools import maintenance


@pytest.fixture
def rig(tmp_path, monkeypatch):
    active = SimpleNamespace(session_id="synthetic", start_time=1, history=[])
    agent = AgentSession.__new__(AgentSession)
    agent.platform, agent._system_prompt = "web", "synthetic"
    agent._lock = asyncio.Lock()
    agent.chronicle = ChronicleManager(str(tmp_path / "chronicle.db"))
    agent.session_manager = SimpleNamespace(
        maybe_compress_daily=AsyncMock(), get_or_create_active_session=lambda: active,
        record_input_time=Mock(), context_tokens=0, save_session=Mock(),
    )
    agent._save_rollback_state = Mock()
    agent._clear_rollback_state = Mock()
    monkeypatch.setattr('agent.session.FileManager', lambda: SimpleNamespace(configured=False))
    payloads, order = [], []

    async def broadcast(payload):
        payloads.append(payload)
        order.append(payload["type"])

    settings = WebSettings(str(tmp_path / "settings.json"))
    adapter = WebAdapter(agent, SimpleNamespace(broadcast=broadcast), settings=settings)
    agent.callback = adapter._callback

    async def run(messages, context):
        await agent._on_event(Event(EventType.TEXT_DELTA, {"content": "synthetic reply"}, context))
        await agent._on_event(Event(EventType.FINAL_ANSWER, {"content": "synthetic reply"}, context))
        return LoopResult([*messages, {"role": "assistant", "content": "synthetic reply"}],
                          "synthetic reply", {})

    agent._loop = SimpleNamespace(run=run)
    app = create_app(None, adapter, agent, None, None, agent.chronicle, settings, None)
    return SimpleNamespace(agent=agent, active=active, adapter=adapter, payloads=payloads, order=order, app=app)


def terminal(rig):
    events = [p["data"] for p in rig.payloads if p["type"] == "round_finished"]
    assert len(events) == 1
    return events[0]


@pytest.mark.parametrize("failure", ["api_error", "empty_response", "cancel"])
async def test_tool_batch_is_on_disk_before_followup_and_never_replayed(rig, tmp_path, failure):
    agent, active = rig.agent, rig.active
    active.history = [None, {"role": "assistant", "content": None}, {"role": "user", "content": "old input"}]
    path = tmp_path / "synthetic-active.json"
    agent.session_manager.save_session.side_effect = lambda: SessionManager._write_cache({"history": active.history}, path)
    executed = AsyncMock(return_value="confirmed saved")
    loop = object.__new__(AgentLoop)
    loop.model_config = ModelConfig("synthetic", "https://model.invalid", provider="deepseek", stream=False)
    loop.client = SimpleNamespace(context_cache=None)
    loop.callback = agent._on_event
    loop.tool_executor = SimpleNamespace(execute=executed)
    loop.tool_definitions, loop.checkpoint = [], None
    agent._loop = loop
    waiting = asyncio.Event()
    calls = []

    async def call(**kwargs):
        calls.append(kwargs["full_messages"][:])
        if len(calls) == 1:
            message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[{
                "id": "synthetic-save", "type": "function",
                "function": {"name": "synthetic_save", "arguments": "{}"},
            }])
        elif len(calls) == 2:
            saved = json.loads(path.read_text(encoding="utf-8"))["history"]
            assert saved[-1] == {"role": "tool", "tool_call_id": "synthetic-save", "content": "confirmed saved"}
            assert all(msg is not None for msg in saved)
            if failure == "api_error":
                raise RuntimeError("synthetic API failure")
            if failure == "cancel":
                waiting.set()
                await asyncio.Event().wait()
            message = ChatCompletionMessage(role="assistant", content=None)
        else:
            assert any(msg.get("tool_call_id") == "synthetic-save" for msg in kwargs["full_messages"])
            message = ChatCompletionMessage(role="assistant", content="recovered")
        return SimpleNamespace(message=message, usage=None, web_search_calls=[])

    loop.dialect = SimpleNamespace(call=call)
    task = asyncio.create_task(agent.run_round("synthetic request"))
    if failure == "cancel":
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.stop_reason == "error"
    assert loop.checkpoint is None
    # 模拟进程恢复只读取刚才保存的合成缓存，下一轮不重新执行工具。
    active.history = json.loads(path.read_text(encoding="utf-8"))["history"]
    result = await agent.run_round("continue")
    assert result.final_answer == "recovered"
    executed.assert_awaited_once()
    assert not any(msg.get("role") == "assistant" and not msg.get("tool_calls") and msg.get("content") is None
                   for msg in active.history)


async def test_finish_follows_all_saves_and_http_uses_identical_outcome(rig, monkeypatch):
    agent = rig.agent
    agent.session_manager.save_session.side_effect = lambda: rig.order.append("save_history")
    append = agent.chronicle.append_messages

    def archive(*args, **kwargs):
        rig.order.append("save_archive")
        append(*args, **kwargs)

    monkeypatch.setattr(agent.chronicle, "append_messages", archive)
    agent._save_rollback_state.side_effect = lambda state: rig.order.append("save_rollback")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url="https://test") as client:
        response = await client.post("/api/chat/send", json={"text": "synthetic", "request_id": "req-1"})
    assert response.status_code == 200
    assert rig.order == ["message_delta", "final", "save_history", "save_archive", "save_rollback", "round_finished"]
    outcome = terminal(rig)
    assert response.json()["round"] == outcome
    assert outcome["status"] == "completed" and outcome["request_id"] == "req-1"
    assert {m["round_id"] for m in agent.chronicle.get_recent_rounds(limit=1)} == {outcome["round_id"]}
    assert all(p["round"]["round_id"] == outcome["round_id"] for p in rig.payloads)


@pytest.mark.parametrize("stop_reason", ["complete", "cancelled", "error", "waiting_input"])
async def test_agent_maintenance_hands_off_only_after_normal_terminal_event(rig, tmp_path, monkeypatch, stop_reason):
    agent = rig.agent
    status = tmp_path / "host-status"
    status.mkdir()
    (status / "version.json").write_text('{"version":"v1.7"}')
    controller = MaintenanceController(agent, status, SimpleNamespace(is_socket=lambda: True))
    agent._restarting = False
    agent.maintenance = controller
    monkeypatch.setattr(maintenance, "_controller", controller)
    terminal_started, terminal_release, submitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_callback = agent.callback

    async def callback(event):
        if event.type == EventType.ROUND_FINISHED:
            assert not agent._lock.locked()
            terminal_started.set()
            await terminal_release.wait()
        await original_callback(event)

    async def run(messages, context):
        reply = json.loads(await maintenance.maintenance({"action": "restart"}))
        assert reply["operation"]["state"] == "waiting"
        assert agent._restarting
        await agent._on_event(Event(EventType.FINAL_ANSWER, {"content": "synthetic reply"}, context))
        return LoopResult(messages, "synthetic reply", {}, stop_reason=stop_reason)

    async def submit(request):
        assert rig.order[-1] == "round_finished"
        agent.session_manager.save_session.assert_called()
        agent._save_rollback_state.assert_called()
        assert controller.snapshot(request["id"])["operation"]["state"] == "submitting"
        (status / OPERATIONS).write_text(json.dumps([{**request, "state": "succeeded"}]))
        submitted.set()

    agent.callback = callback
    agent._loop.run = run
    monkeypatch.setattr(controller, "_submit", submit)
    task = asyncio.create_task(agent.run_round("synthetic request"))
    try:
        await asyncio.wait_for(terminal_started.wait(), 2)
        assert not submitted.is_set()
        terminal_release.set()
        await asyncio.wait_for(task, 2)
        if stop_reason == "complete":
            await asyncio.wait_for(submitted.wait(), 2)
            assert controller.snapshot()["operation"]["state"] == "succeeded"
        else:
            assert controller.snapshot()["operation"]["state"] == "cancelled"
            assert not submitted.is_set()
        assert not agent._restarting
    finally:
        terminal_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await controller.close()


@pytest.mark.parametrize("stage", ["history", "archive", "rollback", "prepare"])
async def test_save_or_preparation_failure_never_reports_success(rig, monkeypatch, stage):
    error = OSError(f"synthetic {stage} failure")
    if stage == "history":
        rig.agent.session_manager.save_session.side_effect = error
    elif stage == "archive":
        monkeypatch.setattr(rig.agent.chronicle, "append_messages", Mock(side_effect=error))
    elif stage == "rollback":
        rig.agent._save_rollback_state.side_effect = error
    else:
        monkeypatch.setattr(rig.agent, "_prepare_user_media", AsyncMock(side_effect=error))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url="https://test") as client:
        response = await client.post("/api/chat/send", json={"text": "synthetic", "request_id": "req-fail"})
    assert response.status_code == 500 and response.json()["ok"] is False
    outcome = terminal(rig)
    assert outcome == response.json()["round"]
    assert outcome["status"] == "failed" and outcome["error"] == str(error)
    assert outcome["request_id"] == "req-fail" and rig.agent._pending_user_rounds == 0


async def test_model_error_return_is_failed_in_http_and_ws(rig):
    async def run(messages, context):
        await rig.agent._on_event(Event(EventType.ERROR, {"error": "synthetic model failure"}, context))
        return LoopResult(messages, "", {}, stop_reason="error", error="synthetic model failure")

    rig.agent._loop.run = run
    async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url="https://test") as client:
        response = await client.post("/api/chat/send", json={"text": "synthetic", "request_id": "req-model"})
    assert response.status_code == 500
    assert response.json()["round"] == terminal(rig)
    assert terminal(rig)["stop_reason"] == "error"


async def test_command_has_same_terminal_without_model_or_archive(rig):
    rig.agent._loop.run = AsyncMock(side_effect=AssertionError("must not call model"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url="https://test") as client:
        response = await client.post("/api/chat/send", json={"text": "/help", "request_id": "req-command"})
    assert response.status_code == 200
    outcome = terminal(rig)
    assert outcome == response.json()["round"]
    assert outcome["stop_reason"] == "command" and outcome["command"] == response.json()["command"]
    assert rig.order == ["round_finished"]
    assert rig.agent.chronicle.get_recent_rounds() == []


async def test_cancel_while_waiting_for_lock_produces_own_terminal(rig):
    await rig.agent._lock.acquire()
    task = asyncio.create_task(rig.agent.run_round("synthetic", request_id="cancelled-request"))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert terminal(rig)["status"] == "cancelled"
        assert error.value.round_status == terminal(rig)
        assert rig.agent._pending_user_rounds == 0
    finally:
        rig.agent._lock.release()


async def test_command_does_not_flush_concurrent_heartbeat_stream_or_steal_its_identity(rig):
    started, resume = asyncio.Event(), asyncio.Event()

    async def run(messages, context):
        await rig.agent._on_event(Event(EventType.TEXT_DELTA, {"content": "first"}, context))
        started.set()
        await resume.wait()
        await rig.agent._on_event(Event(EventType.TEXT_DELTA, {"content": "second"}, context))
        await rig.agent._on_event(Event(EventType.FINAL_ANSWER, {"content": "firstsecond"}, context))
        return LoopResult([*messages, {"role": "assistant", "content": "firstsecond"}], "firstsecond", {})

    rig.agent._loop.run = run
    beat = asyncio.create_task(rig.agent.run_round(source="heartbeat"))
    await asyncio.wait_for(started.wait(), 2)
    command = await rig.agent.run_round("/help", "harmony", request_id="command-during-heartbeat")
    assert rig.adapter._stream_buf == "first"
    resume.set()
    heartbeat = await asyncio.wait_for(beat, 2)
    assert command.round_status["round_id"] != heartbeat.round_status["round_id"]
    assert command.round_status["source"] == "user" and heartbeat.round_status["source"] == "heartbeat"
    assert heartbeat.round_status["request_id"] is None
    deltas = [p for p in rig.payloads if p["type"] == "message_delta"]
    assert {p["round"]["round_id"] for p in deltas} == {heartbeat.round_status["round_id"]}
    assert rig.agent.chronicle.get_recent_rounds(limit=1)[-1]["content"] == "firstsecond"


async def test_ws_terminal_delivery_failure_leaves_successful_http_result(rig, monkeypatch):
    callback = rig.agent.callback

    async def fail_terminal(event):
        if event.type == EventType.ROUND_FINISHED:
            raise OSError("synthetic disconnected socket")
        await callback(event)

    monkeypatch.setattr(rig.agent, "callback", fail_terminal)
    result = await rig.agent.run_round("synthetic", request_id="rest-fallback")
    assert result.round_status["status"] == "completed"
    assert rig.agent.chronicle.get_recent_rounds(limit=1)[-1]["content"] == "synthetic reply"


def install_interrupt_loop(rig, call, *, stream=True):
    loop = AgentLoop.__new__(AgentLoop)
    loop.model_config = ModelConfig("synthetic", "https://model.invalid", stream=stream)
    loop.client = SimpleNamespace(context_cache=None)
    loop.callback = rig.agent._on_event
    loop.dialect = SimpleNamespace(call=AsyncMock(side_effect=call))
    loop.tool_definitions, loop.tool_executor, loop.checkpoint = [], None, None
    rig.agent._loop = loop
    return loop


@pytest.mark.parametrize("stream,partial", [(True, "partial synthetic text"), (True, ""), (False, "")])
async def test_explicit_stop_saves_partial_before_terminal_and_does_not_stop_next_round(rig, stream, partial):
    started, closed = asyncio.Event(), asyncio.Event()

    async def call(**kwargs):
        if len(loop.dialect.call.call_args_list) > 1:
            return SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="new answer"),
                                   usage=None, web_search_calls=[])
        if partial:
            await kwargs["emit"](Event(EventType.TEXT_DELTA, {"content": partial}, kwargs["context"]))
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    loop = install_interrupt_loop(rig, call, stream=stream)

    def save():
        assert closed.is_set()
        rig.order.append("save_history")

    rig.agent.session_manager.save_session.side_effect = save
    async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url="https://test") as client:
        task = asyncio.create_task(client.post("/api/chat/send", json={"text": "synthetic", "request_id": "stop-1"}))
        await asyncio.wait_for(started.wait(), 2)
        assert (await client.post("/api/chat/stop", json={})).status_code == 400
        assert not (await client.post("/api/chat/stop", json={"request_id": "old"})).json()["accepted"]
        first = (await client.post("/api/chat/stop", json={"request_id": "stop-1"})).json()
        assert first["accepted"] and first["state"] == "stopping"
        # 重复点击可接受；也可能已经进入保存或结束，但不能创建第二个回合。
        await client.post("/api/chat/stop", json={"round_id": first["round_id"]})
        response = await asyncio.wait_for(task, 2)
        assert response.status_code == 200 and response.json()["ok"] is False
        assert response.json()["round"] == terminal(rig)
        assert terminal(rig)["status"] == terminal(rig)["stop_reason"] == "cancelled"
        assert rig.order.index("save_history") < rig.order.index("round_finished")
        archived = (await client.get("/api/chat/history?limit=1")).json()["messages"]
        replies = [m for m in archived if m["role"] == "assistant"]
        assert [m["content"] for m in replies] == ([partial] if partial else [])
        assert all(m["interrupted"] is True for m in replies)
        canonical = [m for m in rig.active.history if m["role"] == "assistant"]
        assert [m["content"] for m in canonical] == ([partial] if partial else [])
        assert "已被用户中断" in rig.active.history[-1]["content"]
        assert rig.agent._round_controls == {} and rig.agent._pending_user_rounds == 0
        # 迟到的停止不影响新回合，也不重放上一回合。
        assert not (await client.post("/api/chat/stop", json={"round_id": first["round_id"]})).json()["accepted"]
        next_response = await client.post("/api/chat/send", json={"text": "continue", "request_id": "stop-2"})
        assert next_response.json()["round"]["status"] == "completed"
        assert rig.active.history[-1]["content"] == "new answer"


@pytest.mark.parametrize("source", ["user", "heartbeat"])
async def test_explicit_stop_waits_for_current_tool_skips_rest_and_keeps_pairs(rig, source):
    started, finish = asyncio.Event(), asyncio.Event()

    async def call(**kwargs):
        await kwargs["emit"](Event(EventType.TEXT_DELTA, {"content": "before tools"}, kwargs["context"]))
        return SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="before tools", tool_calls=[
            {"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for name in ("first", "second")
        ]), usage=None, web_search_calls=[])

    async def execute(name, args):
        assert name == "first"
        started.set()
        await finish.wait()
        return "confirmed result"

    loop = install_interrupt_loop(rig, call)
    loop.tool_executor = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    task = asyncio.create_task(rig.agent.run_round("synthetic", source=source, request_id="tools-stop"))
    await asyncio.wait_for(started.wait(), 2)
    assert rig.agent.request_stop(request_id="tools-stop")["accepted"]
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    result = await asyncio.wait_for(task, 2)
    assert result.round_status["status"] == "cancelled"
    loop.tool_executor.execute.assert_awaited_once_with("first", {})
    loop.dialect.call.assert_awaited_once()
    tools = [m for m in rig.active.history if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["first", "second"]
    assert tools[0]["content"] == "confirmed result"
    assert json.loads(tools[1]["content"])["status"] == "not_executed"
    assert [m["content"] for m in rig.active.history if m["role"] == "assistant"] == ["before tools"]
    assert rig.agent.chronicle.get_recent_rounds(limit=1)[1]["interrupted"]


async def test_stop_queued_round_finishes_without_acquiring_or_releasing_another_rounds_lock(rig):
    rig.agent._loop.run = AsyncMock()
    await rig.agent._lock.acquire()
    try:
        task = asyncio.create_task(rig.agent.run_round("synthetic", request_id="queued-stop"))
        await asyncio.sleep(0)
        assert rig.agent.request_stop(request_id="queued-stop")["accepted"]
        result = await asyncio.wait_for(task, 2)
        assert result.round_status["status"] == "cancelled"
        assert rig.agent._lock.locked()
        rig.agent._loop.run.assert_not_awaited()
        assert rig.agent.chronicle.get_recent_rounds(limit=1) == []
        assert rig.agent._round_controls == {}
    finally:
        rig.agent._lock.release()


async def test_stop_requires_unambiguous_request_and_matching_round_id(rig):
    await rig.agent._lock.acquire()
    tasks = [asyncio.create_task(rig.agent.run_round("synthetic", request_id="same-id")) for _ in range(2)]
    try:
        await asyncio.sleep(0)
        assert rig.agent.request_stop(request_id="same-id") == {"accepted": False, "state": "ambiguous"}
        ids = list(rig.agent._round_controls)
        assert not rig.agent.request_stop(round_id=ids[0], request_id="wrong")["accepted"]
        assert rig.agent.request_stop(round_id=ids[0], request_id="same-id")["accepted"]
        assert (await asyncio.wait_for(tasks[0], 2)).stop_reason == "cancelled"
        assert not tasks[1].done()
    finally:
        rig.agent._lock.release()
        await asyncio.gather(*tasks)


async def test_stop_after_final_does_not_cancel_saving(rig, monkeypatch):
    saving, finish = asyncio.Event(), asyncio.Event()
    original = rig.agent._update_history_and_archive

    async def save(*args, **kwargs):
        saving.set()
        await finish.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(rig.agent, "_update_history_and_archive", save)
    task = asyncio.create_task(rig.agent.run_round("synthetic", request_id="saving"))
    await asyncio.wait_for(saving.wait(), 2)
    stopped = rig.agent.request_stop(request_id="saving")
    assert not stopped["accepted"] and stopped["state"] == "finishing"
    finish.set()
    assert (await asyncio.wait_for(task, 2)).round_status["status"] == "completed"


async def test_stop_does_not_hide_archive_failure(rig, monkeypatch):
    started = asyncio.Event()

    async def call(**kwargs):
        await kwargs["emit"](Event(EventType.TEXT_DELTA, {"content": "partial"}, kwargs["context"]))
        started.set()
        await asyncio.Event().wait()

    install_interrupt_loop(rig, call)
    monkeypatch.setattr(rig.agent.chronicle, "append_messages", Mock(side_effect=OSError("synthetic full disk")))
    task = asyncio.create_task(rig.agent.run_round("synthetic", request_id="failed-save"))
    await asyncio.wait_for(started.wait(), 2)
    rig.agent.request_stop(request_id="failed-save")
    with pytest.raises(OSError, match="full disk"):
        await task
    assert terminal(rig)["status"] == "failed"
    assert terminal(rig)["error"] == "synthetic full disk"


async def test_stop_on_later_iteration_keeps_previous_tool_checkpoint_and_both_visible_messages(rig):
    started = asyncio.Event()

    async def call(**kwargs):
        if loop.dialect.call.await_count == 1:
            await kwargs["emit"](Event(EventType.TEXT_DELTA, {"content": "first message"}, kwargs["context"]))
            return SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="first message", tool_calls=[{
                "id": "saved", "type": "function", "function": {"name": "save", "arguments": "{}"},
            }]), usage=None, web_search_calls=[])
        assert rig.active.history[-1]["content"] == "confirmed saved"
        await kwargs["emit"](Event(EventType.TEXT_DELTA, {"content": "second partial"}, kwargs["context"]))
        started.set()
        await asyncio.Event().wait()

    loop = install_interrupt_loop(rig, call)
    loop.tool_executor = SimpleNamespace(execute=AsyncMock(return_value="confirmed saved"))
    task = asyncio.create_task(rig.agent.run_round("synthetic", request_id="later-stop"))
    await asyncio.wait_for(started.wait(), 2)
    rig.agent.request_stop(request_id="later-stop")
    await asyncio.wait_for(task, 2)
    replies = [m for m in rig.agent.chronicle.get_recent_rounds() if m["role"] == "assistant"]
    assert [m["content"] for m in replies] == ["first message", "second partial"]
    assert not replies[0].get("interrupted") and replies[1]["interrupted"]
    assert [m["content"] for m in rig.active.history if m["role"] == "assistant"] == ["first message", "second partial"]
    loop.tool_executor.execute.assert_awaited_once()


async def test_stop_endpoint_uses_chat_authentication(rig):
    # 配置只操作合成临时设置，不读取本机认证文件。
    rig.adapter._settings.set_password("synthetic password")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url="https://test") as client:
        response = await client.post("/api/chat/stop", json={"request_id": "synthetic"})
        assert response.status_code == 401
        assert (await client.get("/api/chat/sync")).status_code == 401


async def test_reconnect_sync_restores_live_output_and_stop_target_then_archived_result(rig):
    started = asyncio.Event()

    async def call(**kwargs):
        await kwargs["emit"](Event(EventType.THINKING_DELTA, {"content": "synthetic thought"}, kwargs["context"]))
        for text in ("partial", " reply"):
            await kwargs["emit"](Event(EventType.TEXT_DELTA, {"content": text}, kwargs["context"]))
        started.set()
        await asyncio.Event().wait()

    loop = install_interrupt_loop(rig, call)
    rig.agent._system_prompt = "synthetic internal system context"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url="https://test") as client:
        task = asyncio.create_task(rig.agent.run_round("visible user input", request_id="reconnect"))
        await asyncio.wait_for(started.wait(), 2)
        snapshot = (await client.get("/api/chat/sync")).json()
        assert snapshot["messages"] == [] and len(snapshot["rounds"]) == 1
        live = snapshot["rounds"][0]
        assert live["request_id"] == "reconnect" and live["phase"] == "running" and not live["stopping"]
        assert live["input"]["content"] == "visible user input"
        assert "synthetic internal system context" not in json.dumps(snapshot)
        text = [e for e in snapshot["events"] if e["type"] == "message_delta"]
        assert "".join(e["data"]["text"] for e in text) == "partial reply"
        assert all(e["chat_seq"] <= snapshot["seq"] for e in snapshot["events"])
        stopped = (await client.post("/api/chat/stop", json={"round_id": live["round_id"]})).json()
        assert stopped["accepted"]
        result = await asyncio.wait_for(task, 2)
        finished = (await client.get("/api/chat/sync")).json()
        assert finished["rounds"] == finished["events"] == []
        assert finished["finished"][-1] == result.round_status
        assert finished["seq"] > snapshot["seq"]
        assert [m["content"] for m in finished["messages"] if m["role"] == "assistant"] == ["partial reply"]
        loop.dialect.call.assert_awaited_once()


async def test_sync_during_post_archive_saving_reports_finishing_without_removing_history(rig, monkeypatch):
    saving, finish = asyncio.Event(), asyncio.Event()
    original = rig.agent._update_history_and_archive

    async def save(*args, **kwargs):
        await original(*args, **kwargs)
        saving.set()
        await finish.wait()

    monkeypatch.setattr(rig.agent, "_update_history_and_archive", save)
    task = asyncio.create_task(rig.agent.run_round("synthetic", request_id="sync-saving"))
    try:
        await asyncio.wait_for(saving.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url="https://test") as client:
            snapshot = (await client.get("/api/chat/sync")).json()
        assert snapshot["rounds"][0]["phase"] == "finishing"
        assert snapshot["messages"][-1]["content"] == "synthetic reply"
        assert snapshot["messages"][-1]["round_id"] == snapshot["rounds"][0]["round_id"]
        assert snapshot["finished"] == []
    finally:
        finish.set()
        await task


async def test_real_asgi_websocket_reconnect_snapshot_precedes_new_stream_deltas(rig):
    started, resume = asyncio.Event(), asyncio.Event()

    async def call(**kwargs):
        await kwargs["emit"](Event(EventType.TEXT_DELTA, {"content": "before disconnect"}, kwargs["context"]))
        started.set()
        await resume.wait()
        await kwargs["emit"](Event(EventType.TEXT_DELTA, {"content": " after reconnect"}, kwargs["context"]))
        await asyncio.Event().wait()

    install_interrupt_loop(rig, call)
    connections = ConnectionManager()
    rig.adapter.conn_manager = connections
    app = create_app(connections, rig.adapter, rig.agent, None, None, rig.agent.chronicle, rig.adapter._settings, None)
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    scope = {"type": "websocket", "path": "/ws/chat", "raw_path": b"/ws/chat", "root_path": "",
             "scheme": "wss", "server": ("test", 443), "client": ("test", 5000),
             "query_string": b"", "headers": [], "subprotocols": []}
    task = asyncio.create_task(rig.agent.run_round("synthetic", request_id="socket-reconnect"))
    await asyncio.wait_for(started.wait(), 2)
    socket = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    try:
        await incoming.put({"type": "websocket.connect"})
        assert (await asyncio.wait_for(outgoing.get(), 2))["type"] == "websocket.accept"
        hello = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
        assert hello["type"] == "hello"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://test") as c:
            snapshot = (await c.get("/api/chat/sync")).json()
            assert snapshot["events"][-1]["data"]["text"] == "before disconnect"
            resume.set()
            delta = json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])
            assert delta["data"]["text"] == " after reconnect"
            assert delta["chat_seq"] > snapshot["seq"] and delta["chat_instance"] == snapshot["instance_id"]
            await c.post("/api/chat/stop", json={"round_id": snapshot["rounds"][0]["round_id"]})
            result = await asyncio.wait_for(task, 2)
            assert result.round_status["status"] == "cancelled"
            assert [m["content"] for m in rig.active.history if m["role"] == "assistant"] == ["before disconnect after reconnect"]
    finally:
        rig.agent.request_stop(request_id="socket-reconnect")
        await asyncio.gather(task, return_exceptions=True)
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(socket, 2)
    assert connections.count() == 0


def test_session_save_is_atomic_and_propagates_write_failure(tmp_path, monkeypatch):
    manager = SessionManager.__new__(SessionManager)
    manager.cache_path = tmp_path / "session.json"
    manager._current_session = Session("synthetic", 1, history=[{"role": "user", "content": "new"}])
    manager._context_tokens, manager._context_warning_sent = 0, False
    manager.cache_path.write_text('{"history":"previous"}', encoding="utf-8")
    monkeypatch.setattr('agent.session_manager.os.replace', Mock(side_effect=OSError("synthetic full disk")))
    with pytest.raises(OSError, match="full disk"):
        manager.save_session()
    assert json.loads(manager.cache_path.read_text(encoding="utf-8")) == {"history": "previous"}
    assert list(tmp_path.glob(".session-*")) == []


def test_archive_write_failure_is_not_silently_successful(tmp_path, monkeypatch):
    chronicle = ChronicleManager(str(tmp_path / "archive.db"))
    monkeypatch.setattr('agent.chronicle.sqlite3.connect', Mock(side_effect=OSError("synthetic full disk")))
    with pytest.raises(OSError, match="full disk"):
        chronicle.append_messages("synthetic", [{"role": "user", "content": "synthetic"}])
