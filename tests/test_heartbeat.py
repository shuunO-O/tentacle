"""
Heartbeat 测试

覆盖：简洁心跳、全部活跃目标及未完成任务、无目标和查询失败时仍唤醒。
不启动 AgentSession、不调副模型。
"""

from unittest.mock import ANY, AsyncMock

import pytest

from runtime.goals import GoalManager
from runtime.heartbeat import Heartbeat, heartbeat_settings
from agent.protocol import LoopResult


async def test_disabled_periodic_heartbeat_never_starts_round_and_reenable_resets_timer(tmp_path):
    session = AsyncMock()
    heartbeat = Heartbeat(session, interval_minutes=1, settings={'enabled': False},
                          state_file=tmp_path / 'synthetic-heartbeat.json')
    generation = heartbeat._timer_generation
    result = await heartbeat._beat_once(0, expected_generation=generation)
    assert result.stop_reason == 'skipped'
    session.run_round.assert_not_called()
    heartbeat.configure({'enabled': True})
    assert heartbeat._timer_generation > generation
    result = await heartbeat._beat_once(0, expected_generation=generation)
    assert result.stop_reason == 'skipped'
    session.run_round.assert_not_called()


def test_heartbeat_enabled_defaults_to_true_and_requires_boolean():
    assert heartbeat_settings()['enabled'] is True
    with pytest.raises(ValueError):
        heartbeat_settings({'enabled': 'false'})


@pytest.fixture
def manager(tmp_path):
    return GoalManager(str(tmp_path / "hb_test.db"))


@pytest.fixture
def hb(manager, monkeypatch, tmp_path):
    """Heartbeat 实例，get_goal_manager 指向临时库。"""
    monkeypatch.setattr('runtime.heartbeat.get_goal_manager', lambda: manager)
    return Heartbeat(agent_session=None, interval_minutes=1,
                     state_file=tmp_path / 'runtime.heartbeat.json')


async def _seed(manager, goal_priority="high", task_specs=None):
    """建一个 goal + 若干 task。task_specs: [(content, urgency)]"""
    result = await manager.update([
        {"op": "create_goal", "description": "种子目标", "priority": goal_priority},
    ])
    goal_id = result["results"][0]["goal_id"]
    if task_specs:
        await manager.update([
            {"op": "create_task", "goal_id": goal_id, "content": c, "urgency": u}
            for c, u in task_specs
        ])
    return goal_id


# ---------- 消息拼接 ----------


def test_message_without_reminder(hb):
    msgs = hb._build_heartbeat_message("", 1755800000.0)
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert content.startswith("[")
    assert "[system] 心跳" in content
    assert "当前目标" not in content


def test_message_reminder_only(hb):
    msgs = hb._build_heartbeat_message("目标提醒：xxx", 1755800000.0)
    content = msgs[0]["content"]
    assert "[system] 心跳" in content
    assert "目标提醒：xxx" in content


def test_scheduled_content_precedes_reminder(hb):
    msgs = hb._build_heartbeat_message("reminder", 1755800000.0, "scheduled request")
    content = msgs[0]["content"]
    assert "预约事项：scheduled request" in content
    assert "reminder" in content
    assert content.index("预约事项") < content.index("reminder")


def test_message_timestamp_format(hb):
    msgs = hb._build_heartbeat_message("", 1755800000.0)
    # 空段时只有心跳头；时间戳格式 [MM.DD-HH:MM]
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"].count("[system]") == 1


# ---------- 目标提醒触发条件 ----------


async def test_reminder_empty_when_no_goals(hb, manager):
    assert await hb._build_goal_reminder(full_review=True) == ""


async def test_goal_without_tasks_is_visible(hb, manager):
    await _seed(manager, task_specs=None)
    assert await hb._build_goal_reminder(full_review=True) == "目标回顾：\n- [高] 种子目标"


async def test_reminder_triggered_by_in_progress(hb, manager):
    await _seed(manager, task_specs=[("任务A", "medium")])
    tasks = (await manager.list_active_goals())[0]["tasks"]
    await manager.update([
        {"op": "update_task", "task_id": tasks[0]["id"], "status": "in_progress"},
    ])
    reminder = await hb._build_goal_reminder(full_review=True)
    assert "任务A" in reminder
    assert "进行中" in reminder
    assert "plan_view" not in reminder
    assert "goal#" not in reminder
    assert "task#" not in reminder


async def test_reminder_triggered_by_high_pending(hb, manager):
    await _seed(manager, task_specs=[("紧急任务", "high")])
    reminder = await hb._build_goal_reminder(full_review=True)
    assert "紧急任务" in reminder
    assert "待办" in reminder


async def test_reminder_includes_medium_and_low_pending(hb, manager):
    await _seed(manager, task_specs=[("普通任务", "medium"), ("低优任务", "low")])
    reminder = await hb._build_goal_reminder(full_review=True)
    assert "待办：普通任务" in reminder
    assert "待办：低优任务" in reminder


async def test_reminder_silent_on_db_error(hb, manager, monkeypatch):
    """GoalManager 抛异常时静默返回空串，心跳不崩。"""
    async def boom(self):
        raise RuntimeError("数据库炸了")

    class BoomManager:
        list_active_goals = boom

    monkeypatch.setattr('runtime.heartbeat.get_goal_manager', lambda: BoomManager())
    assert await hb._build_goal_reminder(full_review=True) is None


async def test_no_goal_or_task_truncation(hb, manager):
    for index in range(4):
        await manager.update([
            {"op": "create_goal", "description": f"目标{index}", "priority": "low"},
        ])
    goal_id = await _seed(manager, task_specs=[(f"步骤{i}", "low") for i in range(4)])
    tasks = (await manager.list_active_goals(goal_id))[0]["tasks"]
    await manager.update([{"op": "update_task", "task_id": tasks[0]["id"], "status": "completed"}])
    reminder = await hb._build_goal_reminder(full_review=True)
    for index in range(4):
        assert f"目标{index}" in reminder
    assert tasks[0]["content"] not in reminder
    for task in tasks[1:]:
        assert task["content"] in reminder


@pytest.mark.parametrize("query_fails", [False, True])
async def test_bare_heartbeat_runs_even_without_goals(hb, monkeypatch, query_fails):
    session = AsyncMock()
    hb.agent_session = session
    if query_fails:
        manager = AsyncMock()
        manager.list_active_goals.side_effect = RuntimeError("unavailable")
        monkeypatch.setattr('runtime.heartbeat.get_goal_manager', lambda: manager)
    await hb._beat_once(1755800000.0)
    session.run_round.assert_awaited_once_with(
        source="heartbeat",
        injection_messages=[{"role": "user", "content": f"{hb._format_time(1755800000.0)}[system] 心跳"}],
        trigger_time=1755800000.0, should_run=None, request_id=None, before_run=None,
        prepare_heartbeat=ANY, finish_heartbeat=ANY,
    )


async def test_daily_heartbeat_only_in_progress(hb, manager):
    goal_id = await _seed(manager, task_specs=[("当前步骤", "low"), ("紧急待办", "high")])
    tasks = (await manager.list_active_goals(goal_id))[0]["tasks"]
    active = next(task for task in tasks if task["content"] == "当前步骤")
    await manager.update([{"op": "update_task", "task_id": active["id"], "status": "in_progress"}])
    await manager.update([{"op": "create_goal", "description": "长期方向", "priority": "high"}])
    reminder = await hb._build_goal_reminder()
    assert reminder == "进行中的目标：\n- [高] 种子目标\n  进行中：当前步骤"


async def test_pending_and_unsplit_goals_do_not_prompt_daily(hb, manager):
    await _seed(manager, task_specs=[("待办", "high")])
    await _seed(manager)
    assert await hb._build_goal_reminder() == ""


async def test_review_every_24_hours_survives_restart(hb, manager):
    await _seed(manager, task_specs=[("普通待办", "medium")])
    session = AsyncMock()
    hb.agent_session = session
    start = 1755800000.0
    await hb._beat_once(start)
    assert "目标回顾：" in session.run_round.call_args.kwargs["injection_messages"][0]["content"]
    restarted = Heartbeat(session, state_file=hb._state_file)
    await restarted._beat_once(start + 86400 - 1)
    assert session.run_round.call_args.kwargs["injection_messages"][0]["content"].endswith("[system] 心跳")
    await restarted._beat_once(start + 86400)
    content = session.run_round.call_args.kwargs["injection_messages"][0]["content"]
    assert "目标回顾：" in content
    assert "普通待办" in content
    assert "进行中的目标：" not in content
    assert Heartbeat(session, state_file=hb._state_file)._last_goal_review_at == start + 86400


async def test_query_failure_does_not_consume_review(hb, monkeypatch):
    hb.agent_session = AsyncMock()
    manager = AsyncMock()
    manager.list_active_goals.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr('runtime.heartbeat.get_goal_manager', lambda: manager)
    await hb._beat_once(1755800000.0)
    assert not hb._state_file.exists()
    assert hb._last_goal_review_at == 0
    hb.agent_session.run_round.assert_awaited_once()


async def test_failed_heartbeat_does_not_consume_review(hb, manager):
    await _seed(manager)
    hb.agent_session = AsyncMock()
    hb.agent_session.run_round.side_effect = RuntimeError("failed")
    with pytest.raises(RuntimeError, match="failed"):
        await hb._beat_once(1755800000.0)
    assert not hb._state_file.exists()
    assert hb._last_goal_review_at == 0


@pytest.mark.parametrize('stop_reason', ['error', 'yielded', 'iteration_limit', 'skipped'])
async def test_incomplete_heartbeat_result_does_not_consume_review(hb, stop_reason):
    hb.agent_session = AsyncMock()
    hb.agent_session.run_round.return_value = LoopResult([], '', {}, stop_reason=stop_reason)
    await hb._beat_once(1755800000.0)
    assert not hb._state_file.exists()
    assert hb._last_goal_review_at == 0


@pytest.mark.parametrize("contents", ['invalid', '{}', '[]', '{"last_goal_review_at": "NaN"}'])
def test_invalid_review_state_recovers(tmp_path, contents):
    path = tmp_path / 'runtime.heartbeat.json'
    path.write_text(contents)
    assert Heartbeat(None, state_file=path)._last_goal_review_at == 0
