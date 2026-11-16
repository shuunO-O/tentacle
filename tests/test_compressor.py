"""
压缩模块消息 content 归一化单元测试

多模态消息（content 为数组）进入压缩提示词前必须转为纯文本，
避免 dict repr 污染（图片块以 [图片] 占位）。
"""

from agent.compressor import Compressor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.protocol import LoopResult


def test_content_to_text_plain_string():
    assert Compressor._content_to_text("你好") == "你好"


def test_content_to_text_none():
    assert Compressor._content_to_text(None) is None


def test_content_to_text_extracts_text_and_counts_images():
    blocks = [
        {"type": "text", "text": "看这张图"},
        {"type": "image", "local_id": "l-1", "file_id": "f-1"},
        {"type": "text", "text": "还有这张"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]
    assert Compressor._content_to_text(blocks) == "看这张图\n还有这张\n[图片×2]"


def test_content_to_text_image_only():
    blocks = [{"type": "image", "local_id": "l-1"}]
    assert Compressor._content_to_text(blocks) == "[图片×1]"


def test_content_to_text_ignores_non_dict_blocks():
    blocks = ["junk", {"type": "text", "text": "ok"}]
    assert Compressor._content_to_text(blocks) == "ok"


@pytest.mark.parametrize("answer", ['{}', '{"summaries": null}', '[{}]', '[{"summary":""}]', '[42]'])
async def test_invalid_summary_structure_is_not_success(monkeypatch, answer):
    compressor = object.__new__(Compressor)
    compressor._system_prompt = "synthetic compressor"
    compressor.manager = SimpleNamespace(get_model=lambda role: object())
    compressor.chronicle = SimpleNamespace(append_summaries=Mock())
    monkeypatch.setattr('agent.compressor.AgentLoop', lambda **kw: SimpleNamespace(
        run=AsyncMock(return_value=LoopResult([], answer, {})),
    ))
    assert not await compressor._compress_messages([{"content": "synthetic"}], 1, 2)
    compressor.chronicle.append_summaries.assert_not_called()


@pytest.mark.parametrize("answer", ['[]', '{"summaries": []}'])
async def test_explicit_empty_summary_list_is_valid(monkeypatch, answer):
    compressor = object.__new__(Compressor)
    compressor._system_prompt = "synthetic compressor"
    compressor.manager = SimpleNamespace(get_model=lambda role: object())
    compressor.chronicle = SimpleNamespace(append_summaries=Mock())
    monkeypatch.setattr('agent.compressor.AgentLoop', lambda **kw: SimpleNamespace(
        run=AsyncMock(return_value=LoopResult([], answer, {})),
    ))
    assert await compressor._compress_messages([{"content": "synthetic"}], 1, 2)
    compressor.chronicle.append_summaries.assert_not_called()
