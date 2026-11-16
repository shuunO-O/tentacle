"""
记忆提取器

读取 prompt/system_prompt_memory_extractor.md，调用 deepseek（memory_extractor 角色）
把主模型传入的内容解析为结构化 JSON。
"""

import json
import logging
from runtime.paths import prompt_template

from agent.loop import AgentLoop
from agent.protocol import Context
from config.manager import ConfigManager
from agent.prompt import render_names

logger = logging.getLogger(__name__)

class MemoryExtractor:
    """
    结构化记忆提取器
    """

    def __init__(self):
        self.manager = ConfigManager()
        self.model_config = self.manager.get_model("memory_extractor")
        self.system_prompt = self._load_prompt()
        self.loop = AgentLoop(model_config=self.model_config, callback=self._noop_callback)

    def _load_prompt(self) -> str:
        """加载提取器系统提示词"""
        path = prompt_template("system_prompt_memory_extractor.md")
        if path.exists():
            return render_names(path.read_text(encoding="utf-8"), self.manager.snapshot())
        logger.warning(f"提取器提示词不存在: {path}")
        return "你负责从给定文本中提取事件、实体和关系，输出 JSON。"

    @staticmethod
    async def _noop_callback(event):
        """压缩/提取模型不需要向外派发事件"""
        pass

    async def extract(self, content: str) -> dict:
        """
        提取结构化记忆。

        Args:
            content: 主模型传入的记忆文本

        Returns:
            包含 event/entities/relationships 的字典

        Raises:
            ValueError: 两次解析均失败
        """
        user_content = f"<content>\n{content}\n</content>"
        context = Context(
            current_message=user_content,
            history_messages=[],
            system_prompt=self.system_prompt,
            platform="memory_extractor",
            session_id="memory_extractor",
        )

        for attempt in range(2):
            full_messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_content},
            ]
            try:
                result = await self.loop.run(full_messages, context)
                raw = (result.final_answer or "").strip()
                # 去除可能的 markdown 代码块围栏
                if raw.startswith("```"):
                    raw = raw.strip("`")
                    if raw.lower().startswith("json"):
                        raw = raw[4:].strip()
                data = json.loads(raw)

                # 兼容提示词里 array/object 两种写法，统一返回单个对象
                if isinstance(data, list):
                    if not data:
                        raise ValueError("提取结果为空数组")
                    data = data[0]
                if not isinstance(data, dict):
                    raise ValueError("提取结果不是 JSON 对象")

                return data
            except Exception as e:
                logger.warning(f"记忆提取解析失败（第 {attempt + 1} 次）: {e}")

        raise ValueError("记忆提取失败：无法解析为有效 JSON")
