"""Synthetic handover tests: no model, private files, real tools or application startup."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openai.types.chat import ChatCompletionMessage

from agent.loop import AgentLoop
from agent.session import AgentSession
from agent.protocol import Context, Event, EventType, ModelConfig
from config.manager import ConfigManager


def response(message):
    return SimpleNamespace(message=message, usage=None, web_search_calls=[])


def tool_batch():
    msg = ChatCompletionMessage(role='assistant', content=None, tool_calls=[
        {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}
        for call_id, name in [('a', 'save_memory'), ('b', 'update_goal')]
    ])
    object.__setattr__(msg, 'gemini_content', {'role': 'model', 'parts': [
        {'functionCall': {'id': call_id, 'name': name, 'args': {}}, 'thoughtSignature': 'synthetic'}
        for call_id, name in [('a', 'save_memory'), ('b', 'update_goal')]
    ]})
    return msg


def make_loop(call):
    loop = AgentLoop.__new__(AgentLoop)
    loop.model_config = ModelConfig('synthetic', 'https://example.invalid', api_style='gemini_generate',
                                   auth='adc', project='synthetic', service_tier='priority', stream=True)
    loop.client = SimpleNamespace(context_cache=None)
    loop.callback = AsyncMock()
    loop.dialect = SimpleNamespace(call=AsyncMock(side_effect=call))
    loop.tool_executor = None
    loop.tool_definitions = []
    loop.checkpoint = None
    return loop


def make_session(loop, monkeypatch):
    session = AgentSession.__new__(AgentSession)
    session._lock = asyncio.Lock()
    session._pending_user_rounds = 0
    session._heartbeat_interrupt = None
    session._loop = loop
    session.callback = loop.callback
    session.platform = 'web'
    session._system_prompt = 'synthetic system prompt'
    active = SimpleNamespace(session_id='synthetic', start_time=1, history=[])
    session.session_manager = SimpleNamespace(
        get_or_create_active_session=lambda: active, maybe_compress_daily=AsyncMock(),
        record_input_time=Mock(), context_tokens=0, save_session=Mock(), add_usage=AsyncMock(return_value=None),
    )
    session.chronicle = SimpleNamespace(append_messages=Mock())
    session._clear_rollback_state = Mock()
    session._save_rollback_state = Mock()
    monkeypatch.setattr('agent.session.FileManager', lambda: SimpleNamespace(configured=False))
    return session, active


async def test_waiting_user_gets_entire_tool_batch_and_original_signatures(monkeypatch):
    started, finish_write = asyncio.Event(), asyncio.Event()
    calls, executed = [], []

    async def call(**kwargs):
        calls.append((copy.deepcopy(kwargs['full_messages']), kwargs['cfg'], kwargs['context'].source))
        if kwargs['context'].source == 'heartbeat':
            return response(tool_batch())
        return response(ChatCompletionMessage(role='assistant', content='answer user'))

    async def execute(name, args):
        executed.append(name)
        if name == 'save_memory':
            started.set()
            await finish_write.wait()
        return '{"status":"ok"}'

    loop = make_loop(call)
    loop.tool_executor = SimpleNamespace(execute=execute)
    session, active = make_session(loop, monkeypatch)
    heartbeat = asyncio.create_task(session.run_round(source="heartbeat", injection_messages=[{'role': 'user', 'content': 'synthetic heartbeat'}], trigger_time=1))
    await asyncio.wait_for(started.wait(), 2)
    user = asyncio.create_task(session.run_round('synthetic question'))
    await asyncio.sleep(0)
    assert session._heartbeat_interrupt.is_set()
    assert not user.done() and len(calls) == 1
    finish_write.set()
    beat_result, user_result = await asyncio.wait_for(asyncio.gather(heartbeat, user), 2)
    assert beat_result.stop_reason == 'yielded' and user_result.stop_reason == 'complete'
    assert executed == ['save_memory', 'update_goal']
    assert len(calls) == 2
    messages, cfg, _ = calls[1]
    assistant = next(m for m in messages if m.get('tool_calls'))
    index = messages.index(assistant)
    assert [m['tool_call_id'] for m in messages[index + 1:index + 3]] == ['a', 'b']
    assert assistant['gemini_content']['parts'][0]['thoughtSignature'] == 'synthetic'
    assert 'synthetic question' in messages[-1]['content']
    assert calls[0][1].service_tier == 'flex' and cfg.service_tier == 'priority'
    assert loop.model_config.service_tier == 'priority'
    assert active.history[-1]['content'] == 'answer user'
    assert session._pending_user_rounds == 0 and session._heartbeat_interrupt is None


async def test_waiting_model_is_cancelled_without_saving_partial_output(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def call(**kwargs):
        if kwargs['context'].source != 'heartbeat':
            return response(ChatCompletionMessage(role='assistant', content='user answer'))
        await kwargs['emit'](Event(EventType.TEXT_DELTA, {'content': 'unfinished synthetic text'}, kwargs['context']))
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    loop = make_loop(call)
    session, active = make_session(loop, monkeypatch)
    heartbeat = asyncio.create_task(session.run_round(source="heartbeat", injection_messages=[{'role': 'user', 'content': 'synthetic beat'}], trigger_time=1))
    await asyncio.wait_for(started.wait(), 2)
    user = asyncio.create_task(session.run_round('new question'))
    result, _ = await asyncio.wait_for(asyncio.gather(heartbeat, user), 2)
    assert result.stop_reason == 'yielded' and cancelled.is_set()
    assert not any('unfinished synthetic text' in str(m) for m in active.history)
    assert any('心跳因用户输入暂停' in m.get('content', '') for m in active.history)
    assert any(args.args[0].type == EventType.NOTICE for args in loop.callback.await_args_list)


async def test_heartbeat_budget_includes_model_wait_but_does_not_cancel_tool_batch():
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def call(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(tool_batch())
        await asyncio.Event().wait()

    async def execute(name, args):
        started.set()
        await finish.wait()
        return 'saved'

    loop = make_loop(call)
    loop.model_config.heartbeat_timeout_seconds = .01
    loop.tool_executor = SimpleNamespace(execute=execute)
    context = Context('heartbeat', [], '', 'web', source='heartbeat', interrupt_event=asyncio.Event())
    task = asyncio.create_task(loop.run([{'role': 'user', 'content': 'synthetic beat'}], context))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.sleep(.03)
    assert not task.done()  # The API budget is not a tool execution timeout.
    finish.set()
    result = await asyncio.wait_for(task, 2)
    assert result.stop_reason == 'error' and '等待预算' in result.error
    assert [m['tool_call_id'] for m in result.full_messages if m.get('role') == 'tool'] == ['a', 'b']
    assert calls == 2


async def test_complete_response_wins_simultaneous_user_arrival():
    event = asyncio.Event()

    async def call(**kwargs):
        event.set()
        return response(tool_batch())

    loop = make_loop(call)
    loop.tool_executor = SimpleNamespace(execute=AsyncMock(return_value='ok'))
    result = await loop.run([], Context('heartbeat', [], '', 'web', source='heartbeat', interrupt_event=event))
    assert result.stop_reason == 'yielded'
    assert loop.tool_executor.execute.await_count == 2
    assert [m['tool_call_id'] for m in result.full_messages if m.get('role') == 'tool'] == ['a', 'b']


async def test_cancelled_waiting_user_does_not_leave_pending_count(monkeypatch):
    loop = make_loop(AsyncMock())
    session, _ = make_session(loop, monkeypatch)
    await session._lock.acquire()
    user = asyncio.create_task(session.run_round('synthetic question'))
    await asyncio.sleep(0)
    assert session._pending_user_rounds == 1
    user.cancel()
    with pytest.raises(asyncio.CancelledError):
        await user
    session._lock.release()
    assert session._pending_user_rounds == 0


async def test_parent_shutdown_cleans_up_model_wait_and_interrupt_waiter():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def call(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    loop = make_loop(call)
    event = asyncio.Event()
    task = asyncio.create_task(loop.run([], Context('heartbeat', [], '', 'web', source='heartbeat', interrupt_event=event)))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert not event._waiters


def test_config_switch_is_independent_of_heartbeat_flex(monkeypatch):
    monkeypatch.setattr(ConfigManager, '_instance', None)
    manager = ConfigManager()
    manager._raw_config = {
        'providers': {'vertex': {'type': 'google_vertex', 'project': 'synthetic'}},
        'models': {'gemini': {'provider': 'vertex', 'name': 'gemini-synthetic', 'api_style': 'gemini_generate',
                              'defaults': {'service_tier': 'priority'}}},
        'roles': {'chat': {'model': 'gemini', 'explicit_cache': {'enabled': False}}},
        'heartbeat': {'service_tier': 'flex', 'model_timeout_seconds': 45, 'max_retries': 1},
    }
    cfg = manager.get_model('chat')
    assert cfg.explicit_cache == {'enabled': False}
    assert cfg.service_tier == 'priority' and cfg.heartbeat_service_tier == 'flex'
    assert cfg.heartbeat_timeout_seconds == 45 and cfg.heartbeat_max_retries == 1
    manager._raw_config['heartbeat'].pop('model_timeout_seconds')
    assert manager.get_model('chat').heartbeat_timeout_seconds == 300
    assert ModelConfig('synthetic', 'https://example.invalid').heartbeat_timeout_seconds == 300
    manager._raw_config['models']['gemini']['defaults']['service_tier'] = 'flex'
    assert manager.get_model('chat').service_tier == 'flex'
    manager._raw_config['heartbeat']['service_tier'] = 'unknown'
    with pytest.raises(ValueError, match='heartbeat.service_tier'):
        manager.get_model('chat')
