"""可见消息实时/历史一致性；仅使用合成事件、假模型及临时数据库。"""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent.session import AgentSession
from agent.chronicle import ChronicleManager
from agent.protocol import Event, EventType, LoopResult
from web.adapter import WebAdapter
from web.server import create_app
from web.settings import WebSettings


def event(kind, **data):
    return Event(EventType[kind], data)


def displayed_messages(payloads):
    """按现有 WS 协议模拟气泡的增量追加、工具边界与发送确认。"""
    bubbles, streaming = [], False
    for p in payloads:
        kind, data = p["type"], p.get("data", {})
        if kind == "message_delta":
            if not streaming:
                bubbles.append({"platform": data["platform"], "text": ""})
            bubbles[-1]["text"] += data["text"]
            streaming = True
        elif kind == "message":
            bubbles.append({"platform": data["platform"], "text": data["text"]})
            streaming = False
        elif kind in ("tool_call", "message_seg_end", "final", "error"):
            streaming = False
    return [(b["platform"], b["text"].strip()) for b in bubbles]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("delivery", ["success", "failure", "missing"])
async def test_live_messages_equal_saved_history(tmp_path, monkeypatch, stream, split, delivery):
    chronicle = ChronicleManager(str(tmp_path / "chronicle.db"))
    settings = WebSettings(str(tmp_path / "settings.json"))
    settings.update(split_messages=split)
    active = SimpleNamespace(session_id="synthetic", start_time=1, history=[])
    agent = AgentSession.__new__(AgentSession)
    agent._lock = asyncio.Lock()
    agent.platform, agent._system_prompt = "web", "synthetic"
    agent.chronicle = chronicle
    agent.session_manager = SimpleNamespace(
        maybe_compress_daily=AsyncMock(), get_or_create_active_session=lambda: active,
        record_input_time=Mock(), context_tokens=0, save_session=Mock(),
    )
    agent._save_rollback_state = Mock()
    payloads = []
    async def broadcast(payload):
        payloads.append(payload)
    adapter = WebAdapter(agent, SimpleNamespace(broadcast=broadcast), settings=settings)
    agent.callback = adapter._callback
    # The marker is ordinary prose, never a fabricated sent-message bubble.
    before = "我先检查一下\n\n[send_message][qq]\n只是格式示例\n[/send_message]"
    sent, after = "已通知\n\n补充通知", "检查完成\n\n第二段"

    async def run(messages, context):
        async def text(content):
            if stream:
                for delta in (content[:4], content[4:9], content[9:]):
                    await agent._loop.callback(event("TEXT_DELTA", content=delta))
            else:
                await agent._loop.callback(event("TEXT", content=content))
        await text(before)
        await agent._loop.callback(event("TOOL_CALL_START", name="send_message", tool_call_id="send-1",
                                        arguments=json.dumps({"platform": "qq", "text": sent})))
        if delivery != "missing":
            result = {"status": "sent"} if delivery == "success" else {"error": "synthetic failure"}
            await agent._loop.callback(event("TOOL_RESULT", name="send_message", tool_call_id="send-1",
                                            result=json.dumps(result)))
        await text(after)
        await agent._loop.callback(event("FINAL_ANSWER", content=after))
        return LoopResult([*messages, {"role": "assistant", "content": before},
                           {"role": "assistant", "content": after}], after, {})

    agent._loop = SimpleNamespace(run=run, callback=agent._on_event)
    monkeypatch.setattr('agent.session.FileManager', lambda: SimpleNamespace(configured=False))
    await agent.run_round("synthetic input", "harmony")
    saved = chronicle.get_recent_rounds(limit=1)
    expected = [before, sent, after] if delivery == "success" else [before, after]
    assert [m["content"] for m in saved if m["role"] == "assistant"] == expected
    assert len({m["round_id"] for m in saved}) == 1
    assert [m["id"] for m in saved] == sorted(m["id"] for m in saved)
    assert agent._visible_messages is None
    assert active.history[-2]["content"] == before

    app = create_app(None, adapter, agent, None, None, chronicle, settings, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://synthetic") as client:
        response = await client.get("/api/chat/history?limit=1")
    assert response.status_code == 200
    history = response.json()
    assert history["has_more"] is False
    visible_history = [(m["platform"], text.strip()) for m in history["messages"] if m["role"] == "assistant"
                       for text in m["segments"]]
    assert displayed_messages(payloads) == visible_history
    assert all(isinstance(m["content"], str) for m in history["messages"])
    assert any("只是格式示例" in text and "[send_message]" in text for _, text in visible_history)


async def test_history_paginates_complete_rounds_by_id_not_clock(tmp_path):
    chronicle = ChronicleManager(str(tmp_path / "chronicle.db"))
    for number in range(4):
        chronicle.append_messages("synthetic", [
            {"role": "user", "content": f"input {number}"},
            *[{"role": "assistant", "content": f"output {number}-{part}", "platform": "web"}
              for part in range(number + 1)],
        ], round_id=f"round-{number}")
    # Identical and backwards wall-clock timestamps must not reorder messages.
    with sqlite3.connect(chronicle.db_path) as db:
        db.execute("UPDATE raw_messages SET created_time = 100 - id")
    assert len(chronicle.get_recent_rounds(limit=1)) == 5
    assert len(chronicle.get_recent_rounds(limit=2)) == 9
    settings = WebSettings(str(tmp_path / "settings.json"))
    app = create_app(None, None, None, None, None, chronicle, settings, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://synthetic") as client:
        first = (await client.get("/api/chat/history?limit=2")).json()
        all_rounds = (await client.get("/api/chat/history?limit=4")).json()
    assert first["has_more"] is True and len(first["messages"]) == 9
    assert all_rounds["has_more"] is False and len(all_rounds["messages"]) == 14
    assert [m["id"] for m in first["messages"]] == list(range(6, 15))
    assert {m["round_id"] for m in first["messages"]} == {"round-2", "round-3"}
    assert all_rounds["messages"][-9:] == first["messages"]

    chronicle.append_messages("another-session", [{"role": "user", "content": "separate input"}],
                              round_id="another-round")
    assert [m["round_id"] for m in chronicle.get_recent_rounds("synthetic", limit=1)] == ["round-3"] * 5


async def test_stream_error_retains_only_displayed_text_and_does_not_leak_pending_sends():
    from agent.visible import VisibleMessages
    visible = VisibleMessages("web")
    visible.consume(event("TEXT_DELTA", content="partial text"))
    visible.consume(event("TOOL_CALL_START", name="send_message", tool_call_id="missing",
                          arguments='{"platform":"qq","text":"unconfirmed"}'))
    visible.consume(event("ERROR", error="synthetic failure"))
    visible.consume(event("TOOL_RESULT", name="send_message", tool_call_id="missing", result='{"status":"sent"}'))
    assert visible.messages == [{"role": "assistant", "platform": "web", "content": "partial text"}]
