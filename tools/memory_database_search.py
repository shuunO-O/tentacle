"""
memory_database_search 工具

主模型调用此工具从长期记忆中检索相关事件和实体。
"""

import json
import logging
import math

from memory.ladybug_store import DEFAULT_EVENT_LIMIT, MAX_EVENT_LIMIT, LadybugStore, search_date_bounds
from memory.evidence_service import evidence_service
from agent.loop import current_tool_context

logger = logging.getLogger(__name__)

_VALID_TARGET_LABELS = {"Event", "Person", "Concept"}


def _metadata(node: dict) -> str:
    parts = [str(node["id"])]
    for key, prefix in (("name", "名称:"), ("timestamp", ""), ("type", ""), ("score", "score:")):
        if node.get(key) is not None:
            parts.append(prefix + str(node[key]))
    return " | ".join(parts)


def _format_search_data(data: dict) -> str:
    """按排名展示完整匹配正文；实体的关联事件仅展示有上限的摘要。"""
    primary = data["primary_match"]
    nodes = ([primary["node"]] if primary else []) + data["secondary_matches"]
    lines = [f"匹配:{len(nodes)}"]
    for index, node in enumerate(nodes, 1):
        lines.extend(["", f"[{index}]" + _metadata(node)])
        for key, prefix in (("summary", "摘要:"), ("content", "正文:")):
            if node.get(key) is not None:
                lines.append(prefix + node[key])
        context = primary["context"] if index == 1 else node.get("context", {})
        for key, title in (("persons", "人物"), ("locations", "地点"),
                           ("concepts", "概念"), ("attitudes", "态度")):
            items = []
            for item in context.get(key, []):
                relation = item["relation"]
                if "sentiment" in item:
                    relation += ",sentiment:" + str(item["sentiment"])
                items.append(f"{item['name']}({relation})")
            if items:
                lines.append(title + ":" + ";".join(items))
        events = context.get("events", [])
        if events:
            suffix = "(还有更多)" if context.get("events_has_more") else ""
            lines.append(f"关联事件:{len(events)}{suffix}")
            for event in events:
                relations = []
                for item in event["relations"]:
                    text = item["relation"]
                    if item.get("properties"):
                        text += json.dumps(item["properties"], ensure_ascii=False, separators=(",", ":"))
                    relations.append(text)
                lines.append("  " + _metadata(event) + " | " + ";".join(relations))
                if event.get("summary") is not None:
                    lines.append("  摘要:" + event["summary"])
    return "\n".join(lines)


async def memory_database_search(args: dict) -> str:
    """
    检索长期记忆。

    Args:
        args: {
            "operation": "search" | "read" | "expand",
            "query_text": "...",
            "target_label": "Event" | "Person" | "Concept",
            "entity_filter": "...",
            "time_hint": "YYYY-MM-DD",
            "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD",
            "event_limit": 3,
            "event_id": "read使用的事件ID", "start": 0, "limit": 1600,
            "top_k": 4, "min_score": 0.5,
            "cursor": "expand使用的游标", "concept_id": "游标提供的概念ID",
            "reset_seen": false
        }

    Returns:
        成功返回以“匹配:N”开头的紧凑文本；失败返回 status=error JSON，不能视为无匹配。
    """
    try:
        def text_arg(key):
            value = args.get(key)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是字符串")
            if len(value) > (4000 if key == "query_text" else 500):
                raise ValueError(f"{key} 过长")
            return value.strip() or None

        operation = args.get("operation", "search")
        if not isinstance(operation, str) or operation not in {"search", "read", "expand"}:
            raise ValueError("operation 必须是 search、read 或 expand")
        reset = args.get("reset_seen", False)
        if type(reset) is not bool or ("reset_seen" in args and operation != "search"):
            raise ValueError("reset_seen 仅用于 search，必须为布尔值")

        def integer(key, default, maximum):
            value = args.get(key, default)
            if type(value) is not int or not (0 if key == "start" else 1) <= value <= maximum:
                raise ValueError(f"{key} 超出整数范围")
            return value

        cursor, event_id, concept_id = text_arg("cursor"), text_arg("event_id"), text_arg("concept_id")
        if operation != "search":
            if any(key in args for key in ("entity_filter", "time_hint", "date_from", "date_to", "event_limit", "session_id")):
                raise ValueError("追查沿用原查询条件，不能指定新的搜索过滤")
            if args.get("target_label", "Event") != "Event":
                raise ValueError("read/expand 仅适用于 Event")
            if operation == "read":
                if not event_id or cursor or concept_id:
                    raise ValueError("read 需要 event_id，不接受 cursor/concept_id")
                semantic = text_arg("query_text")
                if semantic and "start" in args:
                    raise ValueError("语义read不接受start；可用limit限制字符数")
                if not semantic and any(key in args for key in ("top_k", "min_score")):
                    raise ValueError("top_k/min_score仅用于带query_text的语义read")
                start, limit = integer("start", 0, 2**53 - 1), integer("limit", 10000 if semantic else 1600, 10000)
            else:
                if not cursor or not concept_id or event_id or any(key in args for key in ("start", "query_text", "top_k", "min_score")):
                    raise ValueError("expand 需要 cursor/concept_id，不接受 event_id/start")
                start, limit = 0, integer("limit", 3, 8)
        else:
            if cursor or event_id or concept_id or any(key in args for key in ("start", "limit", "top_k", "min_score")):
                raise ValueError("续查参数需使用 operation=read 或 expand")
            start, limit = 0, 3
        query_text, entity_filter = text_arg("query_text"), text_arg("entity_filter")
        session_id = text_arg("session_id")
        top_k = integer("top_k", 4, 4)
        min_score = args.get("min_score")
        if min_score is not None and (type(min_score) not in (int, float) or not math.isfinite(min_score) or not -1 <= min_score <= 1):
            raise ValueError("min_score必须为-1..1的有限数值")
        time_hint = text_arg("time_hint")
        date_from, date_to = text_arg("date_from"), text_arg("date_to")
        target_label = args.get("target_label", "Event")
        event_limit = args.get("event_limit", DEFAULT_EVENT_LIMIT)
        if not isinstance(target_label, str) or target_label not in _VALID_TARGET_LABELS:
            raise ValueError("target_label 必须是 Event、Person 或 Concept")
        if type(event_limit) is not int or not 1 <= event_limit <= MAX_EVENT_LIMIT:
            raise ValueError(f"event_limit 必须是 1..{MAX_EVENT_LIMIT} 的整数")
        search_date_bounds(time_hint, date_from, date_to)
        if session_id and target_label != "Event":
            raise ValueError("session_id 仅用于 Event search")
        if reset and target_label != "Event":
            raise ValueError("reset_seen 仅用于 Event 搜索")
        if operation == "search" and not any((query_text, entity_filter, time_hint, date_from, date_to, session_id)):
            raise ValueError("query_text、entity_filter、time_hint、date_from、date_to、session_id 至少提供一个")
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)

    try:
        store = LadybugStore()
        if target_label == "Event":
            current = current_tool_context.get()
            owner = current[0].session_id if current else None
            start_time, end_time = search_date_bounds(time_hint, date_from, date_to)
            return await evidence_service(store).call(
                operation=operation, owner=owner, query=query_text,
                filters={"entity_filter": entity_filter, "start_time": start_time, "end_time": end_time, "session_id": session_id},
                reset=reset, event_id=event_id, cursor=cursor, concept_id=concept_id, start=start, limit=limit,
                top_k=top_k, min_score=min_score,
            )
        data = await store.search(
            query_text, target_label, entity_filter, time_hint,
            date_from=date_from, date_to=date_to, event_limit=event_limit,
        )
        return _format_search_data(data)
    except Exception as e:
        logger.error(f"检索记忆失败: {e}", exc_info=True)
        return json.dumps(
            {
                "status": "error",
                "error": f"检索记忆失败：{e}。本次检索未完成，不能据此判断没有相关记忆。",
            },
            ensure_ascii=False,
        )


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="memory_database_search",
        description=(
            "从长期记忆库检索 Event、Person 或 Concept。"
            "search（默认）：Event 按摘要/原文双路召回，返回详细结果与候选索引（含 create_time、raw_session，可用 session_id 限定来源）；"
            "同会话 15 分钟内默认排除已交付事件，新问题可传 reset_seen=true。Person/Concept 附最近关联事件摘要。"
            "read：按 event_id（已交付事件支持数字简写）读原文核实摘要；不传 query_text 按 start/limit 分页，传 query_text 在事件内语义定位（top_k 1..4，不传 start）。"
            "expand：沿 cursor 给出的 concept_id 追查关联事件（复用原条件，每链最多 20 条）。"
            "日期按本地日历过滤事件创建时间（含首尾）；共享概念不证明因果或顺序；score 非置信度且无阈值；空结果可能因去重排除，错误 JSON 不代表无匹配。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["search", "read", "expand"], "default": "search"},
                "event_id": {"type": "string", "description": "read需要的事件ID。"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 4,
                          "description": "仅语义read：最多选择的原文块数，默认4。"},
                "min_score": {"type": "number", "minimum": -1, "maximum": 1,
                              "description": "仅语义read：可选相关性阈值，不是事实置信度。"},
                "cursor": {"type": "string", "description": "expand需要的当前会话追查游标。"},
                "concept_id": {"type": "string", "description": "expand仅接受当前cursor提供的概念入口ID。"},
                "start": {"type": "integer", "minimum": 0, "description": "read的Unicode码点偏移，默认0。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000,
                          "description": "分页read默认1600字符，语义read默认10000，总字符上限10000；expand默认3，接受1..8，受配置和追查剩余额度限制。"},
                "reset_seen": {"type": "boolean", "description": "仅Event search：独立新问题重置本会话去重记录及旧游标。"},
                "query_text": {
                    "type": "string",
                    "description": "用于语义检索的自然语言查询词，如'上次去海边玩'、'我和李四的聚餐'。",
                },
                "target_label": {
                    "type": "string",
                    "enum": ["Event", "Person", "Concept"],
                    "default": "Event",
                    "description": "期望检索的核心节点类型。",
                },
                "session_id": {
                    "type": "string",
                    "maxLength": 500,
                    "description": "仅Event search：精确匹配来源raw_session；可单独使用或与其他条件组合，expand自动继承。不传则跨来源会话检索；不改变宿主会话的去重归属。",
                },
                "entity_filter": {
                    "type": "string",
                    "description": "精确名称或别名。Event 按关联实体过滤，Person/Concept 按自身名称或别名过滤。",
                },
                "time_hint": {
                    "type": "string",
                    "description": "单日 YYYY-MM-DD，不能与 date_from/date_to 同用。",
                },
                "date_from": {
                    "type": "string",
                    "description": "起始日期 YYYY-MM-DD，含当天；可单独提供。",
                },
                "date_to": {
                    "type": "string",
                    "description": "结束日期 YYYY-MM-DD，含当天；可单独提供。",
                },
                "event_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_EVENT_LIMIT,
                    "default": DEFAULT_EVENT_LIMIT,
                    "description": "每个 Person/Concept 返回的最近关联事件摘要数；按事件去重，超出时标记还有更多。",
                },
            },
            "required": [],
        },
        func=memory_database_search,
        is_async=True,
    )
