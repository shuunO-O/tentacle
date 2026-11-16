"""
goal_update 工具

让主模型创建、修改目标（goal）。
注意：inactive 的 goal 不可修改；goal 不物理删除，归档即 status=inactive。
"""

import json
import logging

from runtime.goals import get_goal_manager
from tools._output import batch_text

logger = logging.getLogger(__name__)


async def goal_update(args: dict) -> str:
    """
    批量操作目标。

    Args:
        args: 参数字典
            - operations: 操作列表，每个操作包含 op 字段

    Returns:
        成功按输入顺序返回编号和 ID；失败返回 JSON error。
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
        return batch_text(result)
    except Exception as e:
        logger.error(f"goal_update 异常: {e}", exc_info=True)
        return json.dumps(
            {"status": "error", "error": f"更新目标失败: {str(e)}"},
            ensure_ascii=False,
        )


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="goal_update",
        description="创建、修改目标（goal）；归档用 status=inactive（不可逆，不物理删除）。",
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
                                "enum": ["create_goal", "update_goal"],
                                "description": "操作类型。",
                            },
                            "goal_id": {
                                "type": "integer",
                                "description": "update_goal 时必填。",
                            },
                            "description": {
                                "type": "string",
                                "description": "goal 描述。create_goal 必填，update_goal 可选。",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "goal 优先级。create_goal 必填，update_goal 可选。",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["active", "inactive"],
                                "description": "goal 状态。update_goal 可选；inactive 即归档，不可逆。",
                            },
                            "result": {
                                "type": "string",
                                "description": "goal 当前进展情况。update_goal 可选。",
                            },
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["operations"],
        },
        func=goal_update,
        is_async=True,
    )
