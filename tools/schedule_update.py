"""
schedule_update 工具

让主模型创建、修改、手动触发定时任务（schedule）。
注意：inactive 的 schedule 不可修改；schedule 不物理删除，归档即 status=inactive。
"""

import json
import logging

from runtime.scheduler import get_scheduler
from tools._output import batch_text

logger = logging.getLogger(__name__)


async def schedule_update(args: dict) -> str:
    """
    批量操作定时任务。

    Args:
        args: 参数字典
            - operations: 操作列表，每个操作包含 op 字段

    Returns:
        按输入顺序返回回执，保留 run_id/run_status；失败返回 JSON error。
        任何一步失败都会整体回滚。
    """
    operations = args.get("operations", [])
    if not isinstance(operations, list) or not operations:
        return json.dumps(
            {"status": "error", "error": "operations 必须是非空数组"},
            ensure_ascii=False,
        )

    try:
        manager = get_scheduler()
        result = await manager.update(operations)
        return batch_text(result)
    except Exception as e:
        logger.error(f"schedule_update 异常: {e}", exc_info=True)
        return json.dumps(
            {"status": "error", "error": f"更新定时任务失败: {str(e)}"},
            ensure_ascii=False,
        )


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="schedule_update",
        description="创建、修改、触发定时任务。tool/command 后台执行；heartbeat 唤醒主 Agent（仅 once/cron 且须为唯一动作）。归档用 status=inactive（不可逆）。外部发信可用 tool:send_message。",
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
                                "enum": ["create_schedule", "update_schedule", "run_schedule"],
                                "description": "操作类型。run_schedule 接收一次手动执行并返回 run_id；忙时明确拒绝。通常不改变定时段。"
                            },
                            "schedule_id": {
                                "type": "integer",
                                "description": "update_schedule / run_schedule 时必填。",
                            },
                            "name": {
                                "type": "string",
                                "description": "任务名。create_schedule 必填，update_schedule 可选。",
                            },
                            "acknowledge_unknown": {
                                "type": "boolean",
                                "description": "仅 run_schedule：上次执行结果未知时，确认可能重复后设 true 明确重试。默认 false。",
                            },
                            "trigger": {
                                "type": "object",
                                "description": "触发器 {type, expr}。create_schedule 必填，update_schedule 可选。type: cron（标准5字段表达式如 0 9 * * 1）/ interval（如 90s、30m、2h）/ once（ISO 本地时间如 2026-08-31 09:00，触发后自动归档）。",
                                "properties": {
                                    "type": {"type": "string", "enum": ["cron", "interval", "once"]},
                                    "expr": {"type": "string"},
                                },
                                "required": ["type", "expr"],
                            },
                            "actions": {
                                "type": "array",
                                "description": "按序执行、遇错停止。tool 调工具（如 send_message 配已接入 platform 与 text）；command 执行独立命令；heartbeat 只能单独使用（不支持 interval，不重置固定心跳）。无 notify 动作。",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string", "enum": ["tool", "command", "heartbeat"]},
                                        "name": {"type": "string", "description": "tool 的工具名"},
                                        "args": {"type": "object", "description": "tool 的参数"},
                                        "command": {"type": "string", "description": "command 的 shell 命令"},
                                        "timeout": {"type": "number", "description": "command 的超时秒数"},
                                        "content": {"type": "string", "description": "heartbeat 的非空预约事项"},
                                    },
                                    "required": ["type"],
                                },
                            },
                            "max_runtime": {
                                "type": "number",
                                "description": "tool/command 动作执行预算秒数，默认 600；heartbeat 使用现有心跳预算，不接受此字段。排队、落库及最多10秒失败通知另计。",
                            },
                            "on_failure": {
                                "type": "object",
                                "description": "失败通知 {notify_platform: <platform>}，经已接入通道插件的 send_message 工具发送；空对象清除。",
                                "properties": {"notify_platform": {"type": "string", "description": "已启用的外部消息平台名（如 qq、telegram 等）"}},
                            },
                            "status": {
                                "type": "string",
                                "enum": ["inactive"],
                                "description": "update_schedule 可选；inactive 即归档，不可逆。",
                            },
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["operations"],
        },
        func=schedule_update,
        is_async=True,
    )
