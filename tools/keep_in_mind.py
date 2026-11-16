"""
keep_in_mind 工具

让主模型在运行期间维护 master 与重要记忆（memory）。
由于 system_prompt 不立即刷新，写入的条目将在切换 session 或重启时进入系统提示词。

工具使用方式：
  section: "master" | "memory"
  action: "add" | "remove" | "list"
  entry: string （add/remove 时必填）
"""

import logging

from tools._output import compact_json

logger = logging.getLogger(__name__)

# 由 run_web.py 注入 SystemPromptBuilder 实例
_builder = None


def set_builder(builder):
    """设置 SystemPromptBuilder 实例，供 keep_in_mind 工具使用。"""
    global _builder
    _builder = builder


def keep_in_mind(args: dict) -> str:
    """
    维护 master 或 memory（重要记忆）条目。

    Args:
        args: 参数字典
            - section: "master" 或 "memory"
            - action: "add"（添加）、"remove"（删除）、"list"（列出）
            - entry: 条目内容（add/remove 时必填）

    Returns:
        操作结果字符串
    """
    if _builder is None:
        return compact_json({"error": "SystemPromptBuilder 未初始化"})

    section = args.get("section", "")
    action = args.get("action", "")
    entry = args.get("entry", "")

    if section not in ("master", "memory"):
        return compact_json({"error": f"section 必须是 master 或 memory，当前为 {section}"})

    if action not in ("add", "remove", "list"):
        return compact_json({"error": f"action 必须是 add、remove 或 list，当前为 {action}"})

    if action in ("add", "remove") and not entry:
        return compact_json({"error": "add 和 remove 操作必须提供 entry"})

    try:
        if action == "list":
            entries = _builder.list_entries(section)
            if not entries:
                return f"当前 {section} 暂无条目。"
            return "\n".join(f"- {e}" for e in entries)

        if action == "add":
            added = _builder.add_entry(section, entry)
            if not added:
                return "已存在"
            return "已添加"

        # action == "remove"
        removed = _builder.remove_entry(section, entry)
        if not removed:
            return "未找到"
        return "已删除"

    except Exception as e:
        logger.error(f"keep_in_mind 异常: {e}", exc_info=True)
        return compact_json({"error": f"操作失败: {e}"})


TOOL_BINDINGS = {'builder': set_builder}


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="keep_in_mind",
        description="维护master和memory。当你发现关于重要事实、偏好、习惯或约定时调用，将关键信息写入对应分区。",
        parameters={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["master", "memory"],
                    "description": "要维护的分区：master 记录长期事实、偏好与习惯，memory 为重要记忆。",
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "操作类型：add 添加条目、remove 删除条目、list 列出当前条目。",
                },
                "entry": {
                    "type": "string",
                    "description": "add/remove 时必填，list 时留空。",
                },
            },
            "required": ["section", "action"],
        },
        func=keep_in_mind,
        is_async=False,
        requires=('builder',),
    )
