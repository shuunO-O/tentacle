"""
task_update 工具

让主模型创建、修改、删除任务（task）。
注意：completed 的 task 不可修改。
task 标记 completed 成功后，返回值附带其所属 goal 的剩余未完成任务，提示下一步。
"""

import json
import logging

from runtime.goals import get_goal_manager
from tools._output import batch_text

logger = logging.getLogger(__name__)


async def _build_completed_followups(operations: list[dict], results: list[dict]) -> list[dict]:
    """
    为已提交的 update_task(completed) 按 goal 合并剩余任务摘要。

    Returns:
        摘要列表，每项包含 completed_task_ids / goal_id / goal_description / remaining_tasks。
    """
    # 批量失败会整体回滚，前序 status=ok 不代表任务已完成。
    if any(result.get("status") != "ok" for result in results):
        return []
    manager = get_goal_manager()
    completed_by_goal = {}
    followups = []
    for result in results:
        index = result.get("index")
        if index is None or index >= len(operations):
            continue
        op = operations[index]
        if op.get("op") != "update_task" or op.get("status") != "completed":
            continue
        if result.get("status") != "ok":
            continue
        task_id = result.get("task_id")
        if task_id is None:
            continue
        try:
            task = await manager.get_task(task_id)
            if task is None:
                continue
            completed = completed_by_goal.setdefault(task["goal_id"], [])
            if task_id not in completed:
                completed.append(task_id)
        except Exception as e:
            logger.warning(f"读取已完成任务失败 task_id={task_id}: {e}")

    for goal_id, task_ids in completed_by_goal.items():
        try:
            goals = await manager.list_active_goals(goal_id)
            if not goals:
                continue
            goal = goals[0]
            followups.append(
                {
                    "completed_task_ids": task_ids,
                    "goal_id": goal_id,
                    "goal_description": goal["description"],
                    "remaining_tasks": [
                        {
                            "id": t["id"],
                            "content": t["content"],
                            "status": t["status"],
                            "urgency": t["urgency"],
                            "start_time": t.get("start_time"),
                        }
                        for t in goal["tasks"]
                    ],
                }
            )
        except Exception as e:
            logger.warning(f"构建完成后续摘要失败 goal_id={goal_id}: {e}")
    return followups


async def task_update(args: dict) -> str:
    """
    批量操作任务。

    Args:
        args: 参数字典
            - operations: 操作列表，每个操作包含 op 字段

    Returns:
        紧凑操作回执；完成任务时按 goal 附带剩余任务，失败返回 JSON error。
        任何一步失败都会整体回滚。
    """
    operations = args.get("operations", [])
    if not isinstance(operations, list) or not operations:
        return json.dumps(
            {"status": "error", "error": "operations 必须是非空数组"},
            ensure_ascii=False,
        )

    try:
        manager = get_goal_manager()
        result = await manager.update(operations)
        if result.get("status") == "ok":
            followups = await _build_completed_followups(operations, result.get("results") or [])
            if followups:
                result["completed_followups"] = followups
        return batch_text(result)
    except Exception as e:
        logger.error(f"task_update 异常: {e}", exc_info=True)
        return json.dumps(
            {"status": "error", "error": f"更新任务失败: {str(e)}"},
            ensure_ascii=False,
        )


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="task_update",
        description="创建、修改、删除隶属于 active goal 的任务（task）。置 status=completed 后返回所属 goal 剩余任务。",
        parameters={
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "要执行的操作列表，按顺序执行，任意一步失败会整体回滚。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["create_task", "update_task", "delete_task"],
                                "description": "操作类型。",
                            },
                            "goal_id": {
                                "type": "integer",
                                "description": "create_task 时必填（所属 goal）；update_task 可选（迁移到其他 goal）。",
                            },
                            "task_id": {
                                "type": "integer",
                                "description": "update_task / delete_task 时必填。",
                            },
                            "content": {
                                "type": "string",
                                "description": "task 内容。create_task 必填，update_task 可选。",
                            },
                            "urgency": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "task 紧急程度。create_task 必填，update_task 可选。",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "task 状态。update_task 可选；completed 不可逆。",
                            },
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["operations"],
        },
        func=task_update,
        is_async=True,
    )
