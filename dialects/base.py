"""
方言适配层公共基础

内部规范格式（Canonical Format）：
- 请求侧：Chat Completions 风格 full_messages（dict 列表）+ OpenAI Function 工具定义
- 响应侧：ChatCompletionMessage + 归一化 usage（SimpleNamespace）

DialectAdapter 子类负责「内部规范格式 <-> 各家 API 方言」的双向转译：
- call() 转译请求并调用 client，解析响应（流式/非流式在子类内部分流）
- 流式增量事件（THINKING_DELTA / TEXT_DELTA / WEB_SEARCH 状态）由子类经 emit 派发
- 非流式 THINKING/TEXT/WEB_SEARCH、USAGE、FINAL_ANSWER 等事件由 AgentLoop 主干派发

边界：方言层不执行工具、不累计 usage、不维护会话状态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from agent.protocol import Context, ModelConfig

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessage

logger = logging.getLogger(__name__)


@dataclass
class CanonicalFunction:
    """内部规范格式的工具调用函数定义（兼容 OpenAI Function.model_dump）。"""

    name: str
    arguments: str

    def model_dump(self) -> dict:
        return {"arguments": self.arguments, "name": self.name}


@dataclass
class CanonicalToolCall:
    """内部规范格式的工具调用条目（兼容 ChatCompletionMessageToolCall.model_dump）。"""

    id: str
    function: CanonicalFunction
    type: str = "function"

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "function": self.function.model_dump(),
            "type": self.type,
        }


@dataclass
class CanonicalMessage:
    """轻量内部助手消息容器，避免非 OpenAI 方言为构建消息而加载完整 openai SDK。"""

    role: str = "assistant"
    content: str | None = None
    tool_calls: list[CanonicalToolCall] | None = None

    def model_dump(self) -> dict:
        return {
            "content": self.content,
            "refusal": None,
            "role": self.role,
            "annotations": None,
            "audio": None,
            "function_call": None,
            "tool_calls": (
                [tc.model_dump() for tc in self.tool_calls]
                if self.tool_calls is not None
                else None
            ),
        }


@dataclass
class NormalizedUsage:
    """
    归一化 usage（各方言负责把自家 schema 转成此形态）

    Attributes:
        prompt_tokens: 输入 token（含缓存命中部分）
        completion_tokens: 输出 token
        total_tokens: 总 token（缺失时由输入+输出推算）
        cached_tokens: 缓存命中读（DeepSeek prompt_cache_hit_tokens /
               OpenAI prompt_tokens_details.cached_tokens）
        cache_write_tokens: 缓存写入（Anthropic cache_creation_input_tokens，
               DeepSeek/OpenAI 恒 0，为多模型预留）
        reasoning_tokens: 思考 token（OpenAI completion_tokens_details.reasoning_tokens，
               DeepSeek 计入 completion 无单独字段则恒 0）
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass
class AdapterResult:
    """
    方言调用结果（AgentLoop 主干据此继续工具循环与事件派发）

    Attributes:
        message: 内部规范格式的助手消息
        usage: NormalizedUsage（各方言归一化后的用量）；无 usage 时为 None
        web_search_calls: 服务端联网搜索 items（plain dict，需原样追加进 history
               供多轮回传）；chat 方言恒为空列表
        response_model: 服务端返回的模型标识；未提供时为空，不使用请求配置补填
    """
    message: CanonicalMessage | ChatCompletionMessage
    usage: NormalizedUsage | None
    web_search_calls: list = field(default_factory=list)
    response_model: str = ""  # Server-reported model; never inferred from request config.


class DialectAdapter:
    """
    API 方言适配器基类

    无状态：client 由 AgentLoop 持有并按调用传入，
    同一方言实例可被多个模型角色复用。
    """

    name: str = ""

    async def call(
        self,
        client,
        cfg: ModelConfig,
        full_messages: list[dict],
        tools,
        context: Context,
        emit: Callable,
    ) -> AdapterResult:
        """
        执行一次模型调用（流式/非流式由子类依据 cfg.stream 内部分流）

        Args:
            client: AgentLoop 持有的 AsyncOpenAI 实例
            cfg: 模型配置（含 provider quirks 依据）
            full_messages: 内部规范格式消息列表
            tools: OpenAI Function 定义列表（可为 None/空）
            context: 请求级上下文
            emit: 事件回调（async，接收 Event 对象）

        Returns:
            AdapterResult
        """
        raise NotImplementedError

    # ==================== 内部规范格式构建（各方言共用） ====================

    def _build_message(
        self,
        content: str,
        reasoning_content: str,
        tool_calls_chunks: dict[int, dict],
    ) -> CanonicalMessage:
        """
        根据收集的内容构建完整的 message 对象

        Args:
            content: 完整的文本内容
            reasoning_content: 完整的思考内容
            tool_calls_chunks: 收集的工具调用片段

        Returns:
            CanonicalMessage 对象
        """
        tool_calls = None
        if tool_calls_chunks:
            tool_calls = []
            for idx in sorted(tool_calls_chunks.keys()):
                chunk = tool_calls_chunks[idx]
                tool_calls.append(CanonicalToolCall(
                    id=chunk["id"],
                    type="function",
                    function=CanonicalFunction(
                        name=chunk["function"]["name"],
                        arguments=chunk["function"]["arguments"],
                    ),
                ))

        message = CanonicalMessage(
            role="assistant",
            content=content or None,
            tool_calls=tool_calls,
        )

        # 动态挂载思考内容字段，保持与非流式 ChatCompletionMessage 一致的属性约定
        if reasoning_content:
            object.__setattr__(message, "reasoning_content", reasoning_content)

        return message


# ==================== 模块级公共 helper ====================


def message_to_dict(message: CanonicalMessage | ChatCompletionMessage) -> dict:
    """
    将 ChatCompletionMessage 转为 dict（追加进 full_messages）。

    注意：ChatCompletionMessage 的 model_dump() 只序列化声明字段，
    动态挂上的 reasoning_content 会被丢弃，需要手动补回。
    """
    d = message.model_dump()
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning is not None:
        d["reasoning_content"] = reasoning
    steps = getattr(message, "gemini_steps", None)
    if steps is not None:
        d["gemini_steps"] = steps
    content = getattr(message, "gemini_content", None)
    if content is not None:
        d["gemini_content"] = content
    interaction = getattr(message, "gemini_interaction", None)
    if interaction is not None:
        d["gemini_interaction"] = interaction
    output = getattr(message, 'responses_output', None)
    if output is not None:
        d['responses_output'] = output
    return d
