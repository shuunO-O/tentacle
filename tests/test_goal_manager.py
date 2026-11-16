"""
GoalManager 测试

覆盖：批量操作、失败整体回滚、不可逆约束（inactive goal / completed task）、
get_task（含 completed）、排序规则、get_goal_summary。
全部走 tmp_path 临时库，不碰生产 data/chronicle.db。
"""


def _first_op(result):
    """取批量结果中的第一条操作结果。"""
    return result["results"][0]


async def _make_goal_with_tasks(manager, n_tasks=2):
    """辅助：建一个 goal + n 个 task，返回 (goal_id, [task_id])。"""
    result = await manager.update([
        {"op": "create_goal", "description": "测试目标", "priority": "medium"},
    ])
    goal_id = result["results"][0]["goal_id"]

    ops = [
        {"op": "create_task", "goal_id": goal_id, "content": f"任务{i}", "urgency": "medium"}
        for i in range(1, n_tasks + 1)
    ]
    result = await manager.update(ops)
    task_ids = [r["task_id"] for r in result["results"]]
    return goal_id, task_ids


# ---------- 基础 CRUD ----------


async def test_create_goal_and_tasks(goal_db):
    goal_id, task_ids = await _make_goal_with_tasks(goal_db, n_tasks=3)
    assert goal_id == 1
    assert task_ids == [1, 2, 3]

    goals = await goal_db.list_active_goals()
    assert len(goals) == 1
    assert goals[0]["description"] == "测试目标"
    assert len(goals[0]["tasks"]) == 3
    assert all(t["status"] == "pending" for t in goals[0]["tasks"])


async def test_create_task_rejects_missing_goal(goal_db):
    result = await goal_db.update([
        {"op": "create_task", "goal_id": 999, "content": "孤儿任务", "urgency": "low"},
    ])
    assert _first_op(result)["status"] == "error"


async def test_create_task_requires_fields(goal_db):
    result = await goal_db.update([{"op": "create_task", "content": "缺 goal_id"}])
    assert _first_op(result)["status"] == "error"


async def test_unknown_op_rejected(goal_db):
    result = await goal_db.update([{"op": "drop_table"}])
    assert _first_op(result)["status"] == "error"


# ---------- 批量回滚 ----------


async def test_batch_rollback_on_failure(goal_db):
    """批量中任何一步失败，前面已执行的步骤也回滚。"""
    goal_id, task_ids = await _make_goal_with_tasks(goal_db, n_tasks=1)

    # step1 合法（完成 task1），step2 非法（未知 op）→ 整体回滚
    result = await goal_db.update([
        {"op": "update_task", "task_id": task_ids[0], "status": "completed"},
        {"op": "not_an_op"},
    ])
    # 回滚明确返回失败，不能留下外层成功回执。
    assert result["status"] == "error"
    assert result["rolled_back"] is True
    assert result["results"][1]["status"] == "error"

    # 回滚验证：task1 仍是 pending
    task = await goal_db.get_task(task_ids[0])
    assert task["status"] == "pending"


async def test_batch_all_ok_commits(goal_db):
    goal_id, task_ids = await _make_goal_with_tasks(goal_db, n_tasks=2)
    result = await goal_db.update([
        {"op": "update_task", "task_id": task_ids[0], "status": "in_progress"},
        {"op": "update_goal", "goal_id": goal_id, "result": "进行中"},
    ])
    assert result["status"] == "ok"
    goal = await goal_db.get_goal_summary(goal_id)
    assert goal["result"] == "进行中"


# ---------- 不可逆约束 ----------


async def test_inactive_goal_not_editable(goal_db):
    goal_id, _ = await _make_goal_with_tasks(goal_db)
    await goal_db.update([{"op": "update_goal", "goal_id": goal_id, "status": "inactive"}])

    # inactive 后拒绝任何修改
    result = await goal_db.update([
        {"op": "update_goal", "goal_id": goal_id, "description": "改名"},
    ])
    assert _first_op(result)["status"] == "error"

    # 也不再出现在 active 列表
    goals = await goal_db.list_active_goals()
    assert goals == []


async def test_completed_task_not_editable(goal_db):
    _, task_ids = await _make_goal_with_tasks(goal_db, n_tasks=1)
    await goal_db.update([{"op": "update_task", "task_id": task_ids[0], "status": "completed"}])

    result = await goal_db.update([
        {"op": "update_task", "task_id": task_ids[0], "status": "pending"},
    ])
    assert _first_op(result)["status"] == "error"


async def test_task_migration_requires_active_goal(goal_db):
    goal_id, task_ids = await _make_goal_with_tasks(goal_db, n_tasks=1)
    # 迁移到不存在的 goal 应失败
    result = await goal_db.update([
        {"op": "update_task", "task_id": task_ids[0], "goal_id": 999},
    ])
    assert _first_op(result)["status"] == "error"


# ---------- get_task / get_goal_summary ----------


async def test_get_task_includes_completed(goal_db):
    _, task_ids = await _make_goal_with_tasks(goal_db, n_tasks=1)
    await goal_db.update([{"op": "update_task", "task_id": task_ids[0], "status": "completed"}])

    task = await goal_db.get_task(task_ids[0])
    assert task is not None
    assert task["status"] == "completed"  # completed 也可查（供 task_update 回调用）
    assert task["goal_id"] == 1


async def test_get_task_missing(goal_db):
    assert await goal_db.get_task(42) is None


async def test_get_goal_summary_missing(goal_db):
    assert await goal_db.get_goal_summary(42) is None


# ---------- 排序 ----------


async def test_sorting_priority_and_status(goal_db):
    """goal 按 priority 降序；task 先 in_progress 后 pending，再按 urgency 降序。"""
    await goal_db.update([
        {"op": "create_goal", "description": "低优先", "priority": "low"},
        {"op": "create_goal", "description": "高优先", "priority": "high"},
    ])
    # 两个 goal 的 id
    goals = await goal_db.list_active_goals()
    low_id = next(g["id"] for g in goals if g["priority"] == "low")
    high_id = next(g["id"] for g in goals if g["priority"] == "high")

    await goal_db.update([
        {"op": "create_task", "goal_id": low_id, "content": "低紧急pending", "urgency": "low"},
        {"op": "create_task", "goal_id": high_id, "content": "高紧急pending", "urgency": "high"},
        {"op": "create_task", "goal_id": high_id, "content": "低紧急pending", "urgency": "low"},
        {"op": "create_task", "goal_id": high_id, "content": "进行中任务", "urgency": "low"},
    ])
    # 把最后一个标为 in_progress
    goals = await goal_db.list_active_goals()
    in_progress_id = next(
        t["id"] for g in goals for t in g["tasks"] if t["content"] == "进行中任务"
    )
    await goal_db.update([
        {"op": "update_task", "task_id": in_progress_id, "status": "in_progress"},
    ])

    goals = await goal_db.list_active_goals()
    assert [g["priority"] for g in goals] == ["high", "low"]
    high_goal = goals[0]
    # in_progress 最前，随后 pending 按 urgency 降序
    assert [t["content"] for t in high_goal["tasks"]] == [
        "进行中任务",
        "高紧急pending",
        "低紧急pending",
    ]
