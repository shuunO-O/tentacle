import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.session import AgentSession
from agent.commands import run_command
from agent.compressor import Compressor
from agent.protocol import Event, EventType, LoopResult
from agent.session_manager import SessionManager
from web.adapter import WebAdapter


@pytest.fixture
def session_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(Compressor, "_load_system_prompt", lambda self: "synthetic prompt")
    return make_manager(tmp_path)


def make_manager(tmp_path):
    config = SimpleNamespace(_raw_config={
        "providers": {"p": {"type": "openai"}},
        "models": {"m": {"provider": "p", "name": "gpt-4o-mini", "context_window": 1000}},
        "roles": {"chat": {"model": "m"}},
    })
    return SessionManager(
        config,
        db_path=str(tmp_path / "chronicle.db"),
        cache_path=str(tmp_path / "active_session.json"),
        compression_state_path=str(tmp_path / "compression_state.json"),
    )


@pytest.mark.parametrize("age_days", [0, 1, 2, 3, 8])
async def test_threshold_only_warns_and_keeps_session(session_manager, age_days):
    manager = session_manager
    threshold = 400
    session = manager.get_or_create_active_session()
    session.start_time = time.time() - age_days * 86400
    session.history.append({"role": "user", "content": "synthetic conversation"})
    history = list(session.history)
    on_inactive = AsyncMock()
    manager.set_on_session_inactive(on_inactive)

    assert await manager.add_usage(threshold - 1) is None
    warning = await manager.add_usage(threshold)

    assert "/session" in warning
    assert f"{threshold / 10:.1f}%" in warning
    # 即使继续增长到超过窗口，也不能再自动切换或每轮刷提醒。
    assert await manager.add_usage(1100) is None
    assert manager.current_session is session
    assert session.history == history
    assert session.status == "active"
    assert manager.context_tokens == 1100
    assert manager.chronicle.get_active_session()["session_id"] == session.session_id
    assert manager.chronicle.get_session(session.session_id)["end_time"] is None
    on_inactive.assert_not_awaited()
    cache = json.loads(manager.cache_path.read_text())
    assert cache["history"] == history
    assert cache["context_warning_sent"] is True


async def test_warning_survives_restart_and_manual_command_resets_it(session_manager, tmp_path):
    manager = session_manager
    old_session = manager.get_or_create_active_session()
    assert await manager.add_usage(800)

    SessionManager._instance = None
    restored = make_manager(tmp_path)
    assert restored.current_session.session_id == old_session.session_id
    assert await restored.add_usage(900) is None

    agent = AgentSession.__new__(AgentSession)
    agent._lock = asyncio.Lock()
    agent._system_prompt = "synthetic prompt"
    agent._rollback_state_path = tmp_path / "rollback.json"
    agent.session_manager = restored
    cache = SimpleNamespace(clear=AsyncMock())
    agent._loop = SimpleNamespace(client=SimpleNamespace(context_cache=cache))
    # 固定新 ID，避免随机 ID 与旧 ID 碰撞导致偶发失败。
    restored._generate_session_id = lambda: "sess_manual"

    assert await run_command("session", agent) == "[命令] 已切换 session，新 session: sess_manual"
    cache.clear.assert_awaited_once()
    assert restored.chronicle.get_session(old_session.session_id)["status"] == "inactive"
    assert restored.context_tokens == 0
    assert not json.loads(restored.cache_path.read_text())["context_warning_sent"]
    assert await restored.add_usage(800)


async def test_legacy_cache_gets_first_warning(session_manager, tmp_path):
    manager = session_manager
    session = manager.get_or_create_active_session()
    await manager.add_usage(800)
    cache = json.loads(manager.cache_path.read_text())
    del cache["context_warning_sent"]
    manager.cache_path.write_text(json.dumps(cache))
    SessionManager._instance = None

    restored = make_manager(tmp_path)
    assert restored.current_session.session_id == session.session_id
    assert await restored.add_usage(800)


async def test_rollback_does_not_repeat_warning(session_manager):
    manager = session_manager
    session = manager.get_or_create_active_session()
    await manager.add_usage(800)
    manager.restore_session(session.session_id, session.start_time, session.history, 700)

    assert await manager.add_usage(800) is None


@pytest.mark.parametrize("source", ["web", "qq", "heartbeat", "subagent"])
async def test_round_keeps_history_and_cache_on_warning(session_manager, source):
    manager = session_manager
    session = manager.get_or_create_active_session()
    agent = AgentSession.__new__(AgentSession)
    agent.session_manager = manager
    agent.chronicle = manager.chronicle
    agent.platform = "web"
    agent.callback = AsyncMock()
    agent._clear_gemini_cache = AsyncMock()
    messages = [{"role": "user", "content": "synthetic input"},
                {"role": "assistant", "content": "synthetic output"}]
    result = LoopResult(
        full_messages=[{"role": "system", "content": "synthetic prompt"}, *messages],
        final_answer="synthetic output",
        total_usage={"total_tokens": 810, "context_prompt_tokens": 800},
    )

    await agent._update_history_and_archive(session, result, messages, source, round_id="test-round")

    assert manager.current_session is session
    assert session.history == messages
    event = agent.callback.call_args.args[0]
    assert event.type == EventType.NOTICE
    assert "/session" in event.data["content"]
    agent._clear_gemini_cache.assert_not_awaited()
    archived = manager.chronicle.get_recent_rounds(session_id=session.session_id, limit=10)
    assert [m["content"] for m in archived] == [m["content"] for m in messages]


async def test_notice_broadcasts_as_system_message():
    adapter = WebAdapter(None, SimpleNamespace(broadcast=AsyncMock()), settings=SimpleNamespace())

    await adapter._callback(Event(type=EventType.NOTICE, data={"content": "synthetic warning"}))

    adapter.conn_manager.broadcast.assert_awaited_once_with({
        "type": "message", "data": {"text": "synthetic warning", "platform": "system"},
    })
