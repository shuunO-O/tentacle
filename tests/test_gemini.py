"""Gemini schema/stream/history regression tests, using an in-memory HTTP transport."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from dialects import get_dialect, message_to_dict
from dialects.gemini import GeminiClient
from agent.protocol import Context, EventType, ModelConfig


@pytest.fixture
def cfg():
    return ModelConfig('gemini-test', 'https://gemini.test/v1beta', 'test-key',
                       api_style='interactions', provider='google', vision=True, stream=False)


@pytest.fixture
def context():
    return Context('', [], '', 'test', image_resolver=AsyncMock(return_value='data:image/png;base64,aGVsbG8='))


@pytest.mark.asyncio
async def test_request_images_tools_json_and_history(cfg, context):
    dialect = get_dialect('interactions')
    cfg.tool_names = ['web_search']
    cfg.response_format = {'type': 'json_schema', 'json_schema': {'schema': {'type': 'object'}}}
    saved = [{'type': 'thought', 'signature': 'signed'},
             {'type': 'function_call', 'id': 'fc1', 'name': 'add', 'arguments': {'a': 1}}]
    messages = [
        {'role': 'system', 'content': 'Be concise'},
        {'role': 'user', 'content': [{'type': 'image', 'local_id': 'local', 'file_id': 'deepseek-id'},
                                     {'type': 'image_url', 'image_url': {'url': 'https://example.com/test.jpg'}}]},
        {'role': 'assistant', 'content': None, 'gemini_steps': saved,
         'tool_calls': [{'id': 'fc1', 'function': {'name': 'add', 'arguments': '{"a":1}'}}]},
        {'role': 'tool', 'tool_call_id': 'fc1', 'content': '2'},
    ]
    body = await dialect._request(cfg, messages, None, context)
    assert body['store'] is False
    assert body['system_instruction'] == 'Be concise'
    assert body['tools'] == [{'type': 'google_search', 'search_types': ['web_search']}]
    assert body['response_format'] == {'type': 'text', 'mime_type': 'application/json', 'schema': {'type': 'object'}}
    assert body['input'][0]['content'] == [
        {'type': 'image', 'data': 'aGVsbG8=', 'mime_type': 'image/png'},
        {'type': 'image', 'uri': 'https://example.com/test.jpg', 'mime_type': 'image/jpeg'}]
    assert body['input'][1:3] == saved
    assert body['input'][3] == {'type': 'function_result', 'call_id': 'fc1', 'name': 'add', 'result': '2'}
    assert saved[0]['signature'] == 'signed'
    cfg.vision = False
    assert await dialect._image({'type': 'image', 'file_id': 'anything'}, cfg, context) == {'type': 'text', 'text': '[图片]'}


@pytest.mark.asyncio
async def test_unary_usage_and_signed_roundtrip(cfg, context):
    steps = [{'type': 'thought', 'signature': 'secret-signature', 'summary': [{'type': 'text', 'text': 'Thinking'}]},
             {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Done'}]}]
    def handler(request):
        assert request.url.path == '/v1beta/interactions'
        assert request.headers['x-goog-api-key'] == 'test-key'
        assert request.headers['Api-Revision'] == '2026-05-20'
        return httpx.Response(200, json={'model': 'gemini-server-version', 'status': 'completed', 'steps': steps, 'usage': {
            'total_input_tokens': 10, 'total_output_tokens': 4, 'total_thought_tokens': 6,
            'total_tokens': 20, 'total_cached_tokens': 3}})
    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        result = await get_dialect('interactions').call(client, cfg, [{'role': 'user', 'content': 'Hi'}], None, context, AsyncMock())
    assert result.message.content == 'Done'
    assert result.response_model == 'gemini-server-version'
    assert result.message.reasoning_content == 'Thinking'
    assert result.usage.completion_tokens == 10
    assert result.usage.cached_tokens == 3
    assert result.usage.reasoning_tokens == 6
    # JSON serialization simulates persistence in active_session.json.
    assert json.loads(json.dumps(message_to_dict(result.message)))['gemini_steps'] == [
        {'type': 'thought', 'signature': 'secret-signature'},
        {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Done'}]},
    ]


@pytest.mark.asyncio
async def test_cloud_adc_auth_and_resource_path():
    class Credentials:
        valid = True

        def apply(self, headers):
            headers['authorization'] = 'Bearer adc-token'
            headers['x-goog-user-project'] = 'billing-project'

    def handler(request):
        assert request.url.path == '/v1beta1/projects/cloud-project/locations/global/interactions'
        assert request.headers['authorization'] == 'Bearer adc-token'
        assert request.headers['x-goog-user-project'] == 'billing-project'
        assert 'x-goog-api-key' not in request.headers
        return httpx.Response(200, json={'status': 'completed', 'steps': []})

    async with GeminiClient(
        base_url='https://aiplatform.googleapis.com/v1beta1', auth_mode='adc',
        project='cloud-project', location='global', credentials=Credentials(),
        transport=httpx.MockTransport(handler),
    ) as client:
        response = await client.post('interactions', json={})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_stream_initial_content_arguments_signature_and_usage(cfg, context):
    cfg.stream = True
    events = [
        {'event_type': 'interaction.created', 'interaction': {'status': 'in_progress', 'model': 'gemini-server-version'}},
        {'event_type': 'step.start', 'index': 0, 'step': {'type': 'thought'}},
        {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'thought_summary', 'content': {'type': 'text', 'text': 'Think'}}},
        {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'thought_signature', 'signature': 'signed'}},
        {'event_type': 'step.start', 'index': 1, 'step': {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Let'}]}},
        {'event_type': 'step.delta', 'index': 1, 'delta': {'type': 'text', 'text': ' me'}},
        {'event_type': 'step.delta', 'index': 1, 'delta': {'type': 'text_annotation_delta', 'annotations': [{'type': 'url_citation', 'url': 'https://example.com'}]}},
        {'event_type': 'step.start', 'index': 2, 'step': {'type': 'function_call', 'id': 'fc1', 'name': 'add'}},
        {'event_type': 'step.delta', 'index': 2, 'delta': {'type': 'arguments_delta', 'arguments': '{"a":'}},
        {'event_type': 'step.delta', 'index': 2, 'delta': {'type': 'arguments_delta', 'arguments': '17}'}},
        {'event_type': 'interaction.requires_action', 'interaction': {'usage': {'total_input_tokens': 5, 'total_output_tokens': 3, 'total_tokens': 8}}},
    ]
    wire = ''.join('data: ' + json.dumps(e) + '\r\n\r\n' for e in events)
    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=wire, headers={'Content-Type': 'text/event-stream'})
    )) as client:
        emit = AsyncMock()
        result = await get_dialect('interactions').call(client, cfg, [], None, context, emit)
    assert result.message.content == 'Let me'
    assert result.message.reasoning_content == 'Think'
    assert result.response_model == 'gemini-server-version'
    assert result.message.tool_calls[0].function.arguments == '{"a": 17}'
    saved = message_to_dict(result.message)['gemini_steps']
    assert saved[0] == {'type': 'thought', 'signature': 'signed'}
    assert saved[2]['arguments'] == {'a': 17}
    assert result.usage.total_tokens == 8
    assert [(c.args[0].type, c.args[0].data['content']) for c in emit.call_args_list] == [
        (EventType.THINKING_DELTA, 'Think'), (EventType.TEXT_DELTA, 'Let'), (EventType.TEXT_DELTA, ' me')]


@pytest.mark.asyncio
async def test_truncated_stream_is_error(cfg, context):
    cfg.stream = True
    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text='data: {"event_type":"interaction.created","interaction":{"status":"in_progress"}}\n\n')
    )) as client:
        with pytest.raises(RuntimeError, match='Gemini API error'):
            await get_dialect('interactions').call(client, cfg, [], None, context, AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize('stateful', [False, True])
async def test_agent_loop_tool_execution(cfg, context, monkeypatch, tmp_path, stateful):
    import agent.loop as agent_loop
    from runtime.usage import UsageTracker
    from runtime.tool_registry import registry

    registry.register('gemini_test_add', 'Add', {'type': 'object'}, lambda args: str(args['a'] + 1), False)
    cfg.tool_names = ['gemini_test_add']
    cfg.interactions_stateful = stateful
    monkeypatch.setattr(UsageTracker, "_instance", None)
    tracker = UsageTracker(str(tmp_path / "usage.db"))
    monkeypatch.setattr(agent_loop, 'get_usage_tracker', lambda: tracker)
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            steps = [{'type': 'thought', 'signature': 'round-trip'},
                     {'type': 'function_call', 'name': 'gemini_test_add', 'id': 'fc', 'arguments': {'a': 41}}]
            status = 'requires_action'
        else:
            assert body['input'][-1] == {'type': 'function_result', 'call_id': 'fc', 'name': 'gemini_test_add', 'result': '42'}
            if stateful:
                assert body['previous_interaction_id'] == 'turn-1'
                assert len(body['input']) == 1
            else:
                assert body['input'][-3]['signature'] == 'round-trip'
            steps = [{'type': 'model_output', 'content': [{'type': 'text', 'text': '42'}]}]
            status = 'completed'
        return httpx.Response(200, json={'steps': steps, 'status': status, 'id': f'turn-{len(requests)}',
                                        'usage': {'total_input_tokens': 10, 'total_output_tokens': 2, 'total_tokens': 15,
                                                  'total_thought_tokens': 3, 'total_cached_tokens': 6}})
    async with GeminiClient('test-key', cfg.base_url, transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(agent_loop, 'GeminiClient', lambda **kw: client)
        callback = AsyncMock()
        loop = agent_loop.AgentLoop(cfg, callback)
        result = await loop.run([{'role': 'user', 'content': 'Add 41 and 1'}], context)
    assert result.final_answer == '42'
    assert result.total_usage['total_tokens'] == 30
    assert result.total_usage['cached_tokens'] == 12
    assert result.total_usage['reasoning_tokens'] == 6
    assert [msg['role'] for msg in result.full_messages] == ['user', 'assistant', 'tool', 'assistant']
    usage_events = [c.args[0] for c in callback.call_args_list if c.args[0].type == EventType.USAGE]
    assert len(usage_events) == 2
    assert all(e.data['cached_tokens'] == 6 for e in usage_events)
    summary = (await tracker.query_usage())['summary']['today']
    assert summary['requests'] == 2
    assert summary['cached_tokens'] == 12
    assert summary['completion_tokens'] == 10
    assert summary['reasoning_tokens'] == 6
    assert summary['cache_hit_rate'] == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [None, "", "http://windows.test:7897"])
async def test_google_resumable_upload_delete(monkeypatch, proxy):
    from attachments.manager import GeminiFilesUploader
    from datetime import datetime

    calls = []
    uri = 'https://generativelanguage.googleapis.com/v1beta/files/test'
    class Response:
        status = 200
        headers = {'X-Goog-Upload-URL': 'https://upload.test/session'}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def raise_for_status(self):
            pass
        async def json(self):
            return {'file': {'uri': uri, 'expirationTime': '2026-09-18T00:00:00Z'}}
    class Session(Response):
        def __init__(self, **kw):
            assert kw['headers']['x-goog-api-key'] == 'test-key'
            assert kw['trust_env'] is (proxy is None)
        def post(self, url, **kw):
            calls.append(('POST', url, kw))
            return Response()
        def delete(self, url, **kw):
            calls.append(('DELETE', url, kw))
            return Response()
    monkeypatch.setattr('attachments.manager.aiohttp.ClientSession', Session)
    uploader = GeminiFilesUploader('https://generativelanguage.googleapis.com/v1beta', 'test-key', 30, proxy=proxy)
    file_id, expires = await uploader.upload(b'png-data', 'red.png', 'image/png')
    assert file_id == uri
    assert expires == datetime.fromisoformat('2026-09-18T00:00:00+00:00').timestamp()
    assert calls[0][1].endswith('/upload/v1beta/files')
    assert calls[0][2]['headers']['X-Goog-Upload-Header-Content-Length'] == '8'
    assert calls[1][2]['data'] == b'png-data'
    await uploader.delete(uri)
    assert calls[-1][:2] == ('DELETE', uri)
    await uploader.delete('deepseek-id')
    assert len(calls) == 3
    assert all(kw['proxy'] == (proxy or None) for _, _, kw in calls)


@pytest.mark.asyncio
async def test_google_file_uri_falls_back_for_other_dialects(context):
    from dialects.openai_chat import OpenAIChatDialect
    from dialects.responses import ResponsesDialect

    block = {'type': 'image', 'local_id': 'local',
             'file_id': 'https://generativelanguage.googleapis.com/v1beta/files/test'}
    chat = await OpenAIChatDialect._image_to_chat_block(block, context.image_resolver, True)
    responses = await ResponsesDialect._image_to_input_image(block, context.image_resolver)
    assert chat['image_url']['url'].startswith('data:image/png;base64,')
    assert responses['image_url'].startswith('data:image/png;base64,')


@pytest.mark.asyncio
async def test_stream_search_replay_preserves_signatures_and_adds_search_type(cfg, context):
    cfg.stream = True
    cfg.tool_names = ['web_search']
    events = [
        {'event_type': 'step.start', 'index': 0,
         'step': {'type': 'google_search_call', 'id': 'search1', 'signature': ''}},
        {'event_type': 'step.delta', 'index': 0,
         'delta': {'type': 'google_search_call', 'arguments': {'queries': ['Eiffel Tower']}, 'signature': 'call-signed'}},
        {'event_type': 'step.stop', 'index': 0},
        {'event_type': 'step.start', 'index': 1,
         'step': {'type': 'google_search_result', 'call_id': 'search1', 'signature': ''}},
        {'event_type': 'step.delta', 'index': 1,
         'delta': {'type': 'google_search_result', 'result': [{'search_suggestions': '<div>Search</div>'}],
                   'signature': 'result-signed', 'is_error': False}},
        {'event_type': 'step.stop', 'index': 1},
        {'event_type': 'step.start', 'index': 2, 'step': {'type': 'thought'}},
        {'event_type': 'step.delta', 'index': 2,
         'delta': {'type': 'thought_signature', 'signature': 'thought-signed'}},
        {'event_type': 'step.start', 'index': 3, 'step': {'type': 'model_output'}},
        {'event_type': 'step.delta', 'index': 3, 'delta': {'type': 'text', 'text': 'Paris'}},
        {'event_type': 'interaction.completed', 'interaction': {'status': 'completed'}},
    ]
    wire = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
    async with GeminiClient('test', cfg.base_url, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=wire)
    )) as client:
        dialect = get_dialect('interactions')
        emit = AsyncMock()
        result = await dialect.call(client, cfg, [], None, context, emit)
    saved = json.loads(json.dumps(message_to_dict(result.message)))
    steps = saved['gemini_steps']
    assert steps[0]['search_type'] == 'web_search'
    assert [s['signature'] for s in steps[:3]] == ['call-signed', 'result-signed', 'thought-signed']
    assert steps[1]['result'] == [{'search_suggestions': '<div>Search</div>'}]
    assert result.web_search_calls[0]['action']['queries'] == ['Eiffel Tower']
    assert any(c.args[0].type == EventType.WEB_SEARCH for c in emit.call_args_list)
    # Old persisted histories omit search_type. Repair only the outbound copy.
    del steps[0]['search_type']
    body = await dialect._request(cfg, [saved, {'role': 'user', 'content': 'Where?'}], None, context)
    assert body['input'][0]['search_type'] == 'web_search'
    assert 'search_type' not in steps[0]
    assert body['input'][1:3] == steps[1:3]
    assert body['tools'] == [{'type': 'google_search', 'search_types': ['web_search']}]
    # Never overwrite a known type, including future image-search history.
    steps[0]['search_type'] = 'image_search'
    body = await dialect._request(cfg, [saved], None, context)
    assert body['input'][0]['search_type'] == 'image_search'


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('sse', [False, True])
@pytest.mark.asyncio
async def test_http_error_reports_safe_reason_without_echoing_inputs(cfg, context, stream, sse):
    cfg.stream = stream
    error = {'error': {'code': 'invalid_request', 'message':
             'contents[1].parts[0]: Tool type of tool_call part does not match with tool call context. '
             'PRIVATE_CONVERSATION secret-api-key'}}
    body = 'data: ' + json.dumps(error) + '\n\n' if sse else json.dumps(error)
    async with GeminiClient('secret-api-key', cfg.base_url, transport=httpx.MockTransport(
        lambda request: httpx.Response(400, text=body)
    )) as client:
        with pytest.raises(RuntimeError) as exc:
            await get_dialect('interactions').call(client, cfg, [], None, context, AsyncMock())
    message = str(exc.value)
    assert 'HTTP 400' in message and 'tool_context_mismatch' in message
    assert 'PRIVATE_CONVERSATION' not in message and 'secret-api-key' not in message
