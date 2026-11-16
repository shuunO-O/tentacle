"""
方言单元测试

覆盖：
- NormalizedUsage 各来源形态：chat 方言（DeepSeek prompt_cache_hit_tokens）/
  OpenAI（details 子对象）/ 缺 total / None；responses 方言：input/output + details / None
- chat 方言图片块转换：file_id 直引通道 / base64 兜底 / 多模态消息不被拍平
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from dialects.base import NormalizedUsage
from dialects.openai_chat import OpenAIChatDialect
from dialects.responses import ResponsesDialect
from agent.protocol import ModelConfig


# ==================== chat 方言 ====================


def test_chat_convert_deepseek_form():
    usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
        prompt_cache_hit_tokens=30,
    )
    n = OpenAIChatDialect._convert_usage(usage)
    assert n == NormalizedUsage(
        prompt_tokens=100, completion_tokens=50, total_tokens=150, cached_tokens=30,
    )


def test_chat_convert_openai_form():
    usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
        prompt_tokens_details=SimpleNamespace(cached_tokens=25),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=17),
    )
    n = OpenAIChatDialect._convert_usage(usage)
    assert n == NormalizedUsage(
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
        cached_tokens=25, reasoning_tokens=17,
    )


def test_chat_convert_total_missing_falls_back_to_sum():
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    n = OpenAIChatDialect._convert_usage(usage)
    assert n.total_tokens == 15


def test_chat_convert_anthropic_cache_write_reserved():
    """cache_creation_input_tokens -> cache_write_tokens（为 Anthropic 方言预留）"""
    usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
        cache_creation_input_tokens=40,
    )
    n = OpenAIChatDialect._convert_usage(usage)
    assert n.cache_write_tokens == 40


def test_chat_convert_none():
    assert OpenAIChatDialect._convert_usage(None) is None


# ==================== chat 方言图片块转换 ====================


async def _resolver_fail(*args, **kwargs):
    raise AssertionError("resolver 不应被调用")


async def _resolver_data_url(block):
    return "data:image/jpeg;base64,QUJD"


async def test_chat_image_block_file_id_channel():
    """带 file_id 的内部块 + 通道开 -> {"type":"file"} 直引，不读本地副本"""
    block = {"type": "image", "local_id": "img_1", "file_id": "file-api-abc"}
    out = await OpenAIChatDialect._image_to_chat_block(
        block, _resolver_fail, file_id_channel=True,
        file_resolver=AsyncMock(return_value="file-api-abc"),
    )
    assert out == {"type": "file", "file_id": "file-api-abc"}


async def test_chat_image_block_channel_off_falls_back_to_base64():
    """通道关闭（非 deepseek / 开关关）-> base64 data URL 兜底"""
    block = {"type": "image", "local_id": "img_1", "file_id": "file-api-abc"}
    out = await OpenAIChatDialect._image_to_chat_block(
        block, _resolver_data_url, file_id_channel=False,
    )
    assert out == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}


async def test_chat_image_block_without_file_id_falls_back_to_base64():
    """通道开但块内无 file_id -> base64 兜底"""
    block = {"type": "image", "local_id": "img_1"}
    out = await OpenAIChatDialect._image_to_chat_block(
        block, _resolver_data_url, file_id_channel=True,
    )
    assert out == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}


async def test_chat_prepare_messages_keeps_file_block_multimodal():
    """file 块消息应保持多模态数组形态，不被拍平成字符串"""
    dialect = OpenAIChatDialect()
    cfg = ModelConfig(
        name="m", base_url="https://example.com", api_key="k",
        vision=True, provider="deepseek", chat_file_id_channel=True,
    )
    context = SimpleNamespace(
        image_resolver=None,
        image_file_resolver=AsyncMock(return_value="file-api-abc"),
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "看这张"},
            {"type": "image", "local_id": "img_1", "file_id": "file-api-abc"},
        ],
    }]
    out = await dialect._prepare_messages(cfg, messages, context)
    assert out[0]["content"] == [
        {"type": "text", "text": "看这张"},
        {"type": "file", "file_id": "file-api-abc"},
    ]


async def test_chat_prepare_messages_skips_roleless_items():
    """history 中的 web_search_call 等无 role item（Responses 专属）应被跳过，不产生非法消息"""
    dialect = OpenAIChatDialect()
    cfg = ModelConfig(name="m", base_url="https://example.com", api_key="k", vision=False)
    context = SimpleNamespace(image_resolver=None)
    messages = [
        {"role": "user", "content": "搜下天气"},
        {"type": "web_search_call", "id": "ws_1", "status": "completed",
         "action": {"type": "search", "queries": ["天气"]}},
        {"role": "assistant", "content": "今天晴"},
        {"role": "user", "content": "好的"},
    ]
    out = await dialect._prepare_messages(cfg, messages, context)
    assert out == [
        {"role": "user", "content": "搜下天气"},
        {"role": "assistant", "content": "今天晴"},
        {"role": "user", "content": "好的"},
    ]


# ==================== responses 方言 ====================


def test_responses_convert_full():
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, total_tokens=15,
        input_tokens_details=SimpleNamespace(cached_tokens=4),
        output_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )
    n = ResponsesDialect._convert_usage(usage)
    assert n == NormalizedUsage(
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        cached_tokens=4, reasoning_tokens=2,
    )


def test_responses_convert_total_missing_falls_back_to_sum():
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    n = ResponsesDialect._convert_usage(usage)
    assert n.total_tokens == 15


def test_responses_convert_none():
    assert ResponsesDialect._convert_usage(None) is None
