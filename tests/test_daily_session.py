"""日 session 交接：真实临时文件/归档与合成模型，不调用外部服务。"""

import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.loop import current_tool_context
from agent.session import AgentSession
from agent.compressor import Compressor
from runtime.heartbeat import Heartbeat
from runtime.timezone import get_timezone
from agent.protocol import LoopResult, ModelConfig
from agent.session_manager import SessionManager
from agent.prompt import SystemPromptBuilder
from runtime.tool_registry import registry, tool_result_error
from tools import situation_notes


class FaultConnection:
    """使用真实临时 SQLite，只在指定读、写或提交环节注入故障。"""

    def __init__(self, connection, fault):
        self.connection, self.fault = connection, fault
        self.transition = False

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None and self.transition and self.fault == "commit":
            self.connection.rollback()
            raise sqlite3.OperationalError("synthetic commit failure")
        return self.connection.__exit__(kind, value, traceback)

    def execute(self, sql, parameters=()):
        if "INSERT INTO sessions" in sql:
            self.transition = True
            if self.fault == "insert":
                raise sqlite3.OperationalError("synthetic insert failure")
        if ((self.fault == "messages" and "WHERE created_time >=" in sql)
                or (self.fault == "earliest" and "MIN(created_time)" in sql)
                or (self.fault == "summaries" and "SELECT id, summary" in sql)
                or (self.fault == "recent" and "WITH grouped" in sql)):
            raise sqlite3.OperationalError("synthetic query failure")
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        if self.fault == "summary_write" and "INSERT INTO summaries" in sql:
            raise sqlite3.OperationalError("synthetic summary write failure")
        return self.connection.executemany(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def inject_database_fault(monkeypatch, fault):
    connect = sqlite3.connect
    monkeypatch.setattr('agent.chronicle.sqlite3.connect', lambda *a, **kw: FaultConnection(connect(*a, **kw), fault))


def timestamp(day, clock="03:00"):
    return datetime.fromisoformat(f"2026-10-{day}T{clock}:00").replace(tzinfo=get_timezone()).timestamp()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=timestamp(23))
    monkeypatch.setattr('runtime.heartbeat.time', SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr('agent.session_manager.time', SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(Compressor, "_load_system_prompt", lambda self: "synthetic compressor")
    sm = SessionManager(
        SimpleNamespace(_raw_config={
            "providers": {"p": {"type": "openai"}},
            "models": {"m": {"provider": "p", "name": "gpt-4o-mini", "context_window": 100000}},
            "roles": {"chat": {"model": "m"}},
        }),
        db_path=str(tmp_path / "chronicle.db"), cache_path=str(tmp_path / "active.json"),
        compression_state_path=str(tmp_path / "compression.json"),
    )
    monkeypatch.setattr(sm, "_assemble_summary_message", lambda: "synthetic summary")
    serial = iter(range(100))
    monkeypatch.setattr(sm, "_generate_session_id", lambda: f"synthetic-{next(serial)}")
    old = sm.get_or_create_active_session()
    old.start_time = timestamp(22, "12:00")
    sm.save_session()
    sm.maybe_compress_daily = AsyncMock()
    sm._last_compressed_date = datetime.fromtimestamp(clock.now, get_timezone()).date() - timedelta(days=1)
    sm._compression_state_saved = True
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    for name in ("soul", "guild", "memory", "master"):
        (prompt_dir / f"{name}.md").write_text(f"synthetic {name}", encoding="utf-8")
    builder = SystemPromptBuilder(str(prompt_dir), str(tmp_path / "skills"), str(tmp_path / "preview"))
    builder.write_situation("old synthetic situation")
    monkeypatch.setattr(situation_notes, "_builder", builder)
    situation_notes.register_tools(registry)

    agent = AgentSession.__new__(AgentSession)
    agent._lock = asyncio.Lock()
    agent._pending_user_rounds = 0
    agent.platform = "web"
    agent.callback = AsyncMock()
    agent.chronicle = sm.chronicle
    agent.session_manager = sm
    agent._system_prompt_builder = builder
    agent._system_prompt = builder.build()
    agent._rollback_state_path = tmp_path / "rollback.json"
    cache = SimpleNamespace(clear=AsyncMock())
    agent._loop = SimpleNamespace(
        client=SimpleNamespace(context_cache=cache),
        model_config=ModelConfig("synthetic", "https://model.test", provider="deepseek"),
    )
    monkeypatch.setattr('agent.session.FileManager', lambda: SimpleNamespace(configured=False))
    goals = SimpleNamespace(list_active_goals=AsyncMock(return_value=[{
        "description": "synthetic direction", "priority": "high", "tasks": [],
    }]))
    monkeypatch.setattr('runtime.heartbeat.get_goal_manager', lambda: goals)
    hb = Heartbeat(agent, state_file=tmp_path / 'runtime.heartbeat.json')
    agent.heartbeat = hb
    state = SimpleNamespace(
        agent=agent, manager=sm, old=old, builder=builder, hb=hb, clock=clock, cache=cache,
        write=True, stop_reason="complete", calls=[], arrive=False, tool_error=None,
    )

    async def run(messages, context):
        state.calls.append((messages, context))
        if state.write:
            token = current_tool_context.set((context, messages))
            try:
                answer = await registry.execute("situation_notes", {"action": "write", "content": "new synthetic situation"})
                state.tool_error = tool_result_error(answer)
            finally:
                current_tool_context.reset(token)
        if state.arrive:
            agent._pending_user_rounds += 1
        return LoopResult([*messages, {"role": "assistant", "content": "synthetic done"}],
                          "synthetic done", {}, stop_reason=state.stop_reason)

    agent._loop.run = run
    return state


async def beat(rig):
    return await rig.hb._beat_once(rig.clock.now)


async def test_successful_handover_archives_old_round_then_rebuilds_all_prompt_sections(rig):
    original_prompt = rig.agent._system_prompt
    (rig.builder.prompt_dir / "master.md").write_text("updated synthetic master", encoding="utf-8")
    assert rig.builder.build() == original_prompt
    rig.agent._rollback_state_path.write_text("old rollback", encoding="utf-8")
    result = await beat(rig)
    messages, context = rig.calls[0]
    assert result.stop_reason == "complete" and context.situation_written
    assert "近况交接" in messages[-1]["content"]
    assert "目标回顾：" in messages[-1]["content"]
    assert rig.old.status == "inactive"
    assert rig.manager.current_session is not rig.old
    assert "new synthetic situation" in rig.agent._system_prompt
    assert "updated synthetic master" in rig.agent._system_prompt
    assert rig.builder.build() == rig.agent._system_prompt
    assert not rig.agent._rollback_state_path.exists()
    archive = rig.manager.chronicle.get_recent_rounds(session_id=rig.old.session_id, limit=1)
    assert any("近况交接" in row["content"] for row in archive)
    rig.cache.clear.assert_awaited_once()
    assert rig.hb._last_goal_review_at == rig.clock.now
    await beat(rig)
    assert "近况交接" not in rig.calls[-1][0][-1]["content"]
    rig.cache.clear.assert_awaited_once()


@pytest.mark.parametrize("reason", ["yielded", "error", "iteration_limit", "skipped"])
async def test_incomplete_round_never_switches_even_after_successful_write(rig, reason):
    rig.stop_reason = reason
    await beat(rig)
    assert rig.manager.current_session is rig.old
    rig.cache.clear.assert_not_awaited()


async def test_missing_write_retries_next_heartbeat(rig):
    rig.write = False
    await beat(rig)
    assert rig.manager.current_session is rig.old
    rig.write = True
    await beat(rig)
    assert "近况交接" in rig.calls[-1][0][-1]["content"]
    assert rig.manager.current_session is not rig.old


async def test_failed_daily_summary_blocks_handover_until_next_heartbeat(rig, monkeypatch):
    import runtime.timezone as app_timezone
    monkeypatch.setattr(app_timezone, "time", SimpleNamespace(time=lambda: rig.clock.now))
    rig.manager._last_compressed_date -= timedelta(days=1)
    monkeypatch.setattr(rig.manager.chronicle, "get_messages_by_time_range", lambda *args: [{"content": "synthetic"}])
    rig.manager.compressor.compress_date = AsyncMock(side_effect=[False, True])
    rig.manager.maybe_compress_daily = SessionManager.maybe_compress_daily.__get__(rig.manager)
    await beat(rig)
    assert not rig.manager.daily_compression_ready(rig.clock.now)
    assert "近况交接" not in rig.calls[-1][0][-1]["content"]
    assert rig.manager.current_session is rig.old
    await beat(rig)
    assert rig.manager.daily_compression_ready(rig.clock.now)
    assert "近况交接" in rig.calls[-1][0][-1]["content"]
    assert rig.manager.current_session is not rig.old
    assert rig.manager.compressor.compress_date.await_count == 2


async def test_failed_progress_save_blocks_switch_and_retries_without_recompressing(rig, monkeypatch):
    import runtime.timezone as app_timezone
    monkeypatch.setattr(app_timezone, "time", SimpleNamespace(time=lambda: rig.clock.now))
    rig.manager._last_compressed_date -= timedelta(days=1)
    rig.manager._save_compression_state()
    previous = rig.manager.compression_state_path.read_bytes()
    rig.manager.compressor.compress_date = AsyncMock(return_value=True)
    monkeypatch.setattr(rig.manager.chronicle, "get_messages_by_time_range", lambda *args: [{"content": "synthetic"}])
    rig.manager.maybe_compress_daily = SessionManager.maybe_compress_daily.__get__(rig.manager)
    original = rig.manager._write_cache

    def fail(data, path):
        if path == rig.manager.compression_state_path:
            raise OSError("synthetic compression progress failure")
        return original(data, path)

    monkeypatch.setattr(rig.manager, "_write_cache", fail)
    with pytest.raises(OSError, match="compression progress failure"):
        await beat(rig)
    assert rig.manager.current_session is rig.old
    assert not rig.manager.daily_compression_ready(rig.clock.now)
    assert rig.manager.compression_state_path.read_bytes() == previous
    monkeypatch.setattr(rig.manager, "_write_cache", original)
    await beat(rig)
    assert rig.manager.current_session is not rig.old
    rig.manager.compressor.compress_date.assert_awaited_once()


async def test_previous_round_write_does_not_satisfy_handover(rig):
    rig.clock.now = timestamp(23, "00:59")
    await beat(rig)
    assert rig.calls[-1][1].situation_written
    assert rig.manager.current_session is rig.old
    rig.clock.now = timestamp(23, "01:00")
    rig.write = False
    await beat(rig)
    assert not rig.calls[-1][1].situation_written
    assert rig.manager.current_session is rig.old


async def test_failed_save_cannot_reuse_an_earlier_round_success(rig, monkeypatch):
    original = rig.builder.write_situation

    def fail(content):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(rig.builder, "write_situation", fail)
    await beat(rig)
    assert "synthetic full disk" in rig.tool_error
    assert rig.manager.current_session is rig.old
    assert rig.builder.read_situation() == "old synthetic situation"
    monkeypatch.setattr(rig.builder, "write_situation", original)
    await beat(rig)
    assert rig.manager.current_session is not rig.old


@pytest.mark.parametrize("clock,created_day", [
    ("00:59", 22), ("07:00", 22), ("12:00", 22), ("03:00", 23), ("03:00", 24),
])
async def test_window_and_creation_calendar_day(rig, clock, created_day):
    rig.clock.now = timestamp(23, clock)
    rig.old.start_time = timestamp(created_day, "00:01")
    await beat(rig)
    assert "近况交接" not in rig.calls[0][0][-1]["content"]
    assert rig.manager.current_session is rig.old


async def test_start_of_window_is_inclusive_and_uses_shanghai_calendar_date(rig):
    rig.clock.now = timestamp(23, "01:00")
    # UTC 日期仍是昨天；创建时间也按上海日历日期比较。
    rig.old.start_time = timestamp(22, "23:59")
    await beat(rig)
    assert rig.manager.current_session is not rig.old


@pytest.mark.parametrize("clock,expected", [("23:00", True), ("02:59", True), ("03:00", False), ("22:59", False)])
def test_cross_midnight_window(rig, clock, expected):
    rig.hb.configure({"session_switch": {"window_start": "23:00", "window_end": "03:00"}})
    assert rig.hb._session_switch_due(rig.old, timestamp(23, clock)) is expected


async def test_user_arriving_before_commit_keeps_current_session(rig):
    rig.arrive = True
    await beat(rig)
    assert rig.manager.current_session is rig.old
    rig.cache.clear.assert_not_awaited()


async def test_user_arriving_during_cache_cleanup_cancels_commit(rig):
    async def arrive():
        rig.agent._pending_user_rounds += 1
    rig.cache.clear.side_effect = arrive
    await beat(rig)
    assert rig.manager.current_session is rig.old


async def test_waiting_heartbeat_rechecks_session_after_lock_is_acquired(rig):
    await rig.agent._lock.acquire()
    task = asyncio.create_task(beat(rig))
    await asyncio.sleep(0)
    # 等锁期间，当前 session 已被其他宿主操作更新为今天创建。
    rig.old.start_time = rig.clock.now
    rig.agent._lock.release()
    await task
    assert "近况交接" not in rig.calls[-1][0][-1]["content"]
    assert rig.manager.current_session is rig.old


async def test_round_crossing_window_end_does_not_switch(rig):
    rig.clock.now = timestamp(23, "06:59")
    original = rig.agent._loop.run

    async def finish_late(messages, context):
        result = await original(messages, context)
        rig.clock.now = timestamp(23, "07:00")
        return result

    rig.agent._loop.run = finish_late
    await beat(rig)
    assert "近况交接" in rig.calls[-1][0][-1]["content"]
    assert rig.manager.current_session is rig.old


async def test_prompt_rebuild_failure_keeps_old_session_and_old_prompt(rig, monkeypatch):
    original = rig.agent._system_prompt
    monkeypatch.setattr(rig.builder, "_load_skills", lambda: (_ for _ in ()).throw(OSError("synthetic prompt failure")))
    with pytest.raises(OSError, match="synthetic prompt failure"):
        await beat(rig)
    assert rig.manager.current_session is rig.old
    assert rig.agent._system_prompt == original


async def test_archive_failure_never_switches(rig, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic archive failure")
    monkeypatch.setattr(rig.manager.chronicle, "append_messages", fail)
    with pytest.raises(OSError, match="synthetic archive failure"):
        await beat(rig)
    assert rig.manager.current_session is rig.old
    rig.cache.clear.assert_not_awaited()


async def test_manual_switch_rebuilds_prompt_without_requiring_model_handover(rig):
    rig.builder.write_situation("manually updated synthetic situation")
    await rig.agent.switch_session()
    assert "manually updated synthetic situation" in rig.agent._system_prompt
    assert not rig.calls


@pytest.mark.parametrize("fault", ["insert", "commit", "cache", "summaries", "recent", "collision"])
async def test_failed_switch_preserves_old_session_cache_and_rollback_then_can_retry(rig, monkeypatch, fault):
    rig.manager._assemble_summary_message = SessionManager._assemble_summary_message.__get__(rig.manager)
    rig.old.history.append({"role": "user", "content": "synthetic old history"})
    await rig.manager.add_usage(45000)
    previous_cache = rig.manager.cache_path.read_bytes()
    previous_prompt = rig.agent._system_prompt
    cleanup = AsyncMock()
    rig.manager.set_on_session_inactive(cleanup)
    rig.agent._rollback_state_path.write_text("synthetic rollback", encoding="utf-8")
    rig.builder.write_situation("synthetic candidate prompt")

    with monkeypatch.context() as patch:
        if fault == "cache":
            patch.setattr(rig.manager, "_write_cache", lambda *args: (_ for _ in ()).throw(OSError("synthetic cache failure")))
        elif fault == "collision":
            patch.setattr(rig.manager, "_generate_session_id", lambda: rig.old.session_id)
        else:
            inject_database_fault(patch, fault)
        with pytest.raises((sqlite3.Error, OSError)):
            await rig.agent.switch_session()

    assert rig.manager.current_session is rig.old
    assert rig.old.status == "active" and rig.old.end_time is None
    assert rig.old.history[-1]["content"] == "synthetic old history"
    assert rig.manager.context_tokens == 45000 and rig.manager._context_warning_sent
    assert rig.manager.cache_path.read_bytes() == previous_cache
    assert rig.manager.chronicle.get_active_session()["session_id"] == rig.old.session_id
    assert rig.manager.chronicle.get_session(rig.old.session_id)["end_time"] is None
    assert rig.agent._system_prompt == previous_prompt
    assert rig.agent._rollback_state_path.read_text(encoding="utf-8") == "synthetic rollback"
    await asyncio.sleep(0)
    cleanup.assert_not_awaited()

    await rig.agent.switch_session()
    await asyncio.sleep(0)
    assert rig.old.status == "inactive" and rig.manager.current_session is not rig.old
    assert "synthetic candidate prompt" in rig.agent._system_prompt
    assert not rig.agent._rollback_state_path.exists()
    cleanup.assert_awaited_once_with(rig.old)
    assert len(rig.manager.chronicle.get_active_sessions()) == 1


@pytest.mark.parametrize("fault", ["messages", "summary_write", "earliest"])
async def test_summary_database_failure_keeps_day_pending_and_blocks_handover(rig, monkeypatch, fault):
    rig.manager.maybe_compress_daily = SessionManager.maybe_compress_daily.__get__(rig.manager)
    rig.manager._last_compressed_date -= timedelta(days=1)
    rig.manager._save_compression_state()
    if fault == "earliest":
        rig.manager._last_compressed_date = None
        rig.manager._compression_state_saved = False
        rig.manager.compression_state_path.unlink()
    previous_cursor = rig.manager._last_compressed_date
    rig.manager.chronicle.append_messages(rig.old.session_id, [
        {"role": "user", "content": "synthetic yesterday", "platform": "web"},
    ])
    with sqlite3.connect(rig.manager.chronicle.db_path) as db:
        db.execute("UPDATE raw_messages SET created_time = ?", (timestamp(22, "12:00"),))
    model_run = AsyncMock(return_value=LoopResult([], '[{"summary":"synthetic day"}]', {}))
    rig.manager.compressor.manager = SimpleNamespace(get_model=lambda role: object())
    monkeypatch.setattr('agent.compressor.AgentLoop', lambda **kw: SimpleNamespace(run=model_run))

    with monkeypatch.context() as patch:
        inject_database_fault(patch, fault)
        await beat(rig)
    assert rig.manager.current_session is rig.old
    assert rig.manager._last_compressed_date == previous_cursor
    assert not rig.manager.daily_compression_ready(rig.clock.now)
    assert "近况交接" not in rig.calls[-1][0][-1]["content"]
    if fault != "summary_write":
        model_run.assert_not_awaited()
    with sqlite3.connect(rig.manager.chronicle.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0

    await beat(rig)
    assert rig.manager.current_session is not rig.old
    assert rig.manager.daily_compression_ready(rig.clock.now)
    with sqlite3.connect(rig.manager.chronicle.db_path) as db:
        assert db.execute("SELECT summary FROM summaries").fetchall() == [("synthetic day",)]


async def test_progress_reminders_and_messages_are_independently_configurable(rig):
    rig.hb._last_goal_review_at = rig.clock.now
    rig.hb._build_goal_reminder = AsyncMock(return_value="synthetic ongoing goal")
    rig.hb.configure({"message": "合成唤醒", "session_switch": {"enabled": False}})
    await beat(rig)
    assert rig.calls[-1][0][-1]["content"].endswith("[system] 合成唤醒")
    rig.hb._build_goal_reminder.assert_not_awaited()
    rig.hb.configure({"message": "合成唤醒", "include_in_progress_tasks": True,
                      "session_switch": {"enabled": False}})
    await beat(rig)
    assert "synthetic ongoing goal" in rig.calls[-1][0][-1]["content"]
    rig.hb._build_goal_reminder.assert_awaited_once_with(full_review=False)


async def test_background_loop_survives_a_failed_round_without_immediate_retry(rig, monkeypatch):
    calls, delays = [], []

    async def execute(trigger_time, expected_generation):
        calls.append(trigger_time)
        if len(calls) == 1:
            raise OSError("synthetic heartbeat failure")
        rig.hb._running = False

    async def sleep(delay):
        delays.append(delay)
        rig.clock.now += delay

    rig.hb.interval_seconds = 60
    rig.hb._next_beat_time = rig.clock.now
    rig.hb._running = True
    monkeypatch.setattr(rig.hb, "_beat_once", execute)
    monkeypatch.setattr('runtime.heartbeat.asyncio', SimpleNamespace(sleep=sleep))
    await rig.hb._loop()
    assert len(calls) == 2 and calls[1] - calls[0] >= 60
    assert delays
