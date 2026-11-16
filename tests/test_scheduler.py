"""
Scheduler 测试

覆盖：三种触发器的 next_run 计算、创建/修改/归档与批量回滚、once 自动归档、
停机补跑策略、防重入、三种执行形态（tool/command/notify）、失败通知、
运行记录推进 next_run、schedule_update / schedule_list 工具层。
全走 tmp_path 临时库，不碰生产 data/、不启动真实调度长轮询。
"""

import asyncio
import json
import time
import sys
from datetime import datetime, timedelta
from runtime.timezone import get_timezone, local_datetime

import pytest

from runtime.scheduler import Scheduler, compute_next_run, validate_actions
from runtime.tool_registry import registry


def message(platform, text):
    return {"type": "tool", "name": "send_message", "args": {"platform": platform, "text": text}}


@pytest.fixture(autouse=True)
def message_tool(monkeypatch):
    from tools import send_message
    monkeypatch.setattr(send_message, "_senders", {})
    monkeypatch.setattr(send_message, "SUPPORTED_PLATFORMS", set())
    monkeypatch.setattr(send_message, "_channel_meta", {})
    send_message.register_tools(registry)



# ---------- 夹具与辅助 ----------


@pytest.fixture
def db(tmp_path):
    return Scheduler(str(tmp_path / "test_scheduler.db"))


@pytest.fixture
def sent():
    """notify 出站通道的记录容器"""
    return []


@pytest.fixture
def register_qq_sender(db, sent):
    async def _sender(text: str):
        sent.append(text)
        return {"sent_chunks": 1}

    from tools.send_message import register_sender
    register_sender("qq", _sender)
    return _sender


@pytest.fixture
def dummy_tool():
    """注册一个测试工具，返回调用记录列表"""
    calls = []

    def _func(args):
        calls.append(args)
        return json.dumps({"status": "ok", "echo": args}, ensure_ascii=False)

    registry.register(
        name="dummy_tool",
        description="test",
        parameters={"type": "object"},
        func=_func,
    )
    return calls


async def _wait_until(pred, timeout=3.0, step=0.05):
    """轮询等待条件成立"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def _set_next_run(db, schedule_id, next_run_at):
    """直接改库里的 next_run_at（模拟停机前遗留状态）"""
    with db._connect() as conn:
        conn.execute(
            "UPDATE schedules SET next_run_at = ? WHERE id = ?", (next_run_at, schedule_id)
        )


async def _create(db, name="测试任务", trigger=None, actions=None, **extra):
    """走 update() 建一个任务，返回 (结果, schedule_id)"""
    if trigger is None:
        trigger = {"type": "interval", "expr": "60m"}
    if actions is None:
        actions = [message("qq", "hi")]
    op = {"op": "create_schedule", "name": name, "trigger": trigger, "actions": actions}
    op.update(extra)
    result = await db.update([op])
    return result, result["results"][0].get("schedule_id")


async def _start_and_drain(db):
    await db._startup_catch_up()
    # 模拟轮询派发，不启动长跑服务。
    for _ in range(21):
        due = db._due_schedules_sync(time.time())
        if not due:
            break
        for row in due:
            trigger = 'catch_up' if row['next_run_at'] <= db._catch_up_until.get(row['id'], 0) else 'scheduled'
            await db._run_schedule(row, trigger)


# ---------- compute_next_run ----------


def test_compute_next_run_cron():
    # 2026-08-30 是周日；下一个周一 9:00 是 08-31
    base = datetime(2026, 8, 30, 10, 0, tzinfo=get_timezone()).timestamp()
    nxt = compute_next_run("cron", "0 9 * * 1", base)
    assert nxt == datetime(2026, 8, 31, 9, 0, tzinfo=get_timezone()).timestamp()


def test_compute_next_run_interval():
    base = datetime(2026, 8, 30, 10, 0, tzinfo=get_timezone()).timestamp()
    assert compute_next_run("interval", "30m", base) == base + 1800
    assert compute_next_run("interval", "90s", base) == base + 90
    assert compute_next_run("interval", "2h", base) == base + 7200
    assert compute_next_run("interval", "5", base) == base + 300  # 裸数字=分钟


def test_compute_next_run_interval_invalid():
    with pytest.raises(ValueError):
        compute_next_run("interval", "abc", time.time())
    with pytest.raises(ValueError):
        compute_next_run("interval", "0m", time.time())


def test_compute_next_run_once():
    base = datetime(2026, 8, 30, 10, 0, tzinfo=get_timezone()).timestamp()
    assert compute_next_run("once", "2026-08-31 09:00", base) == datetime(
        2026, 8, 31, 9, 0, tzinfo=get_timezone()
    ).timestamp()
    # 已过期 → None
    assert compute_next_run("once", "2026-01-01 09:00", base) is None
    with pytest.raises(ValueError):
        compute_next_run("once", "not-a-date", base)


def test_compute_next_run_cron_invalid():
    with pytest.raises(ValueError):
        compute_next_run("cron", "not a cron", time.time())
    with pytest.raises(ValueError):
        compute_next_run("weekly", "0 9 * * 1", time.time())


# ---------- validate_actions ----------


def test_validate_actions(dummy_tool):
    assert validate_actions([{"type": "tool", "name": "dummy_tool", "args": {"a": 1}}]) is None
    assert validate_actions([{"type": "command", "command": "echo hi"}]) is None
    assert validate_actions([message("qq", "x")]) is None
    assert "非空数组" in validate_actions([])
    assert "未注册的工具" in validate_actions([{"type": "tool", "name": "nope"}])
    assert "必须提供 command" in validate_actions([{"type": "command"}])
    assert "未知动作类型" in validate_actions([{"type": "agent"}])


# ---------- 创建 / 修改 / 归档 / 回滚 ----------


async def test_create_and_list(db, register_qq_sender):
    result, sid = await _create(db, name="每天提醒")
    assert result["status"] == "ok"
    assert sid == 1
    assert result["results"][0]["next_run_at"] > time.time()

    schedules = await db.list_active_schedules()
    assert len(schedules) == 1
    assert schedules[0]["id"] == 1
    assert schedules[0]["name"] == "每天提醒"
    assert schedules[0]["trigger"] == "interval: 60m"
    assert schedules[0]["next_run"]  # 有格式化时间


async def test_create_validation(db):
    bad_cases = [
        {"op": "create_schedule", "name": "", "trigger": {"type": "once", "expr": "2099-01-01 09:00"}, "actions": [{"type": "command", "command": "x"}]},
        {"op": "create_schedule", "name": "x", "trigger": {"type": "cron", "expr": "bad"}, "actions": [{"type": "command", "command": "x"}]},
        {"op": "create_schedule", "name": "x", "trigger": {"type": "once", "expr": "2000-01-01 09:00"}, "actions": [{"type": "command", "command": "x"}]},
        {"op": "create_schedule", "name": "x", "trigger": {"type": "once", "expr": "2099-01-01 09:00"}, "actions": []},
    ]
    for op in bad_cases:
        result = await db.update([op])
        assert result["results"][0]["status"] == "error", op


async def test_archive_and_guard(db, register_qq_sender):
    _, sid = await _create(db, name="待归档")

    # 归档
    result = await db.update([{"op": "update_schedule", "schedule_id": sid, "status": "inactive"}])
    assert result["results"][0]["status"] == "ok"

    # 归档后列表查不到
    assert await db.list_active_schedules() == []

    # 归档后拒绝修改（不可逆）
    result = await db.update([{"op": "update_schedule", "schedule_id": sid, "name": "改名"}])
    assert "不是 active" in result["results"][0]["error"]


async def test_batch_rollback(db):
    result = await db.update(
        [
            {"op": "create_schedule", "name": "好的", "trigger": {"type": "interval", "expr": "30m"},
             "actions": [{"type": "command", "command": "x"}]},
            {"op": "create_schedule", "name": "坏的", "trigger": {"type": "cron", "expr": "bad"},
             "actions": [{"type": "command", "command": "x"}]},
        ]
    )
    assert result["results"][1]["status"] == "error"
    # 第一个 create 已回滚
    assert await db.list_active_schedules() == []


async def test_forbidden_schedule_tool_action(db, register_qq_sender):
    from tools.schedule_update import register_tools

    register_tools(registry)

    bad = [{"type": "tool", "name": "schedule_update", "args": {"operations": []}}]
    err = validate_actions(bad)
    assert err is not None and "自我复制" in err

    # 创建与修改都被拒绝
    result = await db.update(
        [{"op": "create_schedule", "name": "自复制", "trigger": {"type": "interval", "expr": "60m"},
          "actions": bad}]
    )
    assert "自我复制" in result["results"][0]["error"]

    _, sid = await _create(db, name="正常任务")
    result = await db.update(
        [{"op": "update_schedule", "schedule_id": sid, "actions": bad}]
    )
    assert "自我复制" in result["results"][0]["error"]


async def test_active_schedule_cap(db, register_qq_sender):
    for i in range(50):
        result, _ = await _create(db, name=f"任务{i}")
        assert result["status"] == "ok"

    result, _ = await _create(db, name="第51个")
    assert result["results"][0]["status"] == "error"
    assert "上限" in result["results"][0]["error"]

    # 归档一个腾出名额后可再创建
    await db.update([{"op": "update_schedule", "schedule_id": 1, "status": "inactive"}])
    result, _ = await _create(db, name="补位")
    assert result["results"][0]["status"] == "ok"


async def test_update_trigger_recomputes_next_run(db, register_qq_sender):
    _, sid = await _create(db, name="改触发", trigger={"type": "interval", "expr": "60m"})
    before = db._get_schedule_sync(sid)["next_run_at"]

    result = await db.update(
        [{"op": "update_schedule", "schedule_id": sid, "trigger": {"type": "interval", "expr": "5m"}}]
    )
    assert result["results"][0]["status"] == "ok"
    row = db._get_schedule_sync(sid)
    assert row["trigger_expr"] == "5m"
    assert row["next_run_at"] > before - 3600  # 已从当前时刻按 5m 重算
    assert row["next_run_at"] == pytest.approx(time.time() + 300, abs=60)


# ---------- 执行形态 ----------


async def test_run_tool_action(db, dummy_tool):
    _, sid = await _create(
        db,
        name="直调工具",
        actions=[{"type": "tool", "name": "dummy_tool", "args": {"x": 1}}],
    )
    row = db._get_schedule_sync(sid)
    await db._run_schedule(row, "scheduled")

    assert dummy_tool == [{"x": 1}]
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "ok"
    assert detail["recent_runs"][0]["status"] == "ok"
    assert "dummy_tool" in detail["recent_runs"][0]["result_summary"]
    # 周期任务 next_run 已推进
    assert db._get_schedule_sync(sid)["next_run_at"] > time.time()


async def test_run_command_action(db):
    _, sid = await _create(db, name="跑命令", actions=[{"type": "command", "command": "echo hello"}])
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "ok"
    assert "hello" in detail["recent_runs"][0]["result_summary"]


async def test_run_command_nonzero_exit(db):
    _, sid = await _create(db, name="失败命令", actions=[{"type": "command", "command": "exit 3"}])
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "error"
    assert "退出码 3" in detail["last_error"]


async def test_run_command_timeout(db):
    _, sid = await _create(
        db, name="超时命令",
        actions=[{"type": "command", "command": f'"{sys.executable}" -c "import time; time.sleep(5)"', "timeout": 0.5}],
        max_runtime=60,
    )
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "error"
    assert "超时" in detail["last_error"]


async def test_run_notify_action(db, register_qq_sender, sent):
    _, sid = await _create(db, name="发消息", actions=[message("qq", "定时问候")])
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    assert sent == ["定时问候"]
    assert (await db.get_schedule_detail(sid))["last_status"] == "ok"


async def test_run_notify_unregistered_platform(db):
    _, sid = await _create(db, name="无通道", actions=[message("nope", "x")])
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "error"
    assert "平台尚未接入" in detail["last_error"]


async def test_actions_stop_on_first_failure(db, dummy_tool):
    _, sid = await _create(
        db,
        name="顺序执行",
        actions=[
            {"type": "command", "command": "exit 1"},
            {"type": "tool", "name": "dummy_tool", "args": {}},
        ],
    )
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    assert dummy_tool == []  # 第一步失败后不再执行
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "error"


# ---------- once 归档 / 手动触发 / 失败通知 ----------


async def test_once_auto_archive_after_fire(db, register_qq_sender, sent):
    _, sid = await _create(
        db, name="一次性", trigger={"type": "once", "expr": "2099-01-01 09:00"},
        actions=[message("qq", "only once")],
    )
    assert db._get_schedule_sync(sid)["status"] == "active"

    # 模拟已到触发时刻：把 expr 拨到 1 秒前（create 校验拒绝过去时间，直接改库）
    past = (local_datetime() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect() as conn:
        conn.execute("UPDATE schedules SET trigger_expr = ? WHERE id = ?", (past, sid))

    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    assert sent == ["only once"]
    row = db._get_schedule_sync(sid)
    assert row["status"] == "inactive"
    assert row["next_run_at"] is None
    assert await db.list_active_schedules() == []


async def test_manual_run_keeps_schedule(db, register_qq_sender, sent):
    fire_at = (local_datetime() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    _, sid = await _create(db, name="未来一次性", trigger={"type": "once", "expr": fire_at})
    row_before = db._get_schedule_sync(sid)

    result = await db.update([{"op": "run_schedule", "schedule_id": sid}])
    assert result["results"][0]["status"] == "ok"

    assert await _wait_until(lambda: db._get_schedule_sync(sid)["last_run_at"] is not None)
    row = db._get_schedule_sync(sid)
    # 手动触发不改 next_run / status
    assert row["status"] == "active"
    assert row["next_run_at"] == row_before["next_run_at"]
    detail = await db.get_schedule_detail(sid)
    assert detail["recent_runs"][0]["trigger"] == "manual"


async def test_manual_run_rejects_inactive(db, register_qq_sender):
    _, sid = await _create(db, name="已归档")
    await db.update([{"op": "update_schedule", "schedule_id": sid, "status": "inactive"}])
    result = await db.update([{"op": "run_schedule", "schedule_id": sid}])
    assert result["results"][0]["status"] == "error"


async def test_failure_notify(db, register_qq_sender, sent):
    _, sid = await _create(
        db, name="会失败的任务",
        actions=[{"type": "command", "command": "exit 1"}],
        on_failure={"notify_platform": "qq"},
    )
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    assert len(sent) == 1
    assert "会失败的任务" in sent[0]
    assert "失败" in sent[0]


async def test_max_runtime_enforced(db):
    # 单步命令内部 timeout 10s，但总 max_runtime 0.5s → 整体超时
    _, sid = await _create(
        db, name="总超时",
        actions=[{"type": "command", "command": f'"{sys.executable}" -c "import time; time.sleep(10)"', "timeout": 30}],
        max_runtime=0.5,
    )
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "timeout"
    assert "超时" in detail["last_error"]


# ---------- 停机补跑 ----------


async def test_catch_up_once_within_grace(db, register_qq_sender, sent):
    _, sid = await _create(
        db, name="错过的提醒", trigger={"type": "once", "expr": "2099-01-01 09:00"},
        actions=[message("qq", "补跑")],
    )
    # 模拟自然过期状态：expr 与 next_run_at 都拨到过去（create 校验拒绝过去时间，直接改库）
    past = (local_datetime() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect() as conn:
        conn.execute(
            "UPDATE schedules SET trigger_expr = ?, next_run_at = ? WHERE id = ?",
            (past, time.time() - 3600, sid),  # 1 小时前错过，宽限期 24h 内
        )

    await _start_and_drain(db)
    assert await _wait_until(lambda: db._get_schedule_sync(sid)["last_run_at"] is not None)
    assert sent == ["补跑"]
    # 补跑完成后归档
    assert db._get_schedule_sync(sid)["status"] == "inactive"


async def test_catch_up_once_beyond_grace(db, register_qq_sender, sent):
    _, sid = await _create(db, name="过期太久", trigger={"type": "once", "expr": "2099-01-01 09:00"})
    _set_next_run(db, sid, time.time() - 48 * 3600)  # 超出 24h 宽限期

    await _start_and_drain(db)
    assert await _wait_until(lambda: db._get_schedule_sync(sid)["status"] == "inactive")
    assert sent == []
    detail = await db.get_schedule_detail(sid)
    assert detail["status"] == "inactive"  # 默认列表仍不展示归档任务


async def test_catch_up_cron_recomputed(db, register_qq_sender, sent):
    """cron 停机错过不补跑，从当前时刻重算 next_run"""
    _, sid = await _create(db, name="周期任务", trigger={"type": "cron", "expr": "* * * * *"})
    _set_next_run(db, sid, time.time() - 120)

    await _start_and_drain(db)
    row = db._get_schedule_sync(sid)
    assert row["status"] == "active"
    assert row["next_run_at"] == pytest.approx(time.time() + 60, abs=60)
    assert sent == []
    assert row["last_run_at"] is None


async def test_catch_up_interval_fires_each_missed_point(db, register_qq_sender, sent):
    """interval 停机错过的触发点逐点补跑：30m 间隔错过 90 分钟 → 补 4 次"""
    _, sid = await _create(db, name="补跑任务", trigger={"type": "interval", "expr": "30m"})
    _set_next_run(db, sid, time.time() - 90 * 60)  # 90 分钟前错过，宽限期 6h 内

    await _start_and_drain(db)
    row = db._get_schedule_sync(sid)
    assert row["status"] == "active"
    assert sent == ["hi"] * 4
    # 下一轮从最后一次补跑完成时刻重算
    assert row["next_run_at"] == pytest.approx(time.time() + 1800, abs=1800)


async def test_catch_up_interval_beyond_grace_reschedules(db, register_qq_sender, sent):
    """所有错过点都在宽限窗外：不补跑，仅重算 next_run（8h 间隔，错过点落在 6h 窗外）"""
    _, sid = await _create(db, name="超窗任务", trigger={"type": "interval", "expr": "8h"})
    _set_next_run(db, sid, time.time() - 15 * 3600)  # 错过点在 15h 前，下次本应在 7h 前（窗外）

    await _start_and_drain(db)
    row = db._get_schedule_sync(sid)
    assert row["status"] == "active"
    assert sent == []
    assert row["last_run_at"] is None
    assert row["next_run_at"] == pytest.approx(time.time() + 8 * 3600, abs=60)


async def test_catch_up_interval_partial_window(db, register_qq_sender, sent):
    """部分错过点在窗外：只补窗口内的（60m 间隔错过 6.5h → 窗内 6 个点）"""
    _, sid = await _create(db, name="半窗任务", trigger={"type": "interval", "expr": "60m"})
    _set_next_run(db, sid, time.time() - int(6.5 * 3600))

    await _start_and_drain(db)
    row = db._get_schedule_sync(sid)
    assert row["status"] == "active"
    assert sent == ["hi"] * 6


async def test_catch_up_interval_invalid_expr_archives(db, register_qq_sender, sent):
    """启动补跑时 interval 表达式失效 → 自动归档"""
    _, sid = await _create(db, name="坏表达式", trigger={"type": "interval", "expr": "60m"})
    with db._connect() as conn:
        conn.execute(
            "UPDATE schedules SET trigger_expr = ?, next_run_at = ? WHERE id = ?",
            ("不是表达式", time.time() - 60, sid),
        )

    await _start_and_drain(db)
    assert await _wait_until(lambda: db._get_schedule_sync(sid)["status"] == "inactive")
    assert sent == []
    detail = await db.get_schedule_detail(sid)
    assert detail["status"] == "inactive"  # 默认列表仍不展示归档任务


async def test_catch_up_interval_cap(db, register_qq_sender, sent):
    """错过点数超过 MAX_CATCH_UP_RUNS → 只补最近的 20 个"""
    db.interval_catch_up_grace_seconds = 1000 * 3600  # 放大窗口制造海量错过点
    _, sid = await _create(db, name="海量错过", trigger={"type": "interval", "expr": "1m"})
    _set_next_run(db, sid, time.time() - 100 * 3600)

    await _start_and_drain(db)
    row = db._get_schedule_sync(sid)
    assert row["status"] == "active"
    assert len(sent) == 20


# ---------- 防重入与调度循环 ----------


async def test_spawn_job_skips_when_running(db, register_qq_sender):
    _, sid = await _create(db, name="在跑")
    row = db._get_schedule_sync(sid)
    db._active_jobs.add(sid)

    receipt = await db._run_schedule(row, "scheduled", wait=False)
    assert receipt["run_status"] == "busy"
    assert len(db._job_tasks) == 0  # 被跳过，未派发

    db._active_jobs.discard(sid)
    receipt = await db._run_schedule(row, "scheduled", wait=False)
    assert receipt["run_status"] == "accepted"
    assert len(db._job_tasks) == 1
    await db.stop()


async def test_loop_fires_due_job(db, register_qq_sender, sent):
    db._poll_interval = 0.05
    _, sid = await _create(db, name="快速任务", trigger={"type": "interval", "expr": "0.2s"})
    assert sent == []

    await db.run()
    try:
        assert await _wait_until(lambda: len(sent) >= 2, timeout=5.0)
    finally:
        await db.stop()
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "ok"
    assert detail["recent_runs"][0]["trigger"] == "scheduled"


# ---------- 工具层 ----------


async def test_schedule_update_tool(db, register_qq_sender, dummy_tool):
    import runtime.scheduler as scheduler_mod
    from tools import schedule_list, schedule_update

    schedule_list.register_tools(registry)
    schedule_update.register_tools(registry)

    scheduler_mod._scheduler_instance = db  # 工具层走单例，注入测试实例

    result = (
        await registry.execute(
            "schedule_update",
            {"operations": [{"op": "create_schedule", "name": "工具创建",
                             "trigger": {"type": "cron", "expr": "0 9 * * 1"},
                             "actions": [{"type": "tool", "name": "dummy_tool"}]}]},
        )
    )
    sid = (await db.list_active_schedules())[0]["id"]
    assert result.startswith(f"成功:1\n[1]S{sid} | next_run_at:")

    listing = await registry.execute("schedule_list", {})
    assert f"[S{sid}]工具创建 | cron: 0 9 * * 1" in listing

    detail = await registry.execute("schedule_list", {"schedule_id": sid})
    assert "cron:0 9 * * 1" in detail
    assert 'actions:[{"type":"tool","name":"dummy_tool"}]' in detail

    missing = json.loads(await registry.execute("schedule_list", {"schedule_id": 999}))
    assert missing["status"] == "error"


async def test_tool_schedule_history_is_opt_in(db, register_qq_sender, monkeypatch):
    from tools.schedule_list import schedule_list

    monkeypatch.setattr("tools.schedule_list.get_scheduler", lambda: db)
    _, sid = await _create(db)
    now = time.time()
    for i in range(4):
        with db._connect() as conn:
            conn.execute("INSERT INTO schedule_runs(schedule_id,trigger,started_at,finished_at,status,result_summary) VALUES(?,?,?,?,?,?)",
                         (sid, "manual", now, now, "ok", f"synthetic run {i}"))
    internal = await db.get_schedule_detail(sid)
    assert len(internal["recent_runs"]) == 4
    default = await schedule_list({"schedule_id": sid})
    assert "recent_runs:" not in default
    explicit = await schedule_list({"schedule_id": sid, "run_limit": 2})
    runs = json.loads(explicit.split("recent_runs:", 1)[1])
    assert [run["result_summary"] for run in runs] == ["synthetic run 3", "synthetic run 2"]
    assert len((await db.get_schedule_detail(sid))["recent_runs"]) == 4


@pytest.mark.parametrize("args", [{"run_limit": 1}, {"run_limit": True}, {"run_limit": -1},
                                 {"run_limit": 11}, {"run_limit": "2"}])
async def test_invalid_history_query_does_not_open_scheduler(args, monkeypatch):
    from tools.schedule_list import schedule_list

    monkeypatch.setattr("tools.schedule_list.get_scheduler", lambda: pytest.fail("invalid query reached scheduler"))
    assert json.loads(await schedule_list(args))["error"]


# ---------- 鲁棒性回归 ----------


async def test_preread_failure_notifies_with_snapshot(db, register_qq_sender, sent, monkeypatch):
    """执行前预读 DB 失败：不炸 UnboundLocalError，通知走派发时快照。"""
    _, sid = await _create(
        db, name="预读失败",
        actions=[message("qq", "不会执行")],
        on_failure={"notify_platform": "qq"},
    )
    row = db._get_schedule_sync(sid)

    def _boom(schedule_id):
        raise RuntimeError("db locked")

    monkeypatch.setattr(db, "_get_schedule_sync", _boom)
    await db._run_schedule(row, "scheduled")
    monkeypatch.undo()
    assert sid not in db._active_jobs
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "error"
    assert "db locked" in detail["last_error"]
    assert len(sent) == 1



async def test_record_failure_does_not_escape(db, register_qq_sender, monkeypatch):
    """运行记录/通知等收尾环节抛异常只记日志，不逃逸出任务协程。"""
    _, sid = await _create(db, name="记录失败")
    row = db._get_schedule_sync(sid)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "_record_run_sync", _boom)
    await db._run_schedule(row, "scheduled")
    monkeypatch.undo()

    assert sid not in db._active_jobs
    detail = await db.get_schedule_detail(sid)
    assert detail["recent_runs"][0]["status"] == "running"


@pytest.mark.parametrize(
    "payload",
    [
        '处理完成，未发现 "error" 线索',  # 纯文本含 "error" 字样
        json.dumps({"error": None}),  # error 为 null
        json.dumps({"data": {"error": "无"}}),  # error 只在嵌套层
    ],
)
async def test_tool_error_lookalike_is_success(db, payload):
    """ToolRegistry 契约：只有顶层非空 error 才算失败，其余一律成功。"""
    registry.register(
        name="lookalike_tool",
        description="test",
        parameters={"type": "object"},
        func=lambda args: payload,
    )
    _, sid = await _create(
        db, name="假阳性", actions=[{"type": "tool", "name": "lookalike_tool", "args": {}}]
    )
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    assert (await db.get_schedule_detail(sid))["last_status"] == "ok"


async def test_tool_error_json_stops_actions(db):
    """真失败（顶层 error 非空）仍中止后续动作。"""
    calls = []
    registry.register(
        name="bad_tool",
        description="test",
        parameters={"type": "object"},
        func=lambda args: json.dumps({"error": "boom"}),
    )
    registry.register(
        name="next_tool",
        description="test",
        parameters={"type": "object"},
        func=lambda args: calls.append(args) or "ok",
    )
    _, sid = await _create(
        db,
        name="真失败",
        actions=[
            {"type": "tool", "name": "bad_tool", "args": {}},
            {"type": "tool", "name": "next_tool", "args": {}},
        ],
    )
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    assert calls == []
    detail = await db.get_schedule_detail(sid)
    assert detail["last_status"] == "error"
    assert "boom" in detail["last_error"]


async def test_failure_notify_supports_arbitrary_external_channel_and_rejects_reserved(db):
    tg_sent = []

    async def _tg_sender(text: str):
        tg_sent.append(text)
        return {"sent_chunks": 1, "total_chunks": 1}

    from tools.send_message import register_sender
    register_sender("telegram", _tg_sender)

    for bad in ("web", "system", "", "bad platform"):
        res = await db.update([{
            "op": "create_schedule",
            "name": "非法通知",
            "trigger": {"type": "interval", "expr": "60m"},
            "actions": [{"type": "command", "command": "exit 1"}],
            "on_failure": {"notify_platform": bad},
        }])
        assert res["results"][0]["status"] == "error"

    res, sid = await _create(
        db,
        name="TG 失败告警",
        actions=[{"type": "command", "command": "exit 1"}],
        on_failure={"notify_platform": "telegram"},
    )
    assert res["results"][0]["status"] == "ok"
    await db._run_schedule(db._get_schedule_sync(sid), "scheduled")
    assert len(tg_sent) == 1 and "TG 失败告警" in tg_sent[0]
