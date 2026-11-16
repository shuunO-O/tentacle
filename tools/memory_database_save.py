"""
memory_database_save 工具

主模型调用此工具将值得长期保存的内容写入记忆库。
"""

import json
import logging

from memory.extractor import MemoryExtractor
from memory.ladybug_store import LadybugStore

logger = logging.getLogger(__name__)

# 由 run_web.py 注入 SessionManager，用于获取当前 session_id
_session_manager = None


def set_session_manager(session_manager):
    """设置 SessionManager 实例"""
    global _session_manager
    _session_manager = session_manager


async def memory_database_save(args: dict) -> str:
    """
    保存记忆。

    Args:
        args: {"content": "要保存的记忆文本"}

    Returns:
        JSON 字符串：{"status": "ok", "event_id": "event_1"} 或 {"status": "error", "error": "..."}
    """
    content = args.get("content", "").strip()
    if not content:
        return json.dumps(
            {"status": "error", "error": "content 不能为空"},
            ensure_ascii=False,
        )

    if _session_manager is None:
        return json.dumps(
            {"status": "error", "error": "session_manager 未初始化"},
            ensure_ascii=False,
        )

    try:
        session_id = _session_manager.current_session.session_id
    except Exception as e:
        logger.warning(f"获取当前 session_id 失败: {e}")
        session_id = "unknown"

    try:
        extractor = MemoryExtractor()
        extraction = await extractor.extract(content)

        store = LadybugStore()
        event_id = await store.save_memory(content, session_id, extraction)
        return json.dumps({"status": "ok", "event_id": event_id}, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        logger.error(f"保存记忆失败: {e}", exc_info=True)
        return json.dumps(
            {"status": "error", "error": str(e)},
            ensure_ascii=False,
        )


TOOL_BINDINGS = {'session_manager': set_session_manager}


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="memory_database_save",
        description="将值得长期保存的对话或经历写入长期记忆库，内容应包含时间、地点、人物、经过等关键信息。",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要保存的记忆内容，建议包含时间、地点、人物、经过等关键信息。",
                }
            },
            "required": ["content"],
        },
        func=memory_database_save,
        is_async=True,
        requires=('session_manager',),
    )
