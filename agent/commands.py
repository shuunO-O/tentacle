"""
斜杠命令模块

AgentSession.run_round 在把输入交给模型前统一识别、执行命令。
命令不会进入模型，也不归档到对话历史。

现有命令：
- /session  手动结束当前 session 并开启新 session（新 session 自带最近摘要）
- /compress 立即整理昨天及之前未压缩的历史记忆（调用 compress 模型）
- /rollback 回退上一轮用户对话（不撤销工具副作用与 Token 用量）
- /model    查看或切换当前主对话模型
- /cache    创建/重建当前 Gemini 显式缓存（status 查看，clear 删除）
- /help     列出可用命令
"""

import inspect
import logging
import re

logger = logging.getLogger(__name__)

COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

COMMAND_HELP = {
    "session": "手动切换 session：结束当前会话并开启新会话（新会话自带最近摘要）",
    "compress": "立即整理昨天及之前的历史记忆（调用压缩模型，耗时较长）",
    "rollback": "回退上一轮对话（工具操作、已发送消息和 Token 用量不会撤销）",
    "model": "查看/切换模型：/model list、/model current、/model use <model_id>",
    "cache": "创建/重建 Gemini 显式缓存：/cache；查看 /cache status；删除 /cache clear",
    "help": "显示可用命令",
}


class CommandRegistry:
    """扩展斜杠命令注册表，禁止覆盖内置命令。"""

    def __init__(self):
        self._commands: dict[str, dict] = {}

    def register(self, name: str, description: str, handler, *, source: str = "plugin") -> str:
        cleaned = str(name or "").strip().lstrip("/").lower()
        if not COMMAND_NAME_RE.fullmatch(cleaned):
            raise ValueError(f"无效命令名: {name!r}")
        if cleaned in COMMAND_HELP:
            raise ValueError(f"禁止覆盖内置命令: /{cleaned}")
        if cleaned in self._commands:
            raise ValueError(f"命令重复注册: /{cleaned}")
        if not callable(handler):
            raise ValueError(f"命令 /{cleaned} 的处理函数不可调用")
        self._commands[cleaned] = {
            "name": cleaned,
            "description": str(description or "").strip(),
            "handler": handler,
            "source": source,
        }
        return cleaned

    def unregister(self, name: str) -> bool:
        return self._commands.pop(str(name).strip().lstrip("/").lower(), None) is not None

    def unregister_by_source(self, source: str) -> list[str]:
        targets = [k for k, v in self._commands.items() if v.get("source") == source]
        for k in targets:
            self._commands.pop(k, None)
        return targets

    def get(self, name: str) -> dict | None:
        return self._commands.get(str(name).strip().lstrip("/").lower())

    def catalog(self) -> list[dict]:
        return [{"value": f"/{name}", "description": entry["description"]}
                for name, entry in self._commands.items()]


command_registry = CommandRegistry()


def command_catalog() -> list[dict]:
    """客户端提示来自后端；新增命令不需要维护平台自己的命令清单。"""
    items = [{"value": f"/{name}", "description": description} for name, description in COMMAND_HELP.items()]
    seen = {item["value"] for item in items}
    for entry in command_registry.catalog():
        if entry["value"] not in seen:
            items.append(entry)
            seen.add(entry["value"])
    return items


def parse_command(text: str) -> str | None:
    """
    识别斜杠命令

    Args:
        text: 用户输入文本

    Returns:
        命令名（小写），非命令输入返回 None
    """
    text = text.strip()
    if not text.startswith("/") or len(text) <= 1:
        return None
    parts = text[1:].strip().split(maxsplit=1)
    return parts[0].lower() + (" " + parts[1] if len(parts) > 1 else "") if parts else None


async def run_command(name: str, agent_session) -> str:
    """
    执行命令，返回给用户看的反馈文本

    Args:
        name: 命令名（parse_command 的结果）
        agent_session: AgentSession 实例

    Returns:
        反馈文本，由宿主端打印 / 广播展示
    """
    parts = name.split()
    if getattr(agent_session, "_restarting", False):
        return "[命令] 应用正在准备重启，请恢复后再试"
    command = parts[0].lower()
    if command == "session":
        new_session_id = await agent_session.switch_session()
        logger.info(f"[命令] 手动切换 session -> {new_session_id}")
        return f"[命令] 已切换 session，新 session: {new_session_id}"
    if command == "compress":
        result_text = await agent_session.compress_history()
        logger.info(f"[命令] 手动压缩 -> {result_text}")
        return f"[命令] {result_text}"
    if command == "rollback":
        result_text = await agent_session.rollback_last_round()
        logger.info(f"[命令] 回退上一轮 -> {result_text}")
        return f"[命令] {result_text}"
    if command == "model":
        action = parts[1].lower() if len(parts) > 1 else "current"
        if (action == "current" and len(parts) == 2) or len(parts) == 1:
            config = agent_session.manager.get_model(agent_session.role)
            return f"[命令] 当前模型: {agent_session.model_id} ({config.name})，角色: chat"
        if action == "list" and len(parts) == 2:
            models = agent_session.list_models()
            return "[命令] 可切换模型: " + ", ".join(
                f"{model}{' *' if model == agent_session.model_id else ''}" for model in models
            )
        if action == "use" and len(parts) == 3:
            model_id = parts[2]
            try:
                model_name = await agent_session.switch_model(model_id)
            except ValueError as exc:
                return f"[命令] 切换失败：{exc}"
            return f"[命令] 已切换模型: {model_id} ({model_name})，角色: chat"
        return "[命令] 用法: /model list | /model current | /model use <model_id>"
    if command == "cache":
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in ('status', 'clear')):
            return "[命令] 用法: /cache | /cache status | /cache clear"
        result = await agent_session.manage_gemini_cache(parts[1] if len(parts) == 2 else 'create')
        return f"[命令] {result}"
    if command == "help":
        lines = ["可用命令："]
        for cmd, desc in COMMAND_HELP.items():
            lines.append(f"  /{cmd}  {desc}")
        for entry in command_registry.catalog():
            lines.append(f"  {entry['value']}  {entry['description']}")
        return "\n".join(lines)
    custom = command_registry.get(command)
    if custom is not None:
        args_text = name[len(parts[0]):].strip()
        try:
            res = custom["handler"](args_text, agent_session)
            if inspect.isawaitable(res):
                res = await res
            return str(res) if res is not None else f"[命令] /{command} 已执行"
        except Exception as exc:
            logger.error(f"插件命令执行异常 [/{command}]: {exc}", exc_info=True)
            return f"[命令] /{command} 执行失败：{exc}"
    return f"未知命令: /{name}（输入 /help 查看可用命令）"
