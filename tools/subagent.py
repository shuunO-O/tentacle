"""主 agent 的子任务管理工具。工具函数不等待第二次确认，不注册 wait_task。"""

import json

from agent.loop import current_tool_context

_manager = None


def set_manager(manager):
    global _manager
    _manager = manager


def _runtime():
    if _manager is None:
        raise RuntimeError("子代理系统未启用")
    return _manager


def _parent():
    current = current_tool_context.get()
    if current is None or current[0].platform == "subagent":
        raise PermissionError("创建和追问子任务只能由主 agent 发起，不能由 scheduler 或子代理直接执行")
    return current


def _receipt(result):
    """仅投影模型需要的回执；用量、输入和投递状态仍由 manager 完整保存。"""
    receipt = {key: result[key] for key in ("task_id", "turn_id", "status", "result", "error", "cleanup_error", "dsh_session_id", "cwd", "dsh_running")
               if key in result and result[key] is not None}
    receipt.update({key: True for key in ("background", "wait_timed_out") if result.get(key) is True})
    return json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))


async def subagent_create(args):
    context, _ = _parent()
    if args.get("include_recent_messages"):
        raise ValueError("请在 background 中概述 coding 任务所需背景")
    result = await _runtime().create(args["objective"], cwd=args.get("cwd"), background=args.get("background", ""),
                                     wait=args.get("wait", False),
                                     platform=context.platform, parent_session=context.session_id,
                                     wake_depth=context.subagent_depth)
    return _receipt(result)


async def subagent_followup(args):
    context, _ = _parent()
    result = await _runtime().followup(args["task_id"], args["message"], wait=args.get("wait", False),
                                      wake_depth=context.subagent_depth)
    return _receipt(result)


async def subagent_attach(args):
    context, _ = _parent()
    result = await _runtime().attach(args["session_id"], platform=context.platform, parent_session=context.session_id)
    return _receipt(result)


async def subagent_view(args):
    task_id = args.get("task_id")
    result = await _runtime().view(task_id, include_history=args.get("include_history", False),
                                  include_archived=args.get("include_archived", False),
                                  limit=args.get("limit", 1 if task_id else 20), compact=True,
                                  include_usage=args.get("include_usage", False))
    if task_id and args.get("include_dsh_history"):
        result.update(await _runtime().coding_history(task_id, args.get("limit", 8)))
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


async def subagent_cancel(args):
    result = await _runtime().cancel(args["task_id"])
    return json.dumps(result, ensure_ascii=False)


async def subagent_archive(args):
    result = await _runtime().cancel(args["task_id"], archive=True)
    return json.dumps(result, ensure_ascii=False)


_WAIT = {"type": "boolean", "description": "默认 false，后台执行；true 等待本轮结果，等待超时不取消子任务。"}
_TASK_ID = {"type": "string", "description": "创建时返回的子任务 ID"}


TOOL_BINDINGS = {'subagent_manager': set_manager}


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        "subagent_create",
        "在 workspace 已有项目目录中将代码调查、修改、测试或评审委派给 DeepSeek Harness；后台结果会自动唤醒主 agent。",
        {"type": "object", "properties": {
            "objective": {"type": "string", "description": "任务目标和完成标准，不要求预先拆出操作步骤"},
            "cwd": {"type": "string", "description": "workspace 下已存在的项目目录，接受相对 workspace 的路径或位于其内的绝对路径"},
            "background": {"type": "string", "description": "主 agent 整理的相关技术背景，只包含完成任务所需信息"},
            "wait": _WAIT,
        }, "required": ["objective", "cwd"]}, subagent_create, is_async=True,
        requires=('subagent_manager',),
    )
    registry.register(
        "subagent_attach",
        "按提供的会话 ID 关联已有 DSH 编程会话（仅建立关联，不发消息或启动执行）。",
        {"type": "object", "properties": {"session_id": {"type": "string", "description": "指定的 DSH 会话 ID"}},
         "required": ["session_id"]}, subagent_attach, is_async=True,
        requires=('subagent_manager',),
    )
    registry.register(
        "subagent_followup",
        "在同一 DSH 会话和工作区追改，或继续已中断的 coding 任务；运行中的追问会排队。",
        {"type": "object", "properties": {"task_id": _TASK_ID, "message": {"type": "string"}, "wait": _WAIT},
         "required": ["task_id", "message"]}, subagent_followup, is_async=True,
        requires=('subagent_manager',),
    )
    registry.register(
        "subagent_view", "查看子任务状态和最近结果；不传 task_id 列出任务。仅查询，不触发执行。",
        {"type": "object", "properties": {
            "task_id": _TASK_ID,
            "include_history": {"type": "boolean", "description": "附旧版子任务保存的历史，默认 false"},
            "include_dsh_history": {"type": "boolean", "description": "查看指定任务的 DSH 近期编程对话"},
            "include_usage": {"type": "boolean", "description": "附轮次用量，默认 false"},
            "include_archived": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "description": "列表数量默认20；指定任务时最近轮次数默认1"},
        }, "required": []}, subagent_view, is_async=True,
        requires=('subagent_manager',),
    )
    registry.register(
        "subagent_cancel",
        "取消当前执行和排队追问，保留工作区与已有结果，后续可继续追问。",
        {"type": "object", "properties": {"task_id": _TASK_ID}, "required": ["task_id"]}, subagent_cancel, is_async=True,
        requires=('subagent_manager',),
    )
    registry.register(
        "subagent_archive", "归档子任务并取消未完成执行，保留记录且不再接受追问（不可逆）。",
        {"type": "object", "properties": {"task_id": _TASK_ID}, "required": ["task_id"]}, subagent_archive, is_async=True,
        requires=('subagent_manager',),
    )
