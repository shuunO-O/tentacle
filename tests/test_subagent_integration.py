"""与主循环和唯一叙事流的模拟集成；不启动应用或调用真实模型。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openai.types.chat import ChatCompletionMessage

from agent.protocol import LoopResult
from agent.session import AgentSession
from runtime.heartbeat import Heartbeat
from runtime.tool_registry import registry


def tool_message(name, arguments, call_id="call-one"):
    return ChatCompletionMessage(role="assistant", content=None, tool_calls=[{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
    }])


def adapter_result(message):
    return SimpleNamespace(message=message, usage=None, web_search_calls=[])


def make_session(tmp_path, monkeypatch):
    import agent.session as module

    agent = AgentSession.__new__(AgentSession)
    agent._lock = asyncio.Lock()
    agent.platform = "web"
    agent._system_prompt = "synthetic main prompt"
    agent._rollback_state_path = tmp_path / "rollback.json"
    current = SimpleNamespace(session_id="main-session", history=[{"role": "user", "content": "existing conversation"}])
    agent.session_manager = SimpleNamespace(maybe_compress_daily=AsyncMock(), get_or_create_active_session=lambda: current)
    agent._update_history_and_archive = AsyncMock()
    calls = []

    async def run(messages, context):
        calls.append((messages, context))
        messages.append({"role": "assistant", "content": "main handled the report"})
        return LoopResult(messages, "main handled the report", {})

    agent._loop = SimpleNamespace(run=run)
    monkeypatch.setattr(module, "FileManager", lambda: SimpleNamespace(configured=False))
    return agent, calls


async def test_result_wakes_main_with_distinct_prefix_and_resets_heartbeat(tmp_path, monkeypatch):
    session, calls = make_session(tmp_path, monkeypatch)
    heartbeat = Heartbeat(session, state_file=tmp_path / 'runtime.heartbeat.json')
    session.heartbeat = heartbeat
    heartbeat._next_beat_time = 0
    claim = AsyncMock(return_value=True)
    session._rollback_state_path.write_text("synthetic checkpoint")
    event = {"task_id": "task-one", "id": 1, "status": "completed", "result": "verified child evidence", "platform": "qq"}
    assert await session.run_subagent_result(event, claim)
    claim.assert_awaited_once()
    assert heartbeat._timer_generation == 1
    assert heartbeat._next_beat_time > 0
    assert len(calls) == 1
    messages, context = calls[0]
    assert context.current_message == "subagent"
    assert context.platform == "qq"
    assert "[system][subagent]" in messages[2]["content"]
    assert "verified child evidence" in messages[2]["content"]
    assert "existing conversation" in messages[1]["content"]
    assert not session._rollback_state_path.exists()
    assert session._update_history_and_archive.call_args.args[-1] == "subagent"


async def test_claim_is_checked_inside_main_lock_and_cancelled_event_does_not_wake(tmp_path, monkeypatch):
    session, calls = make_session(tmp_path, monkeypatch)
    heartbeat = Heartbeat(session, state_file=tmp_path / 'runtime.heartbeat.json')
    session.heartbeat = heartbeat
    claim = AsyncMock(return_value=False)
    await session._lock.acquire()
    task = asyncio.create_task(session.run_subagent_result({"task_id": "one"}, claim))
    await asyncio.sleep(0)
    claim.assert_not_awaited()
    session._lock.release()
    assert await task is False
    assert calls == []
    assert heartbeat._timer_generation == 0


async def test_queued_heartbeat_is_suppressed_after_timer_reset(tmp_path, monkeypatch):
    session, calls = make_session(tmp_path, monkeypatch)
    heartbeat = Heartbeat(session, state_file=tmp_path / 'runtime.heartbeat.json')
    session.heartbeat = heartbeat
    generation = heartbeat._timer_generation
    await session._lock.acquire()
    task = asyncio.create_task(session.run_round(source="heartbeat", injection_messages=[{"role": "user", "content": "synthetic heartbeat"}], trigger_time=1, should_run=lambda: heartbeat._timer_generation == generation))
    await asyncio.sleep(0)
    heartbeat.reset_timer()
    session._lock.release()
    assert (await task).stop_reason == "skipped"
    assert calls == []


async def test_heartbeat_loop_does_not_overwrite_a_reset(tmp_path, monkeypatch):
    heartbeat = Heartbeat(None, interval_minutes=1, state_file=tmp_path / 'runtime.heartbeat.json')
    heartbeat._running = True
    heartbeat._next_beat_time = 0
    deadline = []

    async def beat(trigger_time, expected_generation=None):
        heartbeat.reset_timer()
        deadline.append(heartbeat._next_beat_time)
        heartbeat._running = False

    monkeypatch.setattr(heartbeat, "_beat_once", beat)
    monkeypatch.setattr('runtime.heartbeat.asyncio.sleep', AsyncMock())
    await heartbeat._loop()
    assert heartbeat._next_beat_time == deadline[0]


async def test_scheduler_cannot_delegate_even_when_called_from_main(tmp_path):
    from runtime.scheduler import Scheduler, validate_actions

    mutation = AsyncMock(return_value='{"task_id":"must-not-create"}')
    registry.register("subagent_create", "synthetic", {}, mutation, is_async=True)
    action = {"type": "tool", "name": "subagent_create", "args": {"objective": "one"}}
    assert "不允许" in validate_actions([action])
    scheduler = Scheduler(db_path=str(tmp_path / "scheduler.db"))
    _, error = await scheduler._run_actions({"actions": [action]})
    assert error
    mutation.assert_not_awaited()
