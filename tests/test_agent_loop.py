"""
AgentLoop 方言分支测试（mock client，不连网、不碰生产 data/）

为 dialects/ 适配层重构提供行为基线（Phase 0 护栏）：
1. chat 方言：非流式参数组装、流式收集与事件、图片块清洗、usage 缓存归一
2. responses 方言：input 转换、web_search 注入、流式事件、output 解析、usage 转换
3. 主循环：工具执行闭环、参数解析失败兜底、max_iterations、用量记录

所有测试打 AgentLoop.run() 公共接口（fake client 注入），
平移重构后测试零修改即应通过。
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

import agent.loop as agent_loop_mod
from agent.loop import AgentLoop
from agent.protocol import Context, EventType, ModelConfig
from runtime.tool_registry import registry


# ==================== fakes ====================


class FakeStream:
    """把 chunk 列表包装成 async iterator"""

    def __init__(self, chunks):
        self._iter = iter(chunks)

    def __aiter__(self):
        return self

    async def close(self):
        pass

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeEndpoint:
    """按 script 顺序返回响应；请求参数 stream=True 时 script 元素须为 chunk 列表"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **params):
        self.calls.append(params)
        assert self.script, "script 已耗尽：API 调用次数超出预期"
        item = self.script.pop(0)
        if params.get("stream"):
            return FakeStream(item)
        return item


class FakeClient:
    def __init__(self, chat=None, responses=None):
        self.chat = SimpleNamespace(completions=chat)
        self.responses = responses


class FakeTracker:
    def __init__(self):
        self.records = []

    async def record_usage(self, **kw):
        self.records.append(kw)


class EventCollector:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)

    def of_type(self, et):
        return [e for e in self.events if e.type == et]

    def types(self):
        return [e.type for e in self.events]


# ==================== helpers ====================


def make_config(**kw):
    # 原有用例验证 DeepSeek 契约；不再依赖 ModelConfig 的隐式供应商默认值。
    base = dict(name="test-model", base_url="https://example.com", api_key="sk-test", provider="deepseek")
    base.update(kw)
    return ModelConfig(**base)


def make_context(user_id=None):
    return Context(
        current_message="hi",
        history_messages=[],
        system_prompt="sys",
        platform="test",
        user_id=user_id,
    )


def make_loop(collector, client, **cfg):
    loop = AgentLoop(model_config=make_config(**cfg), callback=collector)
    loop.client = client
    return loop


def register_echo():
    registry.register(
        name="echo",
        description="回显工具",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        func=lambda args: json.dumps({"echo": args.get("text", "")}),
    )


def chat_message(content=None, tool_calls=None, reasoning=None):
    msg = ChatCompletionMessage(role="assistant", content=content, tool_calls=tool_calls)
    if reasoning:
        object.__setattr__(msg, "reasoning_content", reasoning)
    return msg


def tool_call_msg(call_id, name, arguments):
    return ChatCompletionMessage(
        role="assistant",
        content=None,
        tool_calls=[ChatCompletionMessageToolCall(
            id=call_id, type="function", function=Function(name=name, arguments=arguments),
        )],
    )


def usage_ds(pt=100, ct=50, hit=0):
    """DeepSeek Chat Completions usage 形态"""
    return SimpleNamespace(
        prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct,
        prompt_cache_hit_tokens=hit,
    )


def usage_openai(pt=100, ct=50, cached=0):
    """OpenAI Chat Completions usage 形态"""
    return SimpleNamespace(
        prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def usage_responses(inp=10, out=5, cached=0):
    """Responses API usage 形态"""
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out, total_tokens=inp + out,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def delta_chunk(content=None, reasoning=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


def usage_only_chunk(usage):
    return SimpleNamespace(choices=[], usage=usage)


def text_response(text, usage=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=chat_message(text))], usage=usage)


@pytest.fixture(autouse=True)
def clean_registry():
    """模块级 registry 实例的工具表清理（AgentLoop 内部引用同一实例）"""
    registry._tools.clear()
    yield
    registry._tools.clear()


@pytest.fixture
def tracker(monkeypatch):
    """替换用量记录，避免写生产 data/chronicle.db"""
    t = FakeTracker()
    monkeypatch.setattr(agent_loop_mod, "get_usage_tracker", lambda: t)
    return t


@pytest.fixture
def collector():
    return EventCollector()


# ==================== chat 方言 ====================


@pytest.mark.parametrize('api_style', ['chat', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('server_model', ['vendor/actual-v1.2', None])
async def test_usage_records_server_model_instead_of_configured_id(
        collector, tracker, api_style, stream, server_model):
    if api_style == 'chat':
        if stream:
            response = [
                SimpleNamespace(model=server_model, choices=[SimpleNamespace(
                    delta=SimpleNamespace(content='ok', tool_calls=None))], usage=None),
                SimpleNamespace(choices=[], usage=usage_ds()),
            ]
        else:
            response = text_response('ok', usage=usage_ds())
            response.model = server_model
    else:
        response = SimpleNamespace(model=server_model, status='completed', usage=usage_responses(),
                                   output=[SimpleNamespace(type='message', content=[
                                       SimpleNamespace(type='output_text', text='ok')])])
        if stream:
            response = [
                SimpleNamespace(type='response.created', response=response),
                SimpleNamespace(type='response.output_text.delta', delta='ok'),
                SimpleNamespace(type='response.completed', response=SimpleNamespace(usage=usage_responses())),
            ]
    endpoint = FakeEndpoint([response])
    client = FakeClient(**{api_style: endpoint})
    loop = make_loop(collector, client, api_style=api_style, stream=stream,
                     provider_id='connection', model_id='configured-id')
    result = await loop.run([{'role': 'user', 'content': 'hi'}], make_context())
    assert result.final_answer == 'ok'
    assert len(tracker.records) == 1
    assert tracker.records[0]['provider_id'] == 'connection'
    assert tracker.records[0]['model'] == (server_model or '')
    assert tracker.records[0]['model_id'] == (f'connection/{server_model}' if server_model else '')


async def test_chat_non_stream_params(collector, tracker):
    register_echo()
    chat = FakeEndpoint([text_response("你好", usage=usage_ds())])
    loop = make_loop(
        collector, FakeClient(chat=chat),
        stream=False, tool_names=["echo"], temperature=0.5,
        response_format={"type": "json_object"},
    )
    ctx = make_context(user_id="u1")
    result = await loop.run([{"role": "user", "content": "hi"}], ctx)

    params = chat.calls[0]
    assert params["model"] == "test-model"
    assert params["stream"] is False
    assert params["temperature"] == 0.5
    assert params["response_format"] == {"type": "json_object"}
    # DeepSeek 特性：user_id 经 extra_body 传递
    assert params["extra_body"] == {"user_id": "u1"}
    assert params["messages"] == [{"role": "user", "content": "hi"}]
    assert params["tools"][0]["function"]["name"] == "echo"
    assert "stream_options" not in params

    assert result.final_answer == "你好"
    assert result.total_usage["prompt_tokens"] == 100
    assert result.total_usage["completion_tokens"] == 50
    assert result.total_usage["total_tokens"] == 150
    assert result.total_usage["context_prompt_tokens"] == 100
    finals = collector.of_type(EventType.FINAL_ANSWER)
    assert finals[-1].data["content"] == "你好"


async def test_chat_user_id_fallback_to_config(collector, tracker):
    chat = FakeEndpoint([text_response("x", usage=usage_ds())])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False, user_id="cfg-u")
    await loop.run([{"role": "user", "content": "hi"}], make_context())
    assert chat.calls[0]["extra_body"] == {"user_id": "cfg-u"}


async def test_generic_chat_does_not_inherit_deepseek_user_id_quirk(collector, tracker):
    chat = FakeEndpoint([text_response("x", usage=usage_openai())])
    loop = make_loop(collector, FakeClient(chat=chat), provider="openai_compatible", stream=False)
    await loop.run([{"role": "user", "content": "hi"}], make_context(user_id="u1"))
    assert chat.calls[0]["user"] == "u1"
    assert "extra_body" not in chat.calls[0]


async def test_generic_responses_does_not_force_deepseek_tool_choice(collector, tracker):
    register_echo()
    response = SimpleNamespace(status="completed", output=[SimpleNamespace(type="message", content=[
        SimpleNamespace(type="output_text", text="ok")])], usage=usage_responses())
    endpoint = FakeEndpoint([response])
    loop = make_loop(collector, FakeClient(responses=endpoint), provider="openai_compatible",
                     api_style="responses", stream=False, tool_names=["echo"])
    await loop.run([{"role": "user", "content": "hi"}], make_context())
    assert endpoint.calls[0]["tools"]
    assert "tool_choice" not in endpoint.calls[0]


async def test_chat_stream_tool_loop(collector, tracker):
    register_echo()
    tc_1 = SimpleNamespace(index=0, id="call_1",
                           function=SimpleNamespace(name="echo", arguments='{"text":'))
    tc_2 = SimpleNamespace(index=0, id="",
                           function=SimpleNamespace(name="", arguments='"你好"}'))
    chunks_1 = [
        delta_chunk(reasoning="想一下"),
        delta_chunk(tool_calls=[tc_1]),
        delta_chunk(tool_calls=[tc_2]),
        usage_only_chunk(usage_openai(cached=7)),
    ]
    chunks_2 = [delta_chunk(content="完成"), usage_only_chunk(usage_openai(pt=200, ct=10))]
    chat = FakeEndpoint([chunks_1, chunks_2])
    loop = make_loop(collector, FakeClient(chat=chat), stream=True, tool_names=["echo"])
    result = await loop.run([{"role": "user", "content": "hi"}], make_context(user_id="u1"))

    # 流式必带 usage 上报配置
    assert chat.calls[0]["stream"] is True
    assert chat.calls[0]["stream_options"] == {"include_usage": True}

    types = collector.types()
    assert EventType.THINKING_DELTA in types
    assert EventType.TEXT_DELTA in types

    # 工具调用闭环：分片 arguments 拼装 + 执行
    start = collector.of_type(EventType.TOOL_CALL_START)[0]
    assert start.data["name"] == "echo"
    assert start.data["tool_call_id"] == "call_1"
    tr = collector.of_type(EventType.TOOL_RESULT)[0]
    assert json.loads(tr.data["result"]) == {"echo": "你好"}

    # 第二轮请求：assistant(tool_calls) + tool 结果在位
    second_msgs = chat.calls[1]["messages"]
    assert second_msgs[1]["tool_calls"][0]["id"] == "call_1"
    assert {"role": "tool", "tool_call_id": "call_1",
            "content": json.dumps({"echo": "你好"})} in second_msgs

    assert result.final_answer == "完成"
    # 两轮 usage 累计 + 单轮缓存字段记录
    assert result.total_usage["prompt_tokens"] == 300
    assert result.total_usage["total_tokens"] == 360
    assert result.total_usage["context_prompt_tokens"] == 200
    assert tracker.records[0]["cached_tokens"] == 7


async def test_chat_image_blocks_sanitized(collector, tracker):
    chat = FakeEndpoint([
        text_response("ok", usage=usage_ds()),
        text_response("ok2", usage=usage_ds()),
    ])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False)

    history_1 = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "file_id": "file-123"},
    ]}]
    await loop.run(history_1, make_context())
    assert chat.calls[0]["messages"][0]["content"] == "看图[图片]"

    history_2 = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]}]
    await loop.run(history_2, make_context())
    assert chat.calls[1]["messages"][0]["content"] == "[图片]"


async def test_chat_image_blocks_base64_when_vision(collector, tracker):
    """vision=True 且关闭 file_id 通道：chat 方言走 base64 兜底通道"""
    chat = FakeEndpoint([text_response("看到图了", usage=usage_ds())])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False, vision=True,
                     chat_file_id_channel=False)

    async def fake_resolver(block):
        return f"data:image/png;base64,{block['local_id']}"

    ctx = make_context()
    ctx.image_resolver = fake_resolver
    history = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "local_id": "l-1", "file_id": "f-1"},
    ]}]
    await loop.run(history, ctx)

    assert chat.calls[0]["messages"][0]["content"] == [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,l-1"}},
    ]


async def test_chat_image_blocks_file_id_channel(collector, tracker):
    """vision=True 默认（provider=deepseek + 通道开）：file_id 经 {"type":"file"} 块直引"""
    chat = FakeEndpoint([text_response("看到图了", usage=usage_ds())])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False, vision=True)

    async def fake_resolver(block):
        raise AssertionError("file_id 直引时不应读本地副本")

    ctx = make_context()
    ctx.image_resolver = fake_resolver
    ctx.image_file_resolver = AsyncMock(return_value="f-1")
    history = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "local_id": "l-1", "file_id": "f-1"},
    ]}]
    await loop.run(history, ctx)

    assert chat.calls[0]["messages"][0]["content"] == [
        {"type": "text", "text": "看图"},
        {"type": "file", "file_id": "f-1"},
    ]


async def test_chat_vision_image_url_passthrough(collector, tracker):
    """vision=True：外部 URL 的 image_url 块直接透传"""
    chat = FakeEndpoint([text_response("ok", usage=usage_ds())])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False, vision=True)
    history = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/y.png", "detail": "low"}},
    ]}]
    await loop.run(history, make_context())
    assert chat.calls[0]["messages"][0]["content"] == [
        {"type": "image_url", "image_url": {"url": "https://x/y.png", "detail": "low"}},
    ]


async def test_chat_vision_without_resolver_falls_back_placeholder(collector, tracker):
    """vision=True 但 FileManager 未配置（无 resolver）：降级占位文本且保持字符串形态"""
    chat = FakeEndpoint([text_response("ok", usage=usage_ds())])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False, vision=True)
    history = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "local_id": "l-1"},
    ]}]
    await loop.run(history, make_context())
    assert chat.calls[0]["messages"][0]["content"] == "看图[图片不可用]"


async def test_chat_usage_cache_hit_deepseek_form(collector, tracker):
    chat = FakeEndpoint([text_response("x", usage=usage_ds(hit=30))])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False)
    await loop.run([{"role": "user", "content": "hi"}], make_context())
    ev = collector.of_type(EventType.USAGE)[0]
    assert ev.data["cached_tokens"] == 30
    assert ev.data["prompt_tokens"] == 100


async def test_chat_usage_cache_hit_openai_form(collector, tracker):
    chat = FakeEndpoint([text_response("x", usage=usage_openai(cached=25))])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False)
    await loop.run([{"role": "user", "content": "hi"}], make_context())
    ev = collector.of_type(EventType.USAGE)[0]
    assert ev.data["cached_tokens"] == 25


async def test_chat_no_usage_event_when_absent(collector, tracker):
    chat = FakeEndpoint([text_response("x", usage=None)])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())
    assert collector.of_type(EventType.USAGE) == []
    assert result.total_usage["total_tokens"] == 0
    assert tracker.records == []


async def test_chat_non_stream_thinking_event(collector, tracker):
    msg = chat_message("答案", reasoning="推理过程")
    chat = FakeEndpoint([SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage_ds())])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False)
    await loop.run([{"role": "user", "content": "hi"}], make_context())
    think = collector.of_type(EventType.THINKING)
    assert think[0].data["content"] == "推理过程"


# ==================== responses 方言 ====================


async def test_responses_non_stream_params(collector, tracker):
    register_echo()
    resp = SimpleNamespace(
        status="completed",
        output=[
            SimpleNamespace(type="message",
                            content=[SimpleNamespace(type="output_text", text="答案")]),
            SimpleNamespace(type="reasoning",
                            content=[SimpleNamespace(type="reasoning_text", text="思考过程")]),
        ],
        usage=usage_responses(inp=10, out=5, cached=4),
    )
    responses = FakeEndpoint([resp])
    loop = make_loop(
        collector, FakeClient(responses=responses),
        api_style="responses", stream=False, tool_names=["echo"], temperature=0.2,
    )
    result = await loop.run([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                            make_context(user_id="u1"))

    params = responses.calls[0]
    # system 提升为 instructions，input 只含其余消息
    assert params["instructions"] == "sys"
    assert params["input"] == [{"role": "user", "content": "hi"}]
    # 工具定义扁平化
    assert params["tools"] == [{
        "type": "function", "name": "echo", "description": "回显工具",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    }]
    # DeepSeek 特性：必须显式 tool_choice
    assert params["tool_choice"] == "auto"
    assert params["temperature"] == 0.2
    # user 走顶层参数（区别于 chat 分支的 extra_body）
    assert params["user"] == "u1"
    assert "stream" not in params

    assert result.final_answer == "答案"
    assert collector.of_type(EventType.THINKING)[0].data["content"] == "思考过程"
    # usage 转换 + 缓存归一
    ev = collector.of_type(EventType.USAGE)[0]
    assert ev.data == {"prompt_tokens": 10, "completion_tokens": 5,
                       "total_tokens": 15, "cached_tokens": 4}
    assert result.total_usage["context_prompt_tokens"] == 10


async def test_responses_input_conversion(collector, tracker):
    resp = SimpleNamespace(
        status="completed",
        output=[SimpleNamespace(type="message",
                                content=[SimpleNamespace(type="output_text", text="好")])],
        usage=usage_responses(),
    )
    responses = FakeEndpoint([resp])
    loop = make_loop(collector, FakeClient(responses=responses),
                     api_style="responses", stream=False, vision=True)
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "file_id": "f-1"},
        ]},
        {"role": "assistant", "content": "好的",
         "tool_calls": [{"id": "c9", "type": "function",
                         "function": {"name": "echo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c9", "content": "结果"},
        {"type": "web_search_call", "id": "ws0", "status": "completed",
         "action": {"type": "search", "queries": ["q"]}},
        {"role": "user", "content": "继续"},
    ]
    ctx = make_context()
    ctx.image_resolver = AsyncMock(return_value="data:image/png;base64,QUJD")
    await loop.run(history, ctx)

    items = responses.calls[0]["input"]
    # 图片块转 input_image；文本块转 input_text
    assert items[0] == {"role": "user", "content": [
        {"type": "input_text", "text": "看图"},
        {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
    ]}
    assert items[1] == {"role": "assistant", "content": "好的"}
    # tool_calls 转 function_call items
    assert items[2] == {"type": "function_call", "call_id": "c9",
                        "name": "echo", "arguments": "{}"}
    # tool 结果转 function_call_output
    assert items[3] == {"type": "function_call_output", "call_id": "c9", "output": "结果"}
    # web_search_call item 原样透传
    assert items[4] == history[4]
    assert items[5] == {"role": "user", "content": "继续"}


async def test_responses_image_base64_fallback(collector, tracker):
    """无 file_id 的图片块经 resolver 转 base64 input_image（厂商无关兜底通道）"""
    resp = SimpleNamespace(
        status="completed",
        output=[SimpleNamespace(type="message",
                                content=[SimpleNamespace(type="output_text", text="好")])],
        usage=usage_responses(),
    )
    responses = FakeEndpoint([resp])
    loop = make_loop(collector, FakeClient(responses=responses),
                     api_style="responses", stream=False, vision=True)

    async def fake_resolver(block):
        return f"data:image/png;base64,{block['local_id']}"

    ctx = make_context()
    ctx.image_resolver = fake_resolver
    history = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "local_id": "l-9"},
    ]}]
    await loop.run(history, ctx)

    items = responses.calls[0]["input"]
    assert items[0] == {"role": "user", "content": [
        {"type": "input_text", "text": "看图"},
        {"type": "input_image", "image_url": "data:image/png;base64,l-9"},
    ]}


async def test_responses_image_vision_off_placeholder(collector, tracker):
    """vision=False：responses 方言把图片块替换为占位文本（非视觉模型不送图）"""
    resp = SimpleNamespace(
        status="completed",
        output=[SimpleNamespace(type="message",
                                content=[SimpleNamespace(type="output_text", text="好")])],
        usage=usage_responses(),
    )
    responses = FakeEndpoint([resp])
    loop = make_loop(collector, FakeClient(responses=responses), api_style="responses", stream=False)
    history = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "local_id": "l-1", "file_id": "f-1"},
    ]}]
    await loop.run(history, make_context())

    items = responses.calls[0]["input"]
    assert items[0] == {"role": "user", "content": [
        {"type": "input_text", "text": "看图"},
        {"type": "input_text", "text": "[图片]"},
    ]}


async def test_responses_image_unresolvable_placeholder(collector, tracker):
    """vision=True 但 resolver 缺失/返回 None：占位文本兜底"""
    resp = SimpleNamespace(
        status="completed",
        output=[SimpleNamespace(type="message",
                                content=[SimpleNamespace(type="output_text", text="好")])],
        usage=usage_responses(),
    )
    responses = FakeEndpoint([resp])
    loop = make_loop(collector, FakeClient(responses=responses),
                     api_style="responses", stream=False, vision=True)
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1"},
    ]}]
    await loop.run(history, make_context())

    items = responses.calls[0]["input"]
    assert items[0] == {"role": "user", "content": [{"type": "input_text", "text": "[图片不可用]"}]}


async def test_responses_web_search_only_injection(collector, tracker):
    """AGENTS.md 记录的坑：纯 web_search 配置时本地 tools 为空也必须注入服务端工具"""
    resp = SimpleNamespace(
        status="completed",
        output=[SimpleNamespace(type="message",
                                content=[SimpleNamespace(type="output_text", text="s")])],
        usage=usage_responses(),
    )
    responses = FakeEndpoint([resp])
    loop = make_loop(collector, FakeClient(responses=responses),
                     api_style="responses", stream=False, tool_names=["web_search"])
    await loop.run([{"role": "user", "content": "搜下"}], make_context())
    params = responses.calls[0]
    assert params["tools"] == [{"type": "web_search"}]
    assert params["tool_choice"] == "auto"


async def test_responses_web_search_call_parse_non_stream(collector, tracker):
    resp = SimpleNamespace(
        status="completed",
        output=[
            SimpleNamespace(type="web_search_call", id="ws1", status="completed",
                            action=SimpleNamespace(type="search", queries=["天气", "ws_call_id=abc"])),
            SimpleNamespace(type="message",
                            content=[SimpleNamespace(type="output_text", text="结果")]),
        ],
        usage=usage_responses(),
    )
    responses = FakeEndpoint([resp])
    loop = make_loop(collector, FakeClient(responses=responses),
                     api_style="responses", stream=False, tool_names=["web_search"])
    result = await loop.run([{"role": "user", "content": "q"}], make_context())

    ev = collector.of_type(EventType.WEB_SEARCH)[0]
    assert ev.data["query"] == "天气"  # ws_call_id 调试串已过滤
    assert ev.data["status"] == "completed"
    # web_search_call item 追加进 history（多轮回传）
    assert any(isinstance(m, dict) and m.get("type") == "web_search_call"
               for m in result.full_messages)
    assert result.final_answer == "结果"


async def test_responses_tool_loop_non_stream(collector, tracker):
    register_echo()
    resp_1 = SimpleNamespace(
        status="completed",
        output=[SimpleNamespace(type="function_call", call_id="c1", name="echo",
                                arguments='{"text": "a"}')],
        usage=usage_responses(),
    )
    resp_2 = SimpleNamespace(
        status="completed",
        output=[SimpleNamespace(type="message",
                                content=[SimpleNamespace(type="output_text", text="done")])],
        usage=usage_responses(),
    )
    responses = FakeEndpoint([resp_1, resp_2])
    loop = make_loop(collector, FakeClient(responses=responses),
                     api_style="responses", stream=False, tool_names=["echo"])
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())

    # 第二轮 input 含 function_call_output
    items_2 = responses.calls[1]["input"]
    assert {"type": "function_call_output", "call_id": "c1",
            "output": json.dumps({"echo": "a"})} in items_2
    assert result.final_answer == "done"


async def test_responses_stream(collector, tracker):
    register_echo()
    chunks_1 = [
        SimpleNamespace(type="response.reasoning_text.delta", delta="想一想"),
        SimpleNamespace(type="response.output_text.delta", delta="早上好"),
        # 流式搜索状态事件：实测只有 item_id 无 item
        SimpleNamespace(type="response.web_search_call.in_progress", item_id="ws1"),
        SimpleNamespace(type="response.output_item.done",
                        item=SimpleNamespace(type="function_call", call_id="c1",
                                             name="echo", arguments='{"text": "b"}')),
        SimpleNamespace(type="response.completed",
                        response=SimpleNamespace(usage=usage_responses(inp=20, out=8))),
    ]
    chunks_2 = [
        SimpleNamespace(type="response.output_text.delta", delta="完毕"),
        SimpleNamespace(type="response.output_item.done",
                        item=SimpleNamespace(type="web_search_call", id="ws1",
                                             status="completed",
                                             action=SimpleNamespace(type="search", queries=["q1"]))),
        SimpleNamespace(type="response.completed",
                        response=SimpleNamespace(usage=usage_responses(inp=30, out=6))),
    ]
    responses = FakeEndpoint([chunks_1, chunks_2])
    loop = make_loop(collector, FakeClient(responses=responses),
                     api_style="responses", stream=True, tool_names=["echo"])
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())

    assert responses.calls[0]["stream"] is True
    assert result.final_answer == "完毕"

    types = collector.types()
    assert EventType.THINKING_DELTA in types
    assert EventType.TEXT_DELTA in types

    # 流式搜索事件：状态类（仅 item_id）+ done 类（完整 query）
    ws_events = collector.of_type(EventType.WEB_SEARCH)
    assert any(e.data == {"query": "", "status": "in_progress", "id": "ws1"} for e in ws_events)
    assert any(e.data["query"] == "q1" and e.data["status"] == "completed" for e in ws_events)

    # 工具执行闭环（流式分片收集后一次性执行）
    tr = collector.of_type(EventType.TOOL_RESULT)[0]
    assert json.loads(tr.data["result"]) == {"echo": "b"}

    # usage 累计（两轮流式 completed 事件）
    assert result.total_usage["prompt_tokens"] == 50
    assert result.total_usage["context_prompt_tokens"] == 30


async def test_responses_failed_stream_raises_error_event(collector, tracker):
    chunks = [
        SimpleNamespace(type="response.failed",
                        response=SimpleNamespace(error=SimpleNamespace(message="boom"))),
    ]
    responses = FakeEndpoint([chunks])
    loop = make_loop(collector, FakeClient(responses=responses),
                     api_style="responses", stream=True)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())
    err = collector.of_type(EventType.ERROR)
    assert len(err) == 1
    assert "boom" in err[0].data["error"]
    assert result.final_answer == ""


# ==================== 主循环 ====================


async def test_chat_tool_args_invalid_json(collector, tracker):
    register_echo()
    chat = FakeEndpoint([
        SimpleNamespace(choices=[SimpleNamespace(message=tool_call_msg("c2", "echo", '{"text": '))],
                        usage=usage_ds()),
        text_response("兜底", usage=usage_ds()),
    ])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False, tool_names=["echo"])
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())

    tr = collector.of_type(EventType.TOOL_RESULT)[0]
    assert "参数解析失败" in tr.data["result"]
    assert result.final_answer == "兜底"


async def test_chat_max_iterations(collector, tracker):
    register_echo()
    chat = FakeEndpoint([SimpleNamespace(
        choices=[SimpleNamespace(message=tool_call_msg(f"c3-{index}", "echo", "{}"))], usage=usage_ds(),
    ) for index in range(3)])
    loop = make_loop(collector, FakeClient(chat=chat), stream=False,
                     tool_names=["echo"], max_iterations=3)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())

    assert result.final_answer == "已达到最大迭代次数，停止执行。"
    assert len(chat.calls) == 3
    assert collector.of_type(EventType.FINAL_ANSWER)[-1].data["content"].startswith("已达到最大迭代次数")


async def test_unexpected_exception_becomes_error_event(collector, tracker):
    class ExplodingEndpoint:
        async def create(self, **params):
            raise RuntimeError("网络炸了")

    chat = ExplodingEndpoint()
    loop = make_loop(collector, FakeClient(chat=chat), stream=False)
    result = await loop.run([{"role": "user", "content": "hi"}], make_context())

    err = collector.of_type(EventType.ERROR)
    assert len(err) == 1
    assert "网络炸了" in err[0].data["error"]
    assert result.final_answer == ""


@pytest.mark.parametrize("api_style", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_empty_response_does_not_poison_next_request(collector, tracker, api_style, stream):
    if api_style == "chat":
        empty = [] if stream else text_response(None)
        valid = [delta_chunk(content="recovered")] if stream else text_response("recovered")
    else:
        empty = [] if stream else SimpleNamespace(output=[], usage=None)
        valid = ([SimpleNamespace(type="response.output_text.delta", delta="recovered")] if stream else
                 SimpleNamespace(output=[SimpleNamespace(type="message", content=[
                     SimpleNamespace(type="output_text", text="recovered"),
                 ])], usage=None))
    endpoint = FakeEndpoint([empty, valid])
    loop = make_loop(collector, FakeClient(chat=endpoint, responses=endpoint), api_style=api_style, stream=stream)
    messages = [{"role": "user", "content": "synthetic question"}]
    result = await loop.run(messages, make_context())
    assert result.stop_reason == "error" and "空响应" in result.error
    assert result.full_messages == [{"role": "user", "content": "synthetic question"}]
    result = await loop.run(result.full_messages, make_context())
    assert result.final_answer == "recovered" and result.stop_reason == "complete"


async def test_tool_result_event_failure_keeps_real_result_and_continues(collector, tracker):
    executed = AsyncMock(return_value="saved exactly once")
    registry.register("synthetic_save", "synthetic", {}, executed)
    endpoint = FakeEndpoint([
        SimpleNamespace(choices=[SimpleNamespace(message=tool_call_msg("save", "synthetic_save", "{}"))], usage=None),
        text_response("done"),
    ])
    loop = make_loop(collector, FakeClient(chat=endpoint), stream=False, tool_names=["synthetic_save"])

    async def callback(event):
        if event.type in (EventType.TOOL_CALL_START, EventType.TOOL_RESULT):
            if event.type == EventType.TOOL_RESULT:
                assert messages[-1]["content"] == "saved exactly once"
            raise OSError("synthetic display failure")
        await collector(event)

    loop.callback = callback
    messages = [{"role": "user", "content": "synthetic question"}]
    result = await loop.run(messages, make_context())
    assert result.stop_reason == "complete"
    executed.assert_awaited_once()
    assert endpoint.calls[1]["messages"][-1]["content"] == "saved exactly once"


@pytest.mark.parametrize("failure", ["executor", "checkpoint"])
async def test_batch_failure_closes_calls_without_replaying_or_requesting_model(collector, tracker, failure):
    message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[
        {"id": call_id, "type": "function", "function": {"name": "synthetic_save", "arguments": "{}"}}
        for call_id in ("a", "b")
    ])
    endpoint = FakeEndpoint([SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)])
    loop = make_loop(collector, FakeClient(chat=endpoint), stream=False)
    loop.tool_executor = SimpleNamespace(execute=AsyncMock(
        side_effect=OSError("synthetic execution failure") if failure == "executor" else None,
        return_value="confirmed saved",
    ))
    loop.checkpoint = AsyncMock(side_effect=OSError("synthetic save failure") if failure == "checkpoint" else None)
    result = await loop.run([{"role": "user", "content": "synthetic question"}], make_context())
    assert result.stop_reason == "error" and len(endpoint.calls) == 1
    assert loop.checkpoint.await_count == 1
    tools = [msg for msg in result.full_messages if msg.get("role") == "tool"]
    assert [msg["tool_call_id"] for msg in tools] == ["a", "b"]
    if failure == "executor":
        loop.tool_executor.execute.assert_awaited_once()
        assert [json.loads(msg["content"])["status"] for msg in tools] == ["unknown", "not_executed"]
    else:
        assert loop.tool_executor.execute.await_count == 2
        assert [msg["content"] for msg in tools] == ["confirmed saved", "confirmed saved"]


async def test_preflight_repairs_old_nulls_and_missing_result_without_running_tool(collector, tracker):
    endpoint = FakeEndpoint([text_response("recovered")])
    loop = make_loop(collector, FakeClient(chat=endpoint), stream=False)
    loop.tool_executor = SimpleNamespace(execute=AsyncMock())
    messages = [None, {"role": "assistant", "content": None},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "old", "type": "function", "function": {"name": "synthetic_save", "arguments": "{}"},
                }]}, {"role": "user", "content": "continue"}]
    result = await loop.run(messages, make_context())
    assert result.stop_reason == "complete"
    sent = endpoint.calls[0]["messages"]
    assert [msg["role"] for msg in sent] == ["assistant", "tool", "user"]
    assert sent[1]["tool_call_id"] == "old" and json.loads(sent[1]["content"])["status"] == "unknown"
    loop.tool_executor.execute.assert_not_awaited()


async def test_unrepairable_history_is_reported_before_api_request(collector, tracker):
    endpoint = FakeEndpoint([])
    loop = make_loop(collector, FakeClient(chat=endpoint), stream=False)
    messages = [{"role": "user", "content": {"text": "invalid content"}}]
    result = await loop.run(messages, make_context())
    assert result.stop_reason == "error" and "历史结构错误" in result.error
    assert endpoint.calls == []
    assert messages == [{"role": "user", "content": {"text": "invalid content"}}]


async def test_metadata_only_history_is_preserved_but_not_sent_as_null_chat_message(collector, tracker):
    endpoint = FakeEndpoint([text_response("recovered")])
    loop = make_loop(collector, FakeClient(chat=endpoint), stream=False)
    old = {"role": "assistant", "content": None, "reasoning_content": "synthetic reasoning",
           "future_provider_state": {"signature": "synthetic signature"}}
    messages = [old, {"role": "user", "content": "continue"}]
    result = await loop.run(messages, make_context())
    assert result.stop_reason == "complete"
    assert result.full_messages[0] == old
    assert endpoint.calls[0]["messages"] == [{"role": "user", "content": "continue"}]
