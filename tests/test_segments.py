"""
segments 分段纯函数 + WebAdapter 流式分段切分测试

覆盖：
- split_segments：普通分段 / 围栏保护 / 连续空行 / 段数上限 / trim / 空文本
- find_confirmed_boundaries：已确认边界 / 末尾空行待定 / 边界不漂移 / 围栏内空行
- WebAdapter._handle_text_delta：跨增量边界的 delta 切分与 message_seg_end 时序
"""

from agent.segments import find_confirmed_boundaries, split_segments
from web.adapter import WebAdapter


# ========== split_segments ==========

class TestSplitSegments:
    def test_no_blank_line(self):
        assert split_segments("hello\nworld") == ["hello\nworld"]

    def test_two_segments(self):
        assert split_segments("第一段\n\n第二段") == ["第一段", "第二段"]

    def test_three_segments(self):
        assert split_segments("a\n\nb\n\nc") == ["a", "b", "c"]

    def test_consecutive_blank_lines_merge(self):
        assert split_segments("a\n\n\n\nb") == ["a", "b"]

    def test_strip_segment_edges(self):
        assert split_segments("  a  \n\n  b  ") == ["a", "b"]

    def test_empty_text(self):
        assert split_segments("") == []

    def test_whitespace_only(self):
        assert split_segments("   \n\n  \n\n ") == []

    def test_no_trailing_empty_segment(self):
        assert split_segments("a\n\n") == ["a"]

    def test_blank_line_inside_fence_not_split(self):
        text = "a\n\n```\nx\n\ny\n```\n\nb"
        assert split_segments(text) == ["a", "```\nx\n\ny\n```", "b"]

    def test_tilde_fence(self):
        text = "a\n\n~~~\nx\n\ny\n~~~\n\nb"
        assert split_segments(text) == ["a", "~~~\nx\n\ny\n~~~", "b"]

    def test_fence_with_info_string(self):
        text = "前\n\n```python\nprint(1)\n\nprint(2)\n```\n\n后"
        assert split_segments(text) == ["前", "```python\nprint(1)\n\nprint(2)\n```", "后"]

    def test_unclosed_fence_trailing_blank_not_split(self):
        # 围栏未闭合：尾部空行仍在围栏内，不切
        assert split_segments("a\n\n```\nx\n\ny") == ["a", "```\nx\n\ny"]

    def test_fence_close_line_with_trailing_spaces(self):
        text = "a\n\n```\nx\n```   \n\nb"
        assert split_segments(text) == ["a", "```\nx\n```", "b"]

    def test_fence_line_longer_close(self):
        # 闭合围栏长度 >= 开围栏即闭合
        text = "a\n\n```\nx\n````\n\nb"
        assert split_segments(text) == ["a", "```\nx\n````", "b"]

    def test_indented_fence(self):
        # 开围栏行的缩进被段首 trim 去掉（markdown 渲染等价），内部内容原样保留
        text = "a\n\n   ```\nx\n\ny\n   ```\n\nb"
        assert split_segments(text) == ["a", "```\nx\n\ny\n   ```", "b"]

    def test_max_segments_at_limit(self):
        text = "\n\n".join(str(i) for i in range(8))
        assert split_segments(text) == [str(i) for i in range(8)]

    def test_max_segments_exceeded_no_split(self):
        text = "\n\n".join(str(i) for i in range(9))
        assert split_segments(text) == [text]

    def test_custom_max_segments(self):
        text = "\n\n".join("abc" for _ in range(3))
        assert split_segments(text, max_segments=2) == [text]

    def test_single_line_fence_only(self):
        assert split_segments("```\ncode\n```") == ["```\ncode\n```"]


# ========== find_confirmed_boundaries ==========

class TestFindConfirmedBoundaries:
    def test_no_boundary(self):
        assert find_confirmed_boundaries("abc\ndef") == []

    def test_empty_text(self):
        assert find_confirmed_boundaries("") == []

    def test_single_boundary(self):
        # "abc\n\ndef"：空行序列末尾在位置 5（"abc\n\n" 之后）
        assert find_confirmed_boundaries("abc\n\ndef") == [5]

    def test_trailing_blank_not_confirmed(self):
        # 末尾空行序列后面没有非空行内容：不确认（可能继续延长）
        assert find_confirmed_boundaries("abc\n\n") == []
        assert find_confirmed_boundaries("abc\n\n\n") == []

    def test_multiple_boundaries(self):
        assert find_confirmed_boundaries("a\n\nb\n\nc") == [3, 6]

    def test_boundary_position_stable_after_more_blanks(self):
        # 已确认边界不因后续追加空行而漂移
        assert find_confirmed_boundaries("abc\n\ndef") == [5]
        assert find_confirmed_boundaries("abc\n\ndef\n") == [5]
        assert find_confirmed_boundaries("abc\n\ndef\n\n") == [5]

    def test_consecutive_blanks_single_boundary(self):
        assert find_confirmed_boundaries("abc\n\n\n\ndef") == [7]

    def test_blank_inside_fence_ignored(self):
        text = "a\n\n```\nx\n\ny\n```\n\nb"
        assert find_confirmed_boundaries(text) == [3, 17]

    def test_unclosed_fence_no_boundary_after_open(self):
        # 围栏未闭合：其后空行都在围栏内
        assert find_confirmed_boundaries("a\n\n```\nx\n\n") == [3]

    def test_boundary_split_across_deltas(self):
        # 模拟跨增量："\n" 单独到达时无边界；"\n" + 内容到达后边界确认
        assert find_confirmed_boundaries("abc\n") == []
        assert find_confirmed_boundaries("abc\n\n") == []
        assert find_confirmed_boundaries("abc\n\nd") == [5]

    def test_multibyte_chars(self):
        # 位置按字符偏移计（非字节），中文场景验证："你好\n"占 3 字符，空行末尾在 4
        assert find_confirmed_boundaries("你好\n\n世界") == [4]


# ========== WebAdapter 流式分段 ==========

class FakeConnManager:
    """记录 broadcast 调用的假连接管理器"""

    def __init__(self):
        self.events = []

    async def broadcast(self, payload: dict):
        self.events.append(payload)


class FakeSettings:
    def __init__(self, split_messages: bool = True):
        self.split_messages = split_messages


def make_adapter(split_messages=True):
    conn = FakeConnManager()
    adapter = WebAdapter(
        session=None,
        conn_manager=conn,
        platform="web",
        settings=FakeSettings(split_messages),
    )
    return adapter, conn


def delta_events(events):
    return [(e["type"], e.get("data", {}).get("text", ""))
            for e in events if e["type"] in ("message_delta", "message_seg_end")]


class TestStreamSegmentation:
    async def test_no_boundary_passthrough(self):
        adapter, conn = make_adapter()
        await adapter._handle_text_delta("hello ")
        await adapter._handle_text_delta("world")
        kinds = [e["type"] for e in conn.events]
        assert kinds == ["message_delta", "message_delta"]
        assert conn.events[0]["data"]["text"] == "hello "
        assert conn.events[1]["data"]["text"] == "world"

    async def test_boundary_across_deltas(self):
        # delta 顺序模拟：段尾 \n、空行 \n、新段内容——边界在内容到达后确认
        adapter, conn = make_adapter()
        await adapter._handle_text_delta("hello\n")
        await adapter._handle_text_delta("\n")
        # 空行已透传但边界未确认（后面没内容）
        assert [e["type"] for e in conn.events] == ["message_delta", "message_delta"]
        await adapter._handle_text_delta("world")
        evs = delta_events(conn.events)
        # 第三个 delta 到达：先透传段尾（空）、发 seg_end、再透传 "world"
        assert evs[-2] == ("message_seg_end", "")
        assert evs[-1] == ("message_delta", "world")

    async def test_boundary_inside_single_delta(self):
        # 一个 delta 内完整跨越段边界：切分透传，先段尾、seg_end、再新段头
        adapter, conn = make_adapter()
        await adapter._handle_text_delta("hello\n\nworld")
        evs = delta_events(conn.events)
        assert evs == [
            ("message_delta", "hello\n\n"),
            ("message_seg_end", ""),
            ("message_delta", "world"),
        ]

    async def test_multiple_boundaries_single_delta(self):
        adapter, conn = make_adapter()
        await adapter._handle_text_delta("a\n\nb\n\nc")
        evs = delta_events(conn.events)
        assert evs == [
            ("message_delta", "a\n\n"),
            ("message_seg_end", ""),
            ("message_delta", "b\n\n"),
            ("message_seg_end", ""),
            ("message_delta", "c"),
        ]

    async def test_no_duplicate_seg_end_for_consecutive_blanks(self):
        # 连续空行只发一次 seg_end
        adapter, conn = make_adapter()
        await adapter._handle_text_delta("a\n\n\n\nb")
        evs = delta_events(conn.events)
        assert evs.count(("message_seg_end", "")) == 1

    async def test_fence_content_not_split(self):
        adapter, conn = make_adapter()
        await adapter._handle_text_delta("```\ncode\n\nmore\n```\n\nafter")
        evs = delta_events(conn.events)
        # 只有围栏结束后的空行是边界
        assert evs.count(("message_seg_end", "")) == 1
        # 围栏整体（含内部空行）在边界之前完整透传
        assert evs[0] == ("message_delta", "```\ncode\n\nmore\n```\n\n")

    async def test_switch_off_passthrough(self):
        adapter, conn = make_adapter(split_messages=False)
        await adapter._handle_text_delta("a\n\nb")
        evs = delta_events(conn.events)
        assert evs == [("message_delta", "a\n\nb")]

    async def test_reset_after_tool_call(self):
        adapter, conn = make_adapter()
        await adapter._handle_text_delta("a\n\nb")
        adapter._reset_stream_state()
        # 重置后缓冲清空，新一段从零开始
        await adapter._handle_text_delta("next")
        evs = delta_events(conn.events)
        assert evs[-1] == ("message_delta", "next")
        assert [e for e in evs].count(("message_delta", "next")) == 1

    async def test_empty_delta_noop(self):
        adapter, conn = make_adapter()
        await adapter._handle_text_delta("abc")
        await adapter._handle_text_delta("")
        assert [e["type"] for e in conn.events] == ["message_delta"]

    async def test_text_event_split(self):
        adapter, conn = make_adapter()
        await adapter._dispatch(_make_event("TEXT", {"content": "a\n\n```\nx\n\ny\n```\n\nb"}))
        messages = [e["data"]["text"] for e in conn.events if e["type"] == "message"]
        assert messages == ["a", "```\nx\n\ny\n```", "b"]

    async def test_text_event_switch_off(self):
        adapter, conn = make_adapter(split_messages=False)
        await adapter._dispatch(_make_event("TEXT", {"content": "a\n\nb"}))
        messages = [e["data"]["text"] for e in conn.events if e["type"] == "message"]
        assert messages == ["a\n\nb"]

    async def test_send_message_grey_bubble_split(self):
        adapter, conn = make_adapter()
        await adapter._dispatch(_make_event("TOOL_CALL_START", {
            "name": "send_message", "tool_call_id": "tc1",
            "arguments": '{"text":"外部\\n\\n消息","platform":"qq"}',
        }))
        await adapter._dispatch(_make_event(
            "TOOL_RESULT", {"name": "send_message", "tool_call_id": "tc1", "result": "ok"}
        ))
        messages = [(e["data"]["platform"], e["data"]["text"])
                    for e in conn.events if e["type"] == "message"]
        assert messages == [("qq", "外部"), ("qq", "消息")]

    async def test_failed_send_does_not_display_delivered_bubble(self):
        adapter, conn = make_adapter()
        await adapter._dispatch(_make_event("TOOL_CALL_START", {
            "name": "send_message", "tool_call_id": "tc1",
            "arguments": '{"text":"synthetic message","platform":"qq"}',
        }))
        await adapter._dispatch(_make_event(
            "TOOL_RESULT", {"name": "send_message", "tool_call_id": "tc1",
                            "result": '{"error":"synthetic send failure"}'}
        ))
        assert [e["type"] for e in conn.events] == ["tool_result"]


def _make_event(type_name: str, data: dict):
    """构造轻量 Event 替身（避免依赖 protocol 完整初始化路径）"""
    from agent.protocol import Event, EventType

    return Event(type=EventType[type_name], data=data)
