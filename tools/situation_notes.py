"""读取或整体重写近况；写入不刷新已经构建的系统提示词。"""

from agent.loop import current_tool_context

_builder = None


def set_builder(builder):
    global _builder
    _builder = builder


def situation_notes(args: dict) -> dict | str:
    if _builder is None:
        return {"error": "SystemPromptBuilder 未初始化"}

    action = args.get("action")
    if action == "read":
        return {"content": _builder.read_situation()}
    if action != "write":
        return {"error": "action 必须是 read 或 write"}
    current = current_tool_context.get()
    context = current[0] if current is not None else None
    if context is not None:
        context.situation_written = False
    content = args.get("content")
    if not isinstance(content, str):
        return {"error": "write 必须提供完整的 content 字符串；空字符串表示清空近况"}
    _builder.write_situation(content)
    if context is not None:
        context.situation_written = True
    return "已保存"


TOOL_BINDINGS = {"builder": set_builder}


def register_tools(registry):
    registry.register(
        name="situation_notes",
        description=(
            "读取或整体重写近况笔记，记录延续当前交流的背景（进行中的事、近期关注、暂时约定与待定想法，与七天摘要互补）。"
            "更新前先 read，整理后用 write 提交完整新版（可合并、改写或删去过时内容）。"
            "未再提及不代表已结束，旧状态也不等同当前确定事实，需保留时间线索与不确定性；仅作可修订背景，下次构建系统提示词时加载。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "write"]},
                "content": {
                    "type": "string",
                    "description": "write 时必填的完整新版近况；空字符串表示清空。read 时无需提供。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        func=situation_notes,
        requires=("builder",),
    )
