"""
方言适配层（多模型转译）

AgentLoop 主干只讲内部规范格式（Chat Completions 风格 messages +
ChatCompletionMessage + 归一化 usage），各家 API 的请求/响应转译
下沉到方言适配器，见 base.py 模块 docstring。

按 api_style 分发方言实例：
- chat: OpenAI Chat Completions 兼容方言（DeepSeek / 官方 / 各兼容网关）
- responses: Responses API 方言（DeepSeek v4 / OpenAI）
- interactions: Gemini Interactions API（保留）
- gemini_generate: Cloud Gemini 原生 generateContent

provider 字段（ModelConfig）承载同一方言内的供应商怪癖（quirks），
见各方言类 docstring。
"""

from dialects.base import (
    AdapterResult,
    DialectAdapter,
    NormalizedUsage,
    message_to_dict,
)
from dialects.gemini import GeminiInteractionsDialect
from dialects.gemini_generate import GeminiGenerateContentDialect
from dialects.openai_chat import OpenAIChatDialect
from dialects.responses import ResponsesDialect

__all__ = [
    "AdapterResult",
    "DialectAdapter",
    "NormalizedUsage",
    "OpenAIChatDialect",
    "ResponsesDialect",
    "GeminiInteractionsDialect",
    "GeminiGenerateContentDialect",
    "message_to_dict",
    "get_dialect",
]

_DIALECTS = {
    "interactions": GeminiInteractionsDialect,
    "gemini_generate": GeminiGenerateContentDialect,
    "chat": OpenAIChatDialect,
    "responses": ResponsesDialect,
}


def get_dialect(api_style: str) -> DialectAdapter:
    """按 api_style 返回方言适配器实例（方言无状态，可复用）"""
    cls = _DIALECTS.get(api_style)
    if cls is None:
        raise ValueError(f"未知的 api_style: {api_style}（可选: {', '.join(_DIALECTS)}）")
    return cls()
