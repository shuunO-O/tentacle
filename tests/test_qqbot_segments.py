"""
QQAdapter._deliver 出站分段测试

mock _send_c2c_text 验证两层切分行为：
- 语义分段（空行切段、代码块保护、段数上限）
- 每段超 max_chunk 时的长度硬分片兜底
- split_messages 开关关闭 / 空文本 / 无发送目标

QQAdapter 构造不发起网络连接（aiohttp session 惰性创建）；
测试屏蔽最近会话文件读取，使用合成目标，不读取生产数据。
"""

import pytest

from plugins.bundled.qqbot.adapter import QQAdapter


@pytest.fixture(autouse=True)
def isolate_chat_file(monkeypatch):
    monkeypatch.setattr(QQAdapter, "_load_latest_chat", lambda self: None)


def make_qq_adapter(**kwargs) -> tuple[QQAdapter, list]:
    """构造测试用 QQAdapter + 发送记录器；强制注入发送目标、关掉限频间隔。"""
    sent: list[str] = []

    async def fake_send(openid, content, reply_to=None):
        sent.append(content)

    adapter = QQAdapter(
        session=None,
        app_id="test",
        client_secret="test",
        default_to="openid-test",
        **kwargs,
    )
    # 覆盖可能读到的生产会话目标；压掉限频 sleep
    adapter._latest_chat = {"type": "c2c", "chat_id": "openid-test", "ts": 0}
    adapter._send_interval = 0
    adapter._send_c2c_text = fake_send
    return adapter, sent


class TestDeliverSegmentation:
    async def test_multi_segment_sends_multiple(self):
        adapter, sent = make_qq_adapter()
        await adapter._deliver("第一段\n\n第二段\n\n第三段")
        assert sent == ["第一段", "第二段", "第三段"]

    async def test_single_segment_one_message(self):
        adapter, sent = make_qq_adapter()
        await adapter._deliver("只有一段\n没有空行")
        assert sent == ["只有一段\n没有空行"]

    async def test_blank_inside_fence_not_split(self):
        adapter, sent = make_qq_adapter()
        text = "说明\n\n```\ncode\n\nmore\n```\n\n结尾"
        await adapter._deliver(text)
        assert sent == ["说明", "```\ncode\n\nmore\n```", "结尾"]

    async def test_exceed_max_segments_no_split(self):
        # 默认上限 3：拆出 4 段 > 3，视为整篇长文不拆
        adapter, sent = make_qq_adapter()
        text = "a\n\nb\n\nc\n\nd"
        await adapter._deliver(text)
        assert sent == [text]

    async def test_at_max_segments_still_splits(self):
        # 恰好 3 段 = 上限内，正常拆
        adapter, sent = make_qq_adapter()
        await adapter._deliver("a\n\nb\n\nc")
        assert sent == ["a", "b", "c"]

    async def test_custom_max_segments(self):
        adapter, sent = make_qq_adapter(split_max_segments=5)
        await adapter._deliver("a\n\nb\n\nc\n\nd")
        assert sent == ["a", "b", "c", "d"]

    async def test_switch_off_single_message(self):
        adapter, sent = make_qq_adapter(split_messages=False)
        await adapter._deliver("a\n\nb\n\nc")
        assert sent == ["a\n\nb\n\nc"]

    async def test_long_segment_hard_chunked(self):
        # 单段超 max_chunk：段内按长度硬分片兜底
        adapter, sent = make_qq_adapter(max_chunk=10)
        long_seg = "x" * 25  # 25 字符 → 10+10+5
        await adapter._deliver(f"短段\n\n{long_seg}")
        assert sent == ["短段", "x" * 10, "x" * 10, "x" * 5]

    async def test_empty_text_not_sent(self):
        adapter, sent = make_qq_adapter()
        with pytest.raises(ValueError, match="消息不能为空"):
            await adapter._deliver("   ")
        assert sent == []

    async def test_no_target_reported(self):
        adapter, sent = make_qq_adapter()
        adapter._latest_chat = None
        adapter._default_to = ""
        with pytest.raises(RuntimeError, match="无可用会话目标"):
            await adapter._deliver("hello")
        assert sent == []

    async def test_text_trimmed(self):
        adapter, sent = make_qq_adapter()
        await adapter._deliver("  首尾空白  \n\n  第二段  ")
        assert sent == ["首尾空白", "第二段"]


class TestQQAdapterInit:
    def test_defaults(self):
        adapter = QQAdapter(session=None, app_id="t", client_secret="t")
        assert adapter._split_messages is True
        assert adapter._split_max_segments == 3

    def test_zero_max_segments_falls_back(self):
        # split_max_segments=0 视为未配置，回退默认 3
        adapter = QQAdapter(session=None, app_id="t", client_secret="t", split_max_segments=0)
        assert adapter._split_max_segments == 3
