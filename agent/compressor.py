"""
压缩模块

根据传入的 session_id 和消息 id 范围，从 chronicle.db 读取原始 user 消息，
调用压缩模型生成事件摘要，并存入 summaries 表。
"""

import json
import logging
from datetime import date
from runtime.paths import prompt_template

from agent.loop import AgentLoop
from runtime.timezone import day_boundaries, local_datetime
from agent.chronicle import ChronicleManager
from agent.protocol import Context
from config.manager import ConfigManager
from agent.prompt import render_names

logger = logging.getLogger(__name__)


def _extract_json(text: str):
    """
    从模型输出中稳健提取 JSON。

    压缩模型有时会把合法 JSON 包在 Markdown 代码围栏（```json ... ```）里，
    或夹带首尾空白/解释性文字。直接 json.loads 会因此失败（
    "Expecting value: line 1 column 1"）。此函数依次尝试：
    1. 原样解析
    2. 去掉 ``` 代码围栏后解析
    3. 截取第一层 [ ] 或 { } 之间的内容解析
    """
    if not text:
        raise json.JSONDecodeError("模型输出为空", text or "", 0)

    stripped = text.strip()

    # 1. 直接解析
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. 去掉 Markdown 代码围栏
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            stripped = candidate

    # 3. 兜底：截取第一层 [ ] 或 { } 之间的内容
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = stripped.find(open_ch)
        end = stripped.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise json.JSONDecodeError("未能从模型输出中提取有效 JSON", text, 0)


class Compressor:
    """
    对话压缩器

    负责将某段对话历史压缩成按事件组织的摘要。
    """

    def __init__(self, manager: ConfigManager, chronicle: ChronicleManager):
        """
        初始化压缩器

        Args:
            manager: 配置管理器
            chronicle: 归档管理器
        """
        self.manager = manager
        self.chronicle = chronicle
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """加载压缩模型系统提示词"""
        prompt_path = prompt_template("system_prompt_compressor.md")
        if prompt_path.exists():
            return render_names(prompt_path.read_text(encoding="utf-8"), self.manager.snapshot())
        logger.warning(f"压缩提示词文件不存在: {prompt_path}")
        return "你负责提取对话中的关键事件，输出 JSON 格式摘要。"

    def _build_user_content(
        self, messages: list[dict], header: str | None = None
    ) -> str:
        """将 user 和 assistant 消息列表组装成压缩模型的 user content"""
        lines = [header or "请对以下对话历史进行摘要："]
        for msg in messages:
            created = msg.get("created_time", 0)
            platform = msg.get("platform", "unknown")
            role = msg.get("role", "user")
            content = self._content_to_text(msg.get("content", ""))
            time_str = local_datetime(created).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{time_str}][{platform}] {role}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _content_to_text(content) -> str:
        """
        消息 content -> 纯文本（压缩模型只吃文本）

        content 为数组（带图片的多模态消息）时提取 text 块，
        图片块以 [图片] 占位，避免 dict repr 污染压缩提示词。
        """
        if not isinstance(content, list):
            return content
        texts: list[str] = []
        img_count = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("text", "input_text"):
                text = block.get("text", "")
                if text:
                    texts.append(text)
            elif btype in ("image", "input_image", "image_url"):
                img_count += 1
        if img_count:
            texts.append(f"[图片×{img_count}]")
        return "\n".join(texts)

    async def _noop_callback(self, event):
        """压缩模型回调，不向外输出"""
        pass

    async def _compress_messages(
        self,
        messages: list[dict],
        default_start: float,
        default_end: float,
        header: str | None = None,
        session_id: str = "compressor",
    ) -> bool:
        """
        压缩给定消息列表的通用实现

        Args:
            messages: 待压缩的 user/assistant 消息列表
            default_start: 模型未返回 start_time 时使用的默认值
            default_end: 模型未返回 end_time 时使用的默认值
            header: 压缩模型 user content 的首行提示
            session_id: 用于兼容 AgentLoop 的会话标识

        Returns:
            压缩成功返回 True，失败返回 False
        """
        if not messages:
            logger.info("[压缩跳过] 没有需要压缩的 user 消息")
            return True

        user_content = self._build_user_content(messages, header=header)

        try:
            model_config = self.manager.get_model("compress")
        except ValueError as e:
            logger.error(f"压缩模型配置错误: {e}")
            return False

        loop = AgentLoop(model_config=model_config, callback=self._noop_callback)

        context = Context(
            current_message=user_content,
            history_messages=[],
            system_prompt=self._system_prompt,
            platform="compressor",
            session_id=session_id,
        )

        full_messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            result = await loop.run(full_messages, context)
            if result.stop_reason != "complete" or result.error:
                logger.warning("压缩模型未正常完成，本日摘要保留为待处理")
                return False
            final_answer = result.final_answer or ""
            parsed = _extract_json(final_answer)
            if isinstance(parsed, list):
                # 提示词要求输出数组格式
                summaries = parsed
            elif isinstance(parsed, dict) and "summaries" in parsed:
                summaries = parsed["summaries"]
            else:
                raise ValueError("摘要输出必须是数组或包含 summaries 数组的对象")
            if not isinstance(summaries, list):
                raise ValueError("summaries 必须是数组")

            for s in summaries:
                if not isinstance(s, dict) or not isinstance(s.get("summary"), str) or not s["summary"].strip():
                    raise ValueError("摘要条目必须包含非空 summary 文本")
                if not s.get("start_time"):
                    s["start_time"] = default_start
                if not s.get("end_time"):
                    s["end_time"] = default_end

            if summaries:
                self.chronicle.append_summaries(summaries)
                logger.info(f"[压缩完成] 生成 {len(summaries)} 条摘要")
            else:
                logger.info("[压缩完成] 没有生成摘要")

            return True
        except json.JSONDecodeError as e:
            logger.error(f"压缩模型输出解析失败: {e}")
            return False
        except Exception as e:
            logger.error(f"压缩过程异常: {e}", exc_info=True)
            return False

    async def compress(self, session_id: str, start_id: int, end_id: int) -> bool:
        """
        压缩指定 id 范围内的对话

        Args:
            session_id: 会话标识（用于日志和范围校验）
            start_id: 起始消息 id（包含）
            end_id: 结束消息 id（包含）

        Returns:
            压缩成功返回 True，失败返回 False
        """
        logger.info(f"[压缩开始] session={session_id}, id_range=[{start_id}, {end_id}]")

        messages = self.chronicle.get_messages_by_id_range(start_id, end_id)
        if messages:
            default_start = messages[0].get("created_time", 0)
            default_end = messages[-1].get("created_time", 0)
        else:
            default_start = default_end = 0

        header = f"请对 session {session_id} 的对话历史进行摘要："
        return await self._compress_messages(
            messages,
            default_start=default_start,
            default_end=default_end,
            header=header,
            session_id=session_id,
        )

    async def compress_date(self, day: date, messages: list[dict]) -> bool:
        """
        压缩某一天的对话

        Args:
            day: 日期
            messages: 当天 user/assistant 消息列表

        Returns:
            压缩成功返回 True，失败返回 False
        """
        logger.info(f"[压缩开始] date={day.isoformat()}")

        start_ts, end_ts = day_boundaries(day)

        header = f"请对 {day.isoformat()} 的对话历史进行摘要："
        return await self._compress_messages(
            messages,
            default_start=start_ts,
            default_end=end_ts,
            header=header,
            session_id=f"compress_{day.isoformat()}",
        )
