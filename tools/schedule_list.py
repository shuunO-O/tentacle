"""
schedule_list 工具

让主模型查看当前活跃的定时任务（schedule）及运行记录。
"""

import json
import logging

from runtime.scheduler import get_scheduler
from tools._output import schedule_text

logger = logging.getLogger(__name__)


async def schedule_list(args: dict) -> str:
    """
    查看当前活跃的定时任务。

    Args:
        args: 参数字典
            - schedule_id: 可选，只查看指定任务
            - run_limit: 可选，附带最近几次运行记录，默认 0

    Returns:
        紧凑任务概览或详情；失败返回 JSON error。
    """
    schedule_id = args.get("schedule_id")
    run_limit = args.get("run_limit", 0)
    if type(run_limit) is not int or not 0 <= run_limit <= 10:
        return json.dumps({"error": "run_limit 必须是 0..10 的整数"}, ensure_ascii=False)
    if run_limit and schedule_id is None:
        return json.dumps({"error": "查询运行历史须提供 schedule_id"}, ensure_ascii=False)
    if schedule_id is not None:
        if type(schedule_id) is not int or schedule_id <= 0:
            return json.dumps(
                {"status": "error", "error": "schedule_id 必须是整数"},
                ensure_ascii=False,
            )

    try:
        manager = get_scheduler()
        if schedule_id is None:
            schedules = await manager.list_active_schedules()
            return schedule_text(schedules)
        detail = await manager.get_schedule_detail(schedule_id, run_limit=run_limit)
        if detail is None:
            return json.dumps(
                {"status": "error", "error": f"schedule_id={schedule_id} 不存在"},
                ensure_ascii=False,
            )
        if not run_limit:
            detail.pop("recent_runs", None)
        return schedule_text([detail], detail=True)
    except Exception as e:
        logger.error(f"schedule_list 异常: {e}", exc_info=True)
        return json.dumps(
            {"status": "error", "error": f"查询定时任务失败: {str(e)}"},
            ensure_ascii=False,
        )


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="schedule_list",
        description="查看定时任务：不填 schedule_id 列出活跃任务；传 ID 可查活跃/归档任务及运行历史（含失败与结果未知）。",
        parameters={
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "integer",
                    "description": "只查看指定任务；不填返回全部概览",
                },
                "run_limit": {"type": "integer", "minimum": 0, "maximum": 10,
                              "description": "附带最近运行次数，默认0；须指定 schedule_id"},
            },
            "required": [],
        },
        func=schedule_list,
        is_async=True,
    )
