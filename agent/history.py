"""内部消息结构与工具配对检查；只修复可明确解释的缺失，不执行工具。"""

import json
import logging

logger = logging.getLogger(__name__)


def missing_tool_result(call_id, *, status="unknown"):
    explanation = ("本轮异常终止，此工具尚未执行。" if status == "not_executed" else
                   "工具结果缺失，无法确认执行状态；操作可能已生效，请先核实，不要直接重复执行。")
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps({"error": explanation, "status": status, "source": "host"}, ensure_ascii=False)}


def validate_tool_calls(calls, used_ids=()):
    if not isinstance(calls, list):
        raise ValueError("历史结构错误：tool_calls 必须是数组")
    seen = set(used_ids)
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError("历史结构错误：工具调用必须是对象")
        call_id, function = call.get("id"), call.get("function")
        if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
            raise ValueError("历史结构错误：工具调用 ID 缺失或重复")
        if (call.get("type", "function") != "function" or not isinstance(function, dict)
                or not isinstance(function.get("name"), str) or not function["name"].strip()
                or not isinstance(function.get("arguments"), str)):
            raise ValueError("历史结构错误：工具调用名称或参数类型不合法")
        seen.add(call_id)


def _content_valid(content):
    if isinstance(content, str) or content is None:
        return
    if not isinstance(content, list):
        raise ValueError("历史结构错误：content 必须是文本或内容块数组")
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ValueError("历史结构错误：内容块必须是带 type 的对象")
        if block["type"] in ("text", "input_text", "output_text") and not isinstance(block.get("text"), str):
            raise ValueError("历史结构错误：文本内容块缺少文本")


def has_content(content):
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(block, dict) and (
            bool(str(block.get("text") or "").strip())
            if block.get("type") in ("text", "input_text", "output_text") else bool(block.get("type"))
        ) for block in content)
    return False


def is_empty_assistant(message):
    """只确认纯空占位；任何思考、签名、原生输出或未知非空字段都阻止清理。"""
    if message.get("role") != "assistant" or message.get("tool_calls"):
        return False
    content = message.get("content")
    if content is not None and content != "" and content != []:
        return False
    for key, value in message.items():
        if key in {"role", "content", "tool_calls"}:
            continue
        if value is None or (isinstance(value, (str, list, dict)) and len(value) == 0):
            continue
        return False
    return True


def prepare_history(messages):
    """保留有数据的旧消息；只删除纯空 assistant，补齐缺失结果，其他损坏明确报错。"""
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise ValueError("历史结构错误：消息历史必须是数组")
    output, pending, used = [], {}, set()
    repairs = 0

    def close_batch():
        nonlocal repairs
        for call_id in pending:
            output.append(missing_tool_result(call_id))
            repairs += 1
        pending.clear()

    for msg in messages:
        if msg is None:
            repairs += 1
            continue
        if not isinstance(msg, dict):
            raise ValueError("历史结构错误：消息必须是对象")
        role = msg.get("role")
        if role is None and msg.get("type") == "web_search_call":
            output.append(msg)
            continue
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("历史结构错误：消息角色不合法")
        content = msg.get("content")
        _content_valid(content)
        if role == "tool":
            call_id = msg.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError("历史结构错误：工具结果没有对应的待完成调用，或结果重复")
            pending.pop(call_id)
            if content is None:
                output.append(missing_tool_result(call_id))
                repairs += 1
            else:
                output.append(msg)
            continue
        calls = msg.get("tool_calls")
        if calls is None:
            calls = []
        if role != "assistant" and calls:
            raise ValueError("历史结构错误：只有 assistant 可以请求工具调用")
        if role == "assistant":
            validate_tool_calls(calls, used)
            if is_empty_assistant(msg):
                repairs += 1
                continue
        elif content is None:
            if role == "user":
                raise ValueError("历史结构错误：用户消息缺少内容")
            msg = {**msg, "content": ""}
            repairs += 1
        close_batch()
        output.append(msg)
        for call in calls:
            pending[call["id"]] = True
            used.add(call["id"])
    close_batch()
    if repairs:
        logger.warning("已修复历史中的 %s 处空消息或缺失工具结果，未执行任何工具", repairs)
    return output
