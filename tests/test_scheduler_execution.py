"""Scheduler 执行实例与预约心跳；临时库和模拟工具，不启动真实应用。"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from runtime.goals import GoalManager
from runtime.heartbeat import Heartbeat
from agent.protocol import LoopResult
from runtime.scheduler import MAX_ACTIVE_SCHEDULES, Scheduler, validate_actions
from runtime.tool_registry import registry


@pytest.fixture
def engine(tmp_path):
    calls = []

    async def action(args):
        calls.append(args)
        return {'status': 'ok'}

    registry.register('synthetic_action', 'synthetic', {}, action)
    return Scheduler(str(tmp_path / 'scheduler.db')), calls


def op(**kwargs):
    return {'op': 'create_schedule', 'name': 'synthetic', 'trigger': {'type': 'interval', 'expr': '60m'},
            'actions': [{'type': 'tool', 'name': 'synthetic_action', 'args': {}}], **kwargs}


async def create(db, **kwargs):
    result = await db.update([op(**kwargs)])
    assert result['status'] == 'ok', result
    return result['results'][0]['schedule_id']


def due(db, sid):
    with db._connect() as conn:
        conn.execute('UPDATE schedules SET next_run_at=? WHERE id=?', (time.time()-1, sid))
    return db._get_schedule_sync(sid)


async def settle(db):
    await asyncio.gather(*tuple(db._job_tasks))


async def test_rollback_receipt_has_no_phantom_id_or_run(engine):
    db, calls = engine
    result = await db.update([op(), {'op': 'run_schedule', 'schedule_id': 999}])
    assert result['status'] == 'error' and result['rolled_back']
    assert result['results'][0] == {'status': 'rolled_back'}
    assert await db.list_active_schedules() == [] and calls == []
    with db._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM schedule_runs').fetchone()[0] == 0


async def test_manual_reservation_is_rolled_back_before_effects(engine):
    db, calls = engine
    sid = await create(db)
    result = await db.update([{'op':'run_schedule','schedule_id':sid}, {'op':'unknown'}])
    assert result['status'] == 'error' and calls == [] and not db._job_tasks
    assert (await db.get_schedule_detail(sid))['recent_runs'] == []


async def test_concurrent_creation_stays_within_limit(engine):
    db, _ = engine
    await db.update([op(name=f'synthetic-{i}') for i in range(MAX_ACTIVE_SCHEDULES-1)])
    results = await asyncio.gather(db.update([op()]), db.update([op()]))
    assert sorted(r['status'] for r in results) == ['error', 'ok']
    assert len(await db.list_active_schedules()) == MAX_ACTIVE_SCHEDULES


async def test_real_goal_batch_error_stops_later_action(engine, tmp_path, monkeypatch):
    from tools import goal_update
    db, calls = engine
    manager = GoalManager(str(tmp_path / 'goals.db'))
    monkeypatch.setattr(goal_update, 'get_goal_manager', lambda: manager)
    goal_update.register_tools(registry)
    sid = await create(db, actions=[{'type': 'tool', 'name': 'goal_update', 'args': {'operations': [
        {'op': 'create_goal', 'description': 'synthetic', 'priority': 'low'},
        {'op': 'update_goal', 'goal_id': 999, 'description': 'missing'},
    ]}}, {'type': 'tool', 'name': 'synthetic_action'}])
    await db._run_schedule(due(db, sid), 'scheduled')
    assert calls == []
    assert (await db.get_schedule_detail(sid))['last_status'] == 'error'
    assert await manager.list_active_goals() == []


async def test_all_sources_use_same_busy_guard_and_actual_manual_receipt(engine):
    db, _ = engine
    started, release = asyncio.Event(), asyncio.Event()
    async def blocked(args):
        started.set()
        await release.wait()
        return 'ok'
    registry.register('blocked', 'synthetic', {}, blocked)
    sid = await create(db, actions=[{'type': 'tool', 'name': 'blocked'}])
    row = due(db, sid)
    first = await db._run_schedule(row, 'scheduled', wait=False)
    await asyncio.wait_for(started.wait(), 1)
    manual, catch_up = await asyncio.gather(
        db.update([{'op': 'run_schedule', 'schedule_id': sid}]), db._run_schedule(row, 'catch_up', wait=False))
    assert first['run_status'] == 'accepted'
    assert manual['status'] == 'error' and catch_up['run_status'] == 'busy'
    release.set()
    await settle(db)
    assert len((await db.get_schedule_detail(sid))['recent_runs']) == 1


async def test_record_failure_blocks_replay_and_restart_requires_explicit_ack(engine, monkeypatch):
    db, calls = engine
    sid = await create(db)
    row = due(db, sid)
    with monkeypatch.context() as patcher:
        patcher.setattr(db, '_record_run_sync', Mock(side_effect=OSError('synthetic full disk')))
        result = await db._run_schedule(row, 'scheduled')
    assert result['status'] == 'unknown' and len(calls) == 1
    assert db._due_schedules_sync(time.time()) == []
    assert (await db._run_schedule(row, 'scheduled'))['run_status'] == 'busy'
    recovered = Scheduler(str(db.db_path))
    detail = await recovered.get_schedule_detail(sid)
    assert detail['last_status'] == 'unknown' and detail['recent_runs'][0]['status'] == 'unknown'
    assert recovered._due_schedules_sync(time.time()) == []
    assert (await recovered.update([{'op': 'run_schedule', 'schedule_id': sid}]))['status'] == 'error'
    result = await recovered.update([{'op': 'run_schedule', 'schedule_id': sid, 'acknowledge_unknown': True}])
    assert result['results'][0]['run_status'] == 'accepted'
    await settle(recovered)
    assert len(calls) == 2
    assert (await recovered.get_schedule_detail(sid))['last_status'] == 'ok'
    assert recovered._get_schedule_sync(sid)['next_run_at'] > time.time()


async def test_cancellation_retains_unknown_effect_and_does_not_auto_retry(engine):
    db, calls = engine
    started = asyncio.Event()
    async def partial(args):
        calls.append('effect')
        started.set()
        await asyncio.Event().wait()
    registry.register('partial', 'synthetic', {}, partial)
    sid = await create(db, actions=[{'type': 'tool', 'name': 'partial'}])
    await db._run_schedule(due(db, sid), 'scheduled', wait=False)
    await asyncio.wait_for(started.wait(), 1)
    await db.stop()
    assert calls == ['effect']
    assert (await db.get_schedule_detail(sid))['recent_runs'][0]['status'] == 'unknown'
    assert db._due_schedules_sync(time.time()) == []


async def test_failed_once_remains_queryable_by_id(engine):
    db, _ = engine
    registry.register('failure', 'synthetic', {}, lambda args: {'error': 'synthetic failure'})
    sid = await create(db, trigger={'type':'once','expr':'2099-01-01 09:00'},
                       actions=[{'type':'tool','name':'failure'}])
    with db._connect() as conn:
        conn.execute("UPDATE schedules SET trigger_expr='2000-01-01 09:00' WHERE id=?", (sid,))
    await db._run_schedule(due(db, sid), 'scheduled')
    assert await db.list_active_schedules() == []
    detail = await db.get_schedule_detail(sid)
    assert detail['status'] == 'inactive' and detail['recent_runs'][0]['status'] == 'error'


async def test_once_consumed_even_if_wall_clock_moves_backwards(engine):
    db, calls = engine
    sid = await create(db, trigger={'type':'once','expr':'2099-01-01 09:00'})
    row = due(db, sid)
    # 模拟结束时的时钟比原计划时间早，不能因此再次保留同一个 once。
    await db._run_schedule(row, 'scheduled')
    assert len(calls) == 1 and db._get_schedule_sync(sid)['status'] == 'inactive'


async def test_startup_plans_slow_interval_without_blocking_once(engine):
    db, _ = engine
    async def blocked(args):
        await asyncio.Event().wait()
    registry.register('blocked', 'synthetic', {}, blocked)
    first = await create(db, actions=[{'type':'tool','name':'blocked'}])
    second = await create(db, trigger={'type':'once','expr':'2099-01-01 09:00'})
    due(db, first)
    due(db, second)
    await asyncio.wait_for(db._startup_catch_up(), 1)
    assert {s['id'] for s in db._due_schedules_sync(time.time())} == {first, second}


async def test_catch_up_cursor_survives_restart_between_completed_points(engine):
    db, calls = engine
    sid = await create(db, trigger={'type':'interval','expr':'30m'})
    with db._connect() as conn:
        conn.execute('UPDATE schedules SET next_run_at=? WHERE id=?', (time.time()-3601, sid))
    await db._startup_catch_up()
    await db._run_schedule(db._get_schedule_sync(sid), 'catch_up')
    assert len(calls) == 1
    recovered = Scheduler(str(db.db_path))
    await recovered._startup_catch_up()
    for _ in range(3):
        rows = recovered._due_schedules_sync(time.time())
        if not rows:
            break
        await recovered._run_schedule(rows[0], 'catch_up')
    assert len(calls) == 3  # 重启后只补剩余两个点。


async def test_changed_queued_action_is_not_executed_under_stale_claim(engine):
    db, calls = engine
    sid = await create(db)
    await db._slots.acquire()
    await db._slots.acquire()
    await db._run_schedule(due(db, sid), 'scheduled', wait=False)
    async def wait_for_queue():
        while not db._slots._waiters:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait_for_queue(), 1)
    await db.update([{'op':'update_schedule','schedule_id':sid,'actions':[
        {'type':'tool','name':'synthetic_action','args':{'new':True}}]}])
    db._slots.release()
    db._slots.release()
    await settle(db)
    assert calls == []
    await db._run_schedule(db._get_schedule_sync(sid), 'scheduled')
    assert calls == [{'new':True}]


@pytest.mark.parametrize('actions,trigger', [
    ([{'type':'heartbeat','content':'synthetic'}], 'interval'),
    ([{'type':'heartbeat','content':'synthetic'}, {'type':'tool','name':'synthetic_action'}], 'once'),
    ([{'type':'notify','platform':'qq','text':'removed'}], 'once'),
    ([{'type':'heartbeat','content':''}], 'once'),
])
async def test_rejected_action_shapes(engine, actions, trigger):
    db, _ = engine
    result = await db.update([op(actions=actions, trigger={'type':trigger,'expr':'60m' if trigger=='interval' else '2099-01-01 09:00'})])
    assert result['status'] == 'error'


async def test_heartbeat_update_validates_combined_trigger_and_actions(engine):
    db, _ = engine
    sid = await create(db, trigger={'type':'once','expr':'2099-01-01 09:00'},
                       actions=[{'type':'heartbeat','content':'synthetic'}])
    result = await db.update([{'op':'update_schedule','schedule_id':sid,'trigger':{'type':'interval','expr':'60m'}}])
    assert result['status'] == 'error'
    sid = await create(db)
    assert (await db.update([{'op':'update_schedule','schedule_id':sid,
        'actions':[{'type':'heartbeat','content':'synthetic'}]}]))['status'] == 'error'


async def test_schedule_heartbeat_calls_public_service_and_cannot_mix_timeout(engine):
    db, _ = engine
    async def scheduled(*args, **kwargs):
        assert await kwargs['before_run']()
        return LoopResult([], '', {})
    service = SimpleNamespace(run_scheduled=AsyncMock(side_effect=scheduled))
    db.set_heartbeat(service)
    sid = await create(db, trigger={'type':'cron','expr':'0 9 * * *'},
                       actions=[{'type':'heartbeat','content':'synthetic appointment'}])
    await db._run_schedule(due(db, sid), 'scheduled')
    args = service.run_scheduled.call_args
    assert args.args == ('synthetic appointment',)
    assert args.kwargs['request_id'].startswith('schedule-run-')
    assert not await args.kwargs['before_run']()  # 完成后计划已推进，旧预约不再具备准入资格。
    assert (await db.get_schedule_detail(sid))['last_status'] == 'ok'
    assert (await db.update([op(trigger={'type':'once','expr':'2099-01-01 09:00'},
        actions=[{'type':'heartbeat','content':'synthetic'}], max_runtime=30)]))['status'] == 'error'


async def test_modified_waiting_heartbeat_does_not_run_stale_appointment(engine, tmp_path, monkeypatch):
    db, _ = engine
    session = SimpleNamespace(background_ready=False, run_round=AsyncMock())
    hb = Heartbeat(session, state_file=tmp_path/'runtime.heartbeat.json')
    started = asyncio.Event()
    original = hb.run_scheduled
    async def wait(*args, **kwargs):
        started.set()
        return await original(*args, **kwargs)
    monkeypatch.setattr(hb, 'run_scheduled', wait)
    db.set_heartbeat(hb)
    sid = await create(db, trigger={'type':'once','expr':'2099-01-01 09:00'},
                       actions=[{'type':'heartbeat','content':'old appointment'}])
    await db._run_schedule(due(db, sid), 'scheduled', wait=False)
    await asyncio.wait_for(started.wait(), 1)
    await db.update([{'op':'update_schedule','schedule_id':sid,'trigger':{'type':'once','expr':'2099-02-01 09:00'}}])
    await asyncio.wait_for(settle(db), 1)
    session.run_round.assert_not_awaited()
    row = db._get_schedule_sync(sid)
    assert row['status'] == 'active' and row['next_run_at'] > time.time()
    assert (await db.get_schedule_detail(sid))['last_status'] == 'cancelled'


async def test_scheduled_heartbeat_waits_for_user_and_does_not_change_timer(tmp_path, monkeypatch):
    session = SimpleNamespace(background_ready=False, run_round=AsyncMock(return_value=LoopResult([], '', {})))
    hb = Heartbeat(session, state_file=tmp_path/'runtime.heartbeat.json')
    monkeypatch.setattr(hb, '_build_goal_reminder', AsyncMock(return_value=''))
    task = asyncio.create_task(hb.run_scheduled('synthetic appointment', request_id='appointment-1'))
    await asyncio.sleep(0)
    session.run_round.assert_not_awaited()
    hb.reset_timer()  # 用户输入重置固定计时，不取消预约。
    expected_time, generation = hb._next_beat_time, hb._timer_generation
    session.background_ready = True
    await asyncio.wait_for(task, 1)
    kwargs = session.run_round.call_args.kwargs
    assert kwargs['source'] == 'heartbeat' and kwargs['request_id'] == 'appointment-1'
    assert 'synthetic appointment' in kwargs['injection_messages'][0]['content']
    assert (hb._next_beat_time, hb._timer_generation) == (expected_time, generation)


async def test_scheduled_heartbeat_preserves_appointment_after_yield(tmp_path, monkeypatch):
    session = SimpleNamespace(background_ready=True, run_round=AsyncMock(side_effect=[
        LoopResult([], '', {}, stop_reason='yielded'), LoopResult([], '', {}),
    ]))
    hb = Heartbeat(session, state_file=tmp_path/'runtime.heartbeat.json')
    monkeypatch.setattr(hb, '_build_goal_reminder', AsyncMock(return_value=''))
    timer = hb._next_beat_time
    await asyncio.wait_for(hb.run_scheduled('synthetic appointment', request_id='appointment-1'), 1)
    assert session.run_round.await_count == 2 and hb._next_beat_time == timer


def test_legacy_qq_converts_to_tool_and_web_is_archived(engine):
    db, _ = engine
    for platform in ('qq', 'web'):
        with db._connect() as conn:
            conn.execute("INSERT INTO schedules(name,status,trigger_type,trigger_expr,actions,next_run_at,created_at,updated_at) "
                         "VALUES(?,'active','interval','60m',?,?,?,?)", (
                'synthetic', json.dumps([{'type':'notify','platform':platform,'text':'synthetic'}]), 9999999999, 1, 1))
    migrated = Scheduler(str(db.db_path))
    qq, web = migrated._get_schedule_sync(1), migrated._get_schedule_sync(2)
    assert qq['actions'] == [{'type':'tool','name':'send_message','args':{'platform':'qq','text':'synthetic'}}]
    assert web['status'] == 'inactive' and web['next_run_at'] is None
    assert not hasattr(migrated, '_senders')


def test_shared_terminal_and_model_side_effect_tools_are_rejected():
    for name in ('terminal_run', 'terminal_interact'):
        registry.register(name, 'synthetic', {}, lambda args: 'unused')
        assert validate_actions([{'type':'tool','name':name}])
