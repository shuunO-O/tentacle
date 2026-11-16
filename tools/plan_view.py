"""
plan_view 工具

让主模型查看当前活跃的目标（goal）及其未完成的任务（task）。
"""

import json
import logging

from runtime.goals import get_goal_manager
from tools._output import plan_text

logger = logging.getLogger(__name__)


async def plan_view(args: dict) -> str:
    """
    查看当前活跃的目标及其未完成任务。

    Args:
        args: 参数字典
            - goal_id: 可选，只查看指定 goal

    Returns:
        按目标分组的紧凑文本；失败返回 JSON error。
    """
    goal_id = args.get("goal_id")
    if goal_id is not None:
        try:
            goal_id = int(goal_id)
        except (ValueError, TypeError):
            return json.dumps(
                {"status": "error", "error": "goal_id 必须是整数"},
                ensure_ascii=False,
            )

    try:
        manager = get_goal_manager()
        goals = await manager.list_active_goals(goal_id)
        return plan_text(goals)
    except Exception as e:
        logger.error(f"plan_view 异常: {e}", exc_info=True)
        return json.dumps(
            {"status": "error", "error": f"查询目标失败: {str(e)}"},
            ensure_ascii=False,
        )


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="plan_view",
        description="查看当前活跃 goal 及未完成 task。心跳触发或准备执行任务前可调用。",
        parameters={
            "type": "object",
            "properties": {
                "goal_id": {
                    "type": "integer",
                    "description": "可选，只查看指定 goal。不填则返回所有活跃目标。",
                },
            },
            "required": [],
        },
        func=plan_view,
        is_async=True,
    )
