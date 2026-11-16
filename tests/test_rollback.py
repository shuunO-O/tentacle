import asyncio
import json
import sqlite3

import pytest

from agent.session import AgentSession
from agent.chronicle import ChronicleManager
from agent.commands import run_command
from agent.state import Session


class FakeSessionManager:
    def __init__(self, current):
        self.current = current
        self.restored = None

    def get_or_create_active_session(self):
        return self.current

    def restore_session(self, session_id, start_time, history, context_tokens, history_model=None):
        self.current = Session(session_id=session_id, start_time=start_time, history=history)
        self.restored = (session_id, start_time, history, context_tokens)


def make_agent(tmp_path, chronicle, current):
    agent = AgentSession.__new__(AgentSession)
    agent._lock = asyncio.Lock()
    agent._rollback_state_path = tmp_path / "rollback_state.json"
    agent.chronicle = chronicle
    agent.session_manager = FakeSessionManager(current)
    return agent


@pytest.mark.asyncio
async def test_rollback_last_round_restores_history_and_archive(tmp_path):
    chronicle = ChronicleManager(str(tmp_path / "chronicle.db"))
    chronicle.upsert_session("sess_old", 10.0, None, "active")
    chronicle.append_messages(
        "sess_old",
        [
            {"role": "user", "content": "bad input"},
            {"role": "assistant", "content": "bad output"},
        ],
        round_id="round-1",
    )
    history_before = [{"role": "user", "content": "earlier"}]
    history_after = history_before + [
        {"role": "user", "content": "bad input"},
        {"role": "assistant", "content": "bad output"},
    ]
    agent = make_agent(
        tmp_path,
        chronicle,
        Session("sess_old", 10.0, history=history_after),
    )
    agent._save_rollback_state({
        "round_id": "round-1",
        "before_session_id": "sess_old",
        "before_start_time": 10.0,
        "history_before": history_before,
        "context_tokens_before": 123,
        "after_session_id": "sess_old",
        "after_history_digest": agent._history_digest(history_after),
    })

    result = await agent.rollback_last_round()

    assert result.startswith("已回退上一轮对话")
    assert agent.session_manager.restored == ("sess_old", 10.0, history_before, 123)
    assert not agent._rollback_state_path.exists()
    with sqlite3.connect(chronicle.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_rollback_reverses_automatic_session_switch(tmp_path):
    chronicle = ChronicleManager(str(tmp_path / "chronicle.db"))
    chronicle.upsert_session("sess_old", 10.0, 20.0, "inactive")
    chronicle.upsert_session("sess_new", 20.0, None, "active")
    chronicle.append_messages(
        "sess_old",
        [{"role": "user", "content": "bad input"}],
        round_id="round-2",
    )
    new_history = [{"role": "user", "content": "generated summary"}]
    agent = make_agent(
        tmp_path,
        chronicle,
        Session("sess_new", 20.0, history=new_history),
    )
    agent._save_rollback_state({
        "round_id": "round-2",
        "before_session_id": "sess_old",
        "before_start_time": 10.0,
        "history_before": [],
        "context_tokens_before": 50,
        "after_session_id": "sess_new",
        "after_history_digest": agent._history_digest(new_history),
    })

    await agent.rollback_last_round()

    assert chronicle.get_active_session()["session_id"] == "sess_old"
    assert chronicle.get_session("sess_new") is None


@pytest.mark.asyncio
async def test_rollback_refuses_if_history_changed(tmp_path):
    chronicle = ChronicleManager(str(tmp_path / "chronicle.db"))
    current = Session("sess_old", 10.0, history=[{"role": "user", "content": "newer"}])
    agent = make_agent(tmp_path, chronicle, current)
    agent._rollback_state_path.write_text(json.dumps({
        "round_id": "round-3",
        "before_session_id": "sess_old",
        "before_start_time": 10.0,
        "history_before": [],
        "context_tokens_before": 0,
        "after_session_id": "sess_old",
        "after_history_digest": agent._history_digest([]),
    }), encoding="utf-8")

    result = await agent.rollback_last_round()

    assert result == "无法回退：上一轮之后 session 已发生变化"
    assert agent._rollback_state_path.exists()


@pytest.mark.asyncio
async def test_rollback_command_uses_agent_session():
    class FakeAgent:
        session_manager = object()

        async def rollback_last_round(self):
            return "done"

    assert await run_command("rollback", FakeAgent()) == "[命令] done"
