"""紧凑回执的业务边界：临时目标、模拟发送/终端，不启动服务。"""

import json
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runtime.tool_registry import tool_result_error
from tools import goal_update, keep_in_mind, plan_view, schedule_list, schedule_update, send_message


async def test_goal_receipts_and_plan_preserve_content_and_rollback(goal_db, monkeypatch):
    monkeypatch.setattr(goal_update, "get_goal_manager", lambda: goal_db)
    monkeypatch.setattr(plan_view, "get_goal_manager", lambda: goal_db)
    created = await goal_update.goal_update({"operations": [
        {"op": "create_goal", "description": "synthetic goal\n完整第二行", "priority": "high"},
    ]})
    gid = (await goal_db.list_active_goals())[0]["id"]
    assert created == f"成功:1\n[1]G{gid}"
    tasks = await goal_db.update([
        {"op": "create_task", "goal_id": gid, "content": "完整任务", "urgency": "low"},
        {"op": "update_goal", "goal_id": gid, "result": "完整进展"},
    ])
    tid = tasks["results"][0]["task_id"]
    await goal_db.update([{"op": "update_task", "task_id": tid, "status": "in_progress"}])
    result = await plan_view.plan_view({})
    assert f"[G{gid}]synthetic goal\n完整第二行 | 优先级:high" in result
    assert "结果:完整进展" in result
    assert f"[T{tid}]完整任务 | in_progress | 紧急度:low | 开始:" in result
    assert "goal_id" not in result
    failure = await goal_update.goal_update({"operations": [
        {"op": "update_goal", "goal_id": gid, "result": "必须回滚"},
        {"op": "update_goal", "goal_id": 99999, "result": "失败"},
    ]})
    assert tool_result_error(failure)
    decoded = json.loads(failure)
    assert decoded["rolled_back"] is True
    assert decoded["results"][0] == {"status": "rolled_back"}
    assert decoded["results"][1]["index"] == 1
    assert (await goal_db.list_active_goals())[0]["result"] == "完整进展"


async def test_schedule_receipt_preserves_accepted_run_and_unknown_failure(monkeypatch):
    manager = SimpleNamespace(update=AsyncMock(return_value={"status": "ok", "results": [
        {"status": "ok", "index": 0, "schedule_id": 8, "run_id": 12, "run_status": "accepted",
         "message": "已接收执行，结果用 schedule_list 查看"},
    ]}))
    monkeypatch.setattr(schedule_update, "get_scheduler", lambda: manager)
    result = await schedule_update.schedule_update({"operations": [{"op": "run_schedule", "schedule_id": 8}]})
    assert "S8" in result and "run_id:12" in result and 'run_status:accepted' in result
    assert "完成" not in result
    manager.update.return_value = {"error": "上次执行结果未知，请确认可能重复", "status": "error"}
    assert "结果未知" in tool_result_error(await schedule_update.schedule_update({"operations": [{}]}))


async def test_schedule_detail_keeps_action_values_and_failure_history(monkeypatch):
    detail = {"id": 8, "name": "synthetic", "status": "inactive",
              "trigger": {"type": "once", "expr": "2026-10-23 10:00"},
              "actions": [{"type": "tool", "name": "synthetic", "args": {"empty": "", "flag": False, "n": 0}}],
              "max_runtime": 30, "on_failure": {"notify_platform": "qq"}, "next_run": "", "last_run": "10.23 10:00",
              "last_status": "unknown", "last_error": "synthetic error\n全部保留",
              "recent_runs": [{"run_id": 2, "status": "unknown", "result_summary": "部分动作可能完成"}]}
    manager = SimpleNamespace(get_schedule_detail=AsyncMock(side_effect=lambda *a, **kw: dict(detail)))
    monkeypatch.setattr(schedule_list, "get_scheduler", lambda: manager)
    result = await schedule_list.schedule_list({"schedule_id": 8, "run_limit": 1})
    assert "状态:inactive" in result and "unknown" in result
    assert '"empty":"","flag":false,"n":0' in result
    assert "max_runtime:30" in result and 'on_failure:{"notify_platform":"qq"}' in result
    assert "synthetic error\n全部保留" in result and "部分动作可能完成" in result
    default = await schedule_list.schedule_list({"schedule_id": 8})
    assert "recent_runs:" not in default and "下次:" not in default
    assert "recent_runs" in detail  # 工具投影不更改原始管理器数据。


async def test_send_receipts_keep_files_and_reject_unconfirmed_delivery(monkeypatch):
    sender = AsyncMock(return_value={"sent_chunks": 3, "total_chunks": 3, "sent_files": 1})
    monkeypatch.setattr(send_message, "_senders", {"qq": sender})
    args = {"platform": "qq", "text": "synthetic"}
    assert await send_message.send_message(args) == "已发送:3段 | 附件:1"
    sender.side_effect = TimeoutError("已确认2/3段，失败段状态未知，请勿整条重发")
    assert "已确认2/3段" in tool_result_error(await send_message.send_message(args))
    assert sender.await_count == 2  # 每次调用只发送一次，不自动重试。
    sender.side_effect = None
    sender.return_value = {"sent_chunks": 2, "total_chunks": 3}
    failure = json.loads(await send_message.send_message(args))
    assert failure["error"] and failure["sent_chunks"] == 2


def test_keep_in_mind_receipts_do_not_echo_entries_and_list_is_complete(monkeypatch):
    entries = []

    def add(section, entry):
        if entry in entries:
            return False
        entries.append(entry)
        return True

    def remove(section, entry):
        if entry not in entries:
            return False
        entries.remove(entry)
        return True

    monkeypatch.setattr(keep_in_mind, "_builder", SimpleNamespace(
        add_entry=add, remove_entry=remove, list_entries=lambda section: entries,
    ))
    args = {"section": "memory", "entry": "完整合成条目\n第二行"}
    assert keep_in_mind.keep_in_mind({**args, "action": "add"}) == "已添加"
    assert keep_in_mind.keep_in_mind({**args, "action": "add"}) == "已存在"
    assert keep_in_mind.keep_in_mind({**args, "action": "list"}) == "- 完整合成条目\n第二行"
    assert keep_in_mind.keep_in_mind({**args, "action": "remove"}) == "已删除"
    assert keep_in_mind.keep_in_mind({**args, "action": "remove"}) == "未找到"
    assert tool_result_error(keep_in_mind.keep_in_mind({**args, "action": "invalid"}))


@pytest.fixture
def terminal(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("PTY/termios 仅 Unix；终端回执由 Linux CI 验证")
    from runtime.terminal_manager import TerminalManager

    tm = object.__new__(TerminalManager)
    import threading
    tm._lock = threading.Lock()
    tm._master_fd = 123
    tm._default_timeout = 30
    monkeypatch.setattr(tm, "ensure_session", lambda: None)
    monkeypatch.setattr(tm, "_safety_check", lambda command: None)
    monkeypatch.setattr(tm, "_protected_write_check", lambda command: None)
    return tm


@pytest.mark.parametrize("code", [0, 1])
def test_terminal_keeps_exit_code_and_program_output(terminal, monkeypatch, code):
    writes = []
    monkeypatch.setattr('runtime.terminal_manager.os.write', lambda fd, data: writes.append(data.decode()))

    def read(timeout):
        sentinel = re.search(r"__TENTACLE_DONE_([0-9a-f]{8})_", writes[0])[1]
        return f"\x1b[31m完整输出\x1b[0m\r\n__TENTACLE_DONE_{sentinel}_{code}__\r\n", False

    monkeypatch.setattr(terminal, "_read_available", read)
    assert terminal._run_sync("echo synthetic", 30, None) == f"退出码:{code}\n完整输出"


def test_terminal_timeout_and_eof_remain_distinct(terminal, monkeypatch):
    monkeypatch.setattr('runtime.terminal_manager.os.write', lambda *args: None)
    ticks = iter([0, 0, 2])
    monkeypatch.setattr('runtime.terminal_manager.time.monotonic', lambda: next(ticks))
    monkeypatch.setattr(terminal, "_read_available", lambda timeout: ("进度120/500\r\n", False))
    result = terminal._run_sync("synthetic", 1, None)
    assert result == "仍在运行 | 已等待:1s\n进度120/500"
    monkeypatch.setattr(terminal, "_read_available", lambda timeout: ("", True))
    assert terminal._interact_sync("", False, 0) == "终端已退出"
    monkeypatch.setattr(terminal, "_read_available", lambda timeout: ("", False))
    assert terminal._interact_sync("", False, 0) == "暂无新输出"


def test_terminal_truncation_remains_visible(terminal):
    output = "开头" + "x" * 10000 + "结尾"
    result = terminal._truncate(output)
    assert result.startswith("开头") and result.endswith("结尾") and "已截断" in result
