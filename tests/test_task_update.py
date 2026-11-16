"""完成回执按 goal 去重；只操作合成目标及临时数据库。"""

import json

from tools.task_update import task_update


async def make_goal(manager, count=3):
    goal = await manager.update([{"op": "create_goal", "description": "synthetic goal", "priority": "medium"}])
    goal_id = goal["results"][0]["goal_id"]
    result = await manager.update([
        {"op": "create_task", "goal_id": goal_id, "content": f"task {i}", "urgency": "medium"}
        for i in range(count)
    ])
    return goal_id, [item["task_id"] for item in result["results"]]


async def test_completed_tasks_share_one_goal_followup(goal_db, monkeypatch):
    monkeypatch.setattr("tools.task_update.get_goal_manager", lambda: goal_db)
    goal_id, task_ids = await make_goal(goal_db)
    result = await task_update({"operations": [
        {"op": "update_task", "task_id": task_id, "status": "completed"} for task_id in task_ids[:2]
    ]})
    assert result.startswith("成功:2\n")
    assert f"已完成:T{task_ids[0]},T{task_ids[1]}" in result
    assert result.count(f"[G{goal_id}]") == 1
    assert f"[G{goal_id}]synthetic goal | 剩余:1" in result
    assert f"[T{task_ids[2]}]task 2 | pending | 紧急度:medium" in result


async def test_followups_stay_separate_for_different_goals(goal_db, monkeypatch):
    monkeypatch.setattr("tools.task_update.get_goal_manager", lambda: goal_db)
    first_goal, first = await make_goal(goal_db, 1)
    second_goal, second = await make_goal(goal_db, 2)
    result = await task_update({"operations": [
        {"op": "update_task", "task_id": task_id, "status": "completed"} for task_id in [first[0], second[0]]
    ]})
    assert f"[G{first_goal}]synthetic goal | 剩余:0" in result
    assert f"[G{second_goal}]synthetic goal | 剩余:1" in result
    assert f"[T{second[1]}]task 1" in result


async def test_rolled_back_completion_has_no_followup(goal_db, monkeypatch):
    monkeypatch.setattr("tools.task_update.get_goal_manager", lambda: goal_db)
    _, task_ids = await make_goal(goal_db, 1)
    result = json.loads(await task_update({"operations": [
        {"op": "update_task", "task_id": task_ids[0], "status": "completed"},
        {"op": "update_task", "task_id": 999999, "status": "completed"},
    ]}))
    assert result["results"][1]["status"] == "error"
    assert "completed_followups" not in result
    assert (await goal_db.get_task(task_ids[0]))["status"] == "pending"


async def test_single_completion_receipt_has_no_duplicate_id(goal_db, monkeypatch):
    monkeypatch.setattr("tools.task_update.get_goal_manager", lambda: goal_db)
    gid, tasks = await make_goal(goal_db, 1)
    receipt = await task_update({"operations": [
        {"op": "update_task", "task_id": tasks[0], "status": "completed"},
    ]})
    assert receipt == f"已完成:T{tasks[0]}\n[G{gid}]synthetic goal | 剩余:0"
