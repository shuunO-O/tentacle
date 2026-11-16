"""中断协议与流资源释放：合成流，无真实模型、工具或用户数据。"""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai.types.chat import ChatCompletionMessage

from agent.chronicle import ChronicleManager
from agent.loop import AgentLoop
from agent.protocol import Context, EventType, ModelConfig
from dialects import get_dialect


@pytest.mark.parametrize("api_style", ["chat", "responses", "interactions", "gemini_generate"])
@pytest.mark.parametrize("shutdown", [False, True])
async def test_stop_closes_each_protocol_stream_and_never_saves_partial_tool_arguments(api_style, shutdown):
    emitted, closed = asyncio.Event(), asyncio.Event()
    payloads = {
        "interactions": {"event_type": "step.start", "index": 0,
                         "step": {"type": "model_output", "content": [{"type": "text", "text": "partial"}]}},
        "gemini_generate": {"candidates": [{"content": {"role": "model", "parts": [{"text": "partial"}]}}]},
    }

    class SyntheticStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ("data: " + json.dumps(payloads[api_style]) + "\n\n").encode()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SyntheticStream())

    async with httpx.AsyncClient(base_url="https://synthetic.invalid/", transport=httpx.MockTransport(handle)) as http:
        class SDKStream:
            async def __aiter__(self):
                if api_style == "chat":
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content="partial", reasoning_content=None, tool_calls=[SimpleNamespace(
                            index=0, id="unfinished", function=SimpleNamespace(
                                name="never_execute", arguments='{"incomplete":'),
                        )],
                    ))], usage=None)
                else:
                    yield SimpleNamespace(type="response.output_text.delta", delta="partial")
                await asyncio.Event().wait()

            async def close(self):
                closed.set()

        async def create(**kwargs):
            requests.append(kwargs)
            return SDKStream()

        client = (SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                  responses=SimpleNamespace(create=create))
                  if api_style in ("chat", "responses") else http)

        async def callback(event):
            if event.type == EventType.TEXT_DELTA:
                emitted.set()

        loop = AgentLoop.__new__(AgentLoop)
        loop.client, loop.callback, loop.dialect = client, callback, get_dialect(api_style)
        loop.model_config = ModelConfig("synthetic", "https://synthetic.invalid", api_style=api_style)
        loop.tool_definitions, loop.checkpoint = [], None
        loop.tool_executor = SimpleNamespace(execute=AsyncMock())
        context = Context("synthetic", [], "", "web", stop_event=asyncio.Event())
        task = asyncio.create_task(loop.run([{"role": "user", "content": "synthetic"}], context))
        ready = asyncio.create_task(emitted.wait())
        done, _ = await asyncio.wait((task, ready), timeout=2, return_when=asyncio.FIRST_COMPLETED)
        if ready not in done:
            ready.cancel()
            task.cancel()
            await asyncio.gather(task, ready, return_exceptions=True)
            pytest.fail("synthetic stream never emitted text")
        if shutdown:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            context.stop_event.set()
            result = await asyncio.wait_for(task, 2)
            assert result.stop_reason == "cancelled" and result.final_answer == "partial"
            assert result.full_messages[-1] == {"role": "assistant", "content": "partial", "interrupted": True}
            assert not any(m.get("tool_calls") for m in result.full_messages)
        assert closed.is_set() and len(requests) == 1
        assert not context.stop_event._waiters
        loop.tool_executor.execute.assert_not_awaited()


async def test_explicit_stop_wins_simultaneous_complete_response_without_executing_tools():
    context = Context("synthetic", [], "", "web", stop_event=asyncio.Event())

    async def call(**kwargs):
        context.stop_event.set()
        return SimpleNamespace(message=ChatCompletionMessage(role="assistant", content=None, tool_calls=[{
            "id": "never", "type": "function", "function": {"name": "write", "arguments": "{}"},
        }]), usage=None, web_search_calls=[])

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_config = ModelConfig("synthetic", "https://synthetic.invalid")
    loop.client, loop.callback = None, AsyncMock()
    loop.dialect = SimpleNamespace(call=call)
    loop.tool_executor = SimpleNamespace(execute=AsyncMock())
    loop.tool_definitions, loop.checkpoint = [], None
    result = await loop.run([], context)
    assert result.stop_reason == "cancelled" and result.full_messages == []
    loop.tool_executor.execute.assert_not_awaited()


def test_interrupted_column_migrates_without_changing_old_archive_or_delivery_receipts(tmp_path):
    path = tmp_path / "synthetic-old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                   "content TEXT, created_time REAL)")
        db.execute("INSERT INTO raw_messages VALUES (1, 'synthetic', 'assistant', 'old text', 1)")
    chronicle = ChronicleManager(str(path))
    chronicle.append_messages("synthetic", [
        {"role": "user", "content": "synthetic", "platform": "web"},
        {"role": "assistant", "content": "partial", "interrupted": True,
         "delivery": {"status": "sent", "platform": "qq"}},
    ])
    # 再次初始化也不改写已有内容；按真实用户轮次查询仍携带中断标记。
    chronicle = ChronicleManager(str(path))
    old = chronicle.get_recent_rounds()[0]
    assert old["content"] == "old text" and "interrupted" not in old
    saved = chronicle.get_recent_rounds(conversation_only=True)[-1]
    assert saved["content"] == "partial" and saved["interrupted"]
    assert saved["delivery"] == {"status": "sent", "platform": "qq"}
