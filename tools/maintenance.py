"""Inspect and request the fixed Tentacle host maintenance operations."""

import json

from runtime.maintenance_protocol import ACTIONS, valid_id

_controller = None


def bind(controller):
    global _controller
    _controller = controller


TOOL_BINDINGS = {"maintenance": bind}


async def maintenance(args):
    action = args.get("action")
    version = args.get("version")
    operation_id = args.get("operation_id")
    if _controller is None:
        return json.dumps({"error": "当前实例未装配主机维护模块"}, ensure_ascii=False)
    try:
        if action == "status":
            if version is not None or (operation_id is not None and not valid_id(operation_id)):
                raise ValueError("状态查询只接受可选的有效操作 ID")
            return json.dumps(_controller.snapshot(operation_id), ensure_ascii=False)
        if operation_id is not None:
            raise ValueError("只有查询状态时可以指定操作 ID")
        operation = _controller.reserve(action, version, from_round=True)
        if action == "check":
            _controller.start()
        return json.dumps({"operation": operation,
                           "message": "已登记检查请求" if action == "check" else "已预约，本轮正常结束后交给宿主执行"},
                          ensure_ascii=False)
    except ValueError as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False)


def register_tools(registry):
    registry.register(
        name="maintenance",
        description="查询 Tentacle 生产版本与维护状态、检查正式更新，或按明确要求预约重启/升级。"
                    "升级须指定已检查的正式版本；重启和升级在本轮正常结束后执行，收到预约回执后说明并结束本轮即可。",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", *ACTIONS]},
                "version": {"type": "string", "description": "仅 update 需要，已检查的目标版本"},
                "operation_id": {"type": "string", "description": "仅 status 可用，查询指定操作"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        func=maintenance,
        requires=("maintenance",),
    )
