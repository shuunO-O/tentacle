"""Interactions chaining must never replace or silently diverge from local history."""

import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.compressor import Compressor
from dialects import get_dialect, message_to_dict
from dialects.gemini import GeminiAPIError, GeminiClient
from agent.protocol import Context, ModelConfig
from config.manager import ConfigManager
from agent.session_manager import SessionManager


@pytest.fixture
def cfg():
    return ModelConfig('gemini-test', 'https://gemini.test/v1beta1', 'test-key',
                       api_style='interactions', provider='google', vision=True,
                       stream=False, interactions_stateful=True, service_tier='priority')


@pytest.fixture
def context():
    return Context('', [], '', 'test', session_id='synthetic-session')


def reply(identifier='turn-1', *, tool=False):
    steps = [{'type': 'thought', 'signature': 'synthetic-signature'}]
    if tool:
        steps.append({'type': 'function_call', 'id': 'call-1', 'name': 'add', 'arguments': {'a': 1}})
    else:
        steps.append({'type': 'model_output', 'content': [{'type': 'text', 'text': 'Synthetic answer'}]})
    return {'id': identifier, 'status': 'requires_action' if tool else 'completed', 'steps': steps}


async def seed(cfg, context, *, tool=False):
    history = [{'role': 'system', 'content': 'Synthetic instructions'},
               {'role': 'user', 'content': 'Synthetic question'}]
    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=reply(tool=tool))
    )) as client:
        result = await get_dialect('interactions').call(client, cfg, history, None, context, AsyncMock())
    history.append(message_to_dict(result.message))
    return history


@pytest.mark.asyncio
async def test_full_history_and_pointer_survive_actual_session_restart(cfg, context, tmp_path, monkeypatch):
    monkeypatch.setattr(Compressor, '_load_system_prompt', lambda self: 'Synthetic prompt')
    manager_config = SimpleNamespace(_raw_config={'models': {'chat': {}}})
    options = {'db_path': str(tmp_path / 'chronicle.db'), 'cache_path': str(tmp_path / 'active_session.json'),
               'compression_state_path': str(tmp_path / 'compression.json')}
    manager = SessionManager(manager_config, **options)
    session = manager.get_or_create_active_session()
    context.session_id = session.session_id
    history = await seed(cfg, context)
    session.history = history[1:]
    manager.save_session()
    manager.chronicle.append_messages(session.session_id, [
        {'role': 'user', 'content': 'Synthetic question'},
        {'role': 'assistant', 'content': 'Synthetic answer'},
    ])
    SessionManager._instance = None
    restarted = SessionManager(manager_config, **options)
    assert restarted.current_session.history == history[1:]
    assert restarted.current_session.history[-1]['gemini_steps'][0]['signature'] == 'synthetic-signature'
    restored = [history[0], *restarted.current_session.history, {'role': 'user', 'content': 'Continue'}]
    original = copy.deepcopy(restored)

    def handler(request):
        body = json.loads(request.content)
        assert body['previous_interaction_id'] == 'turn-1'
        assert body['store'] is True
        assert body['input'] == [{'type': 'user_input', 'content': [{'type': 'text', 'text': 'Continue'}]}]
        assert body['system_instruction'] == 'Synthetic instructions'
        assert body['service_tier'] == 'priority'
        assert body['generation_config']['thinking_summaries'] == 'auto'
        return httpx.Response(200, json=reply('turn-2'))

    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        result = await get_dialect('interactions').call(client, cfg, restored, None, context, AsyncMock())
    assert restored == original
    assert message_to_dict(result.message)['gemini_interaction']['id'] == 'turn-2'


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_tool_batch_chains_and_resends_declarations(cfg, context, stream):
    tools = [{'type': 'function', 'function': {'name': 'add', 'description': 'Add', 'parameters': {'type': 'object'}}}]
    history = [{'role': 'system', 'content': 'Use tools'}, {'role': 'user', 'content': 'Add'}]
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert 'previous_interaction_id' not in body
            value = reply(tool=True)
        else:
            assert body['previous_interaction_id'] == 'turn-1'
            assert body['input'] == [{'type': 'function_result', 'call_id': 'call-1', 'name': 'add', 'result': '2'}]
            assert body['tools'] == requests[0]['tools']
            value = reply('turn-2')
        if not stream:
            return httpx.Response(200, json=value)
        events = [{'event_type': 'interaction.created', 'interaction': {'id': value['id']}}]
        events.extend({'event_type': 'step.start', 'index': i, 'step': step} for i, step in enumerate(value['steps']))
        events.append({'event_type': 'interaction.' + value['status'], 'interaction': {}})
        return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events))

    cfg.stream = stream
    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        dialect = get_dialect('interactions')
        first = await dialect.call(client, cfg, history, tools, context, AsyncMock())
        history.append(message_to_dict(first.message))
        history.append({'role': 'tool', 'tool_call_id': 'call-1', 'content': '2'})
        second = await dialect.call(client, cfg, history, tools, context, AsyncMock())
    assert second.message.content == 'Synthetic answer'
    assert len(history) == 4  # No server-side chaining may truncate local context.
    assert history[2]['tool_calls'][0]['id'] == 'call-1'
    assert history[3]['content'] == '2'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['history', 'signature', 'system', 'model', 'endpoint', 'session',
                                        'tools', 'expiry', 'disabled', 'metadata'])
async def test_changed_or_expired_prefix_is_sent_in_full(cfg, context, change):
    history = await seed(cfg, context)
    old = copy.deepcopy(history)
    if change == 'history':
        history[1]['content'] = 'Edited question'
    elif change == 'signature':
        history[2]['gemini_steps'][0]['signature'] = 'Changed signature'
    elif change == 'system':
        history[0]['content'] = 'Changed instructions'
    elif change == 'model':
        cfg.name = 'another-model'
    elif change == 'endpoint':
        cfg.base_url = 'https://other.test/v1beta1'
    elif change == 'session':
        context.session_id = 'another-session'
    elif change == 'tools':
        cfg.tool_names = ['web_search']
    elif change == 'expiry':
        history[2]['gemini_interaction']['chain_started_at'] = time.time() - 86400
    elif change == 'disabled':
        cfg.interactions_stateful = False
    else:
        history[2]['gemini_interaction'] = 'Malformed metadata'
    history.append({'role': 'user', 'content': 'Next'})

    def handler(request):
        body = json.loads(request.content)
        assert 'previous_interaction_id' not in body
        assert body['input'][0]['type'] == 'user_input'
        assert body['input'][1]['type'] == 'thought'
        assert body['input'][-1]['content'][0]['text'] == 'Next'
        assert len(body['input']) == 4
        return httpx.Response(200, json=reply('fresh-chain'))

    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        result = await get_dialect('interactions').call(client, cfg, history, None, context, AsyncMock())
    if change == 'disabled':
        assert 'gemini_interaction' not in message_to_dict(result.message)
    assert old[1]['content'] == 'Synthetic question'


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status', [400, 404, 410, 403, 429])
async def test_missing_chain_falls_back_once_without_hiding_other_errors(cfg, context, stream, status):
    history = await seed(cfg, context)
    history.append({'role': 'user', 'content': 'Next'})
    original = copy.deepcopy(history)
    cfg.stream = stream
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body['previous_interaction_id'] == 'turn-1'
            text = 'Previous interaction has expired PRIVATE_TEXT'
            return httpx.Response(status, json={'error': {'message': text}})
        assert 'previous_interaction_id' not in body
        assert body['input'][1]['signature'] == 'synthetic-signature'
        assert len(body['input']) == 4
        # Full-history rebuild can also fail; it must not recurse or clear history.
        return httpx.Response(404, json={'error': {'message': 'PRIVATE_TEXT'}})

    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GeminiAPIError) as exc:
            await get_dialect('interactions').call(client, cfg, history, None, context, AsyncMock())
    assert len(requests) == (2 if status in (400, 404, 410) else 1)
    assert 'PRIVATE_TEXT' not in str(exc.value)
    assert history == original


@pytest.mark.asyncio
async def test_rollback_uses_surviving_pointer_and_expiry_does_not_slide(cfg, context):
    history = await seed(cfg, context)
    checkpoint = copy.deepcopy(history)
    root_started = history[-1]['gemini_interaction']['chain_started_at']
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply('turn-' + str(len(requests) + 1)))

    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        dialect = get_dialect('interactions')
        history.append({'role': 'user', 'content': 'Discard this turn'})
        second = await dialect.call(client, cfg, history, None, context, AsyncMock())
        history.append(message_to_dict(second.message))
        assert history[-1]['gemini_interaction']['chain_started_at'] == root_started
        # Restore the local rollback checkpoint. No client-level "last ID" may leak.
        checkpoint.append({'role': 'user', 'content': 'Different continuation'})
        await dialect.call(client, cfg, checkpoint, None, context, AsyncMock())
    assert [r['previous_interaction_id'] for r in requests] == ['turn-1', 'turn-1']
    assert requests[-1]['input'][0]['content'][0]['text'] == 'Different continuation'


@pytest.mark.asyncio
async def test_partial_stream_never_restarts_and_duplicates_output(cfg, context):
    history = await seed(cfg, context)
    history.append({'role': 'user', 'content': 'Next'})
    cfg.stream = True
    requests = []
    events = [
        {'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
        {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': 'Partial'}},
        {'event_type': 'error', 'error': {'message': 'Previous interaction not found'}},
    ]

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events))

    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        emit = AsyncMock()
        with pytest.raises(GeminiAPIError):
            await get_dialect('interactions').call(client, cfg, history, None, context, emit)
    assert len(requests) == 1
    assert emit.call_count == 1


@pytest.mark.asyncio
async def test_cloud_images_use_gcs_and_do_not_resolve_prior_images_on_chaining(cfg, context):
    cfg.auth = 'adc'
    cfg.project = 'synthetic-project'
    context.image_file_resolver = AsyncMock(return_value='gs://synthetic/image.png')
    dialect = get_dialect('interactions')
    assert await dialect._image({'type': 'image', 'local_id': 'local'}, cfg, context) == {
        'type': 'image', 'uri': 'gs://synthetic/image.png', 'mime_type': 'image/png',
    }
    context.image_file_resolver.assert_awaited_with({'type': 'image', 'local_id': 'local'}, ('gcs',))
    history = [{'role': 'user', 'content': [{'type': 'image', 'local_id': 'local'}]}]
    requests = []

    def handler(request):
        assert request.headers['authorization'] == 'Bearer synthetic-token'
        assert request.url.path == '/v1beta1/projects/synthetic-project/locations/global/interactions'
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply(f'turn-{len(requests)}'))

    credentials = SimpleNamespace(valid=True, apply=lambda headers: headers.update(authorization='Bearer synthetic-token'))
    async with GeminiClient(base_url=cfg.base_url, auth_mode='adc', project=cfg.project,
                            credentials=credentials, transport=httpx.MockTransport(handler)) as client:
        first = await dialect.call(client, cfg, history, None, context, AsyncMock())
        assert requests[0]['input'][0]['content'][0]['uri'] == 'gs://synthetic/image.png'
        history.append(message_to_dict(first.message))
        context.image_file_resolver.reset_mock()
        history.append({'role': 'user', 'content': 'Next'})
        await dialect.call(client, cfg, history, None, context, AsyncMock())
    assert requests[-1]['previous_interaction_id'] == 'turn-1'
    context.image_file_resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_chain_successfully_rebuilds_with_new_root_and_keeps_history(cfg, context):
    history = await seed(cfg, context)
    history[-1]['gemini_interaction']['chain_started_at'] -= 3600
    previous_start = history[-1]['gemini_interaction']['chain_started_at']
    history.append({'role': 'user', 'content': 'Next'})
    original = copy.deepcopy(history)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(404, json={'error': {}})
        assert 'previous_interaction_id' not in body
        return httpx.Response(200, json=reply('fresh-root'))

    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        result = await get_dialect('interactions').call(client, cfg, history, None, context, AsyncMock())
    assert history == original
    pointer = message_to_dict(result.message)['gemini_interaction']
    assert pointer['id'] == 'fresh-root'
    assert pointer['chain_started_at'] > previous_start
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('stateful', [False, True])
@pytest.mark.parametrize('stream', [False, True])
async def test_cloud_deleted_id_generic_precondition_is_only_retried_with_pointer(cfg, context, stateful, stream):
    history = await seed(cfg, context)
    history.append({'role': 'user', 'content': 'Next'})
    cfg.interactions_stateful = stateful
    cfg.stream = stream
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={'error': {'message': 'Precondition check failed.'}})
        assert 'previous_interaction_id' not in requests[-1]
        assert len(requests[-1]['input']) == 4
        value = reply('new-root')
        if not stream:
            return httpx.Response(200, json=value)
        events = [
            {'event_type': 'interaction.created', 'interaction': {'id': value['id']}},
            {'event_type': 'step.start', 'index': 0, 'step': value['steps'][1]},
            {'event_type': 'interaction.completed', 'interaction': {}},
        ]
        return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events))

    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        if stateful:
            result = await get_dialect('interactions').call(client, cfg, history, None, context, AsyncMock())
            assert message_to_dict(result.message)['gemini_interaction']['id'] == 'new-root'
            assert len(requests) == 2
        else:
            with pytest.raises(GeminiAPIError, match='precondition_failed'):
                await get_dialect('interactions').call(client, cfg, history, None, context, AsyncMock())
            assert len(requests) == 1


@pytest.mark.asyncio
async def test_ui_search_records_do_not_break_chain(cfg, context):
    history = await seed(cfg, context)
    # AgentLoop inserts this display record before saving the canonical reply.
    history.insert(2, {'type': 'web_search_call', 'id': 'synthetic-search', 'status': 'completed'})
    history.append({'role': 'user', 'content': 'Next'})

    def handler(request):
        body = json.loads(request.content)
        assert body['previous_interaction_id'] == 'turn-1'
        assert len(body['input']) == 1
        return httpx.Response(200, json=reply('turn-2'))

    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        await get_dialect('interactions').call(client, cfg, history, None, context, AsyncMock())


def test_stateful_config_is_opt_in(monkeypatch):
    monkeypatch.setattr(ConfigManager, '_instance', None)
    manager = ConfigManager()
    manager._raw_config = {
        'providers': {'vertex': {'type': 'google_vertex', 'project': 'synthetic-project'}},
        'models': {'gemini': {'provider': 'vertex', 'name': 'gemini-synthetic', 'api_style': 'interactions'}},
        'roles': {'chat': {'model': 'gemini'}},
    }
    assert manager.get_model('chat').interactions_stateful is False
    manager._raw_config['roles']['chat']['interactions_stateful'] = True
    assert manager.get_model('chat').interactions_stateful is True


@pytest.mark.asyncio
async def test_heartbeat_uses_flex_without_changing_local_pointer_scope(cfg, context):
    from agent.loop import AgentLoop

    cfg.service_tier = 'standard'
    history = await seed(cfg, context)
    history.append({'role': 'user', 'content': 'Heartbeat'})
    cfg.auth = 'adc'
    context.source = 'heartbeat'
    loop = AgentLoop.__new__(AgentLoop)
    loop.model_config = cfg
    loop.callback = AsyncMock()
    loop.dialect = SimpleNamespace(call=AsyncMock(return_value='result'))
    loop.client = object()
    assert await loop._call_model(history, None, context) == 'result'
    sent = loop.dialect.call.call_args.kwargs['cfg']
    assert sent.service_tier == 'flex'
    assert sent.max_retries == 1
    assert cfg.service_tier == 'standard'
    # User/heartbeat service tiers may alternate while continuing the same chain.
    cfg.auth = 'api_key'
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply(f'turn-{len(requests) + 1}'))

    transport = httpx.MockTransport(handler)
    async with GeminiClient('test-key', cfg.base_url, transport=transport) as client:
        loop.client = client
        loop.dialect = get_dialect('interactions')
        result = await loop._call_model(history, None, context)
        history.append(message_to_dict(result.message))
        history.append({'role': 'user', 'content': 'Synthetic follow-up'})
        context.source = 'web'
        await loop._call_model(history, None, context)
    assert requests[0]['service_tier'] == 'flex'
    assert requests[0]['previous_interaction_id'] == 'turn-1'
    assert requests[1]['service_tier'] == 'standard'
    assert requests[1]['previous_interaction_id'] == 'turn-2'
