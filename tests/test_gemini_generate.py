"""Native Cloud Gemini generateContent dialect tests."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from dialects import get_dialect, message_to_dict
from dialects.gemini import GeminiClient, _check_response
from agent.protocol import Context, EventType, ModelConfig


@pytest.fixture
def cfg():
    return ModelConfig(
        'gemini-3.8-flash', 'https://aiplatform.googleapis.com/v1beta1', '',
        api_style='gemini_generate', auth='adc', project='cloud-project',
        provider='google', vision=True, stream=False, service_tier='priority',
    )


@pytest.fixture
def context():
    return Context('', [], '', 'test', image_resolver=AsyncMock(
        return_value='data:image/png;base64,aGVsbG8='))


def test_429_error_keeps_safe_google_diagnostics():
    response = httpx.Response(429, headers={
        'retry-after': '2', 'x-goog-request-id': 'request-123',
    }, json={'error': {
        'code': 429,
        'status': 'RESOURCE_EXHAUSTED',
        'message': 'Quota exceeded; echoed private input must not appear in logs',
        'details': [
            {'@type': 'type.googleapis.com/google.rpc.ErrorInfo',
             'reason': 'RATE_LIMIT_EXCEEDED'},
            {'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
             'violations': [{'subject': 'projects/private-project'}]},
        ],
    }}, request=httpx.Request('POST', 'https://example.test'))

    with pytest.raises(RuntimeError) as caught:
        _check_response(response)
    message = str(caught.value)
    assert 'code=RESOURCE_EXHAUSTED' in message
    assert 'reason=quota_exhausted' in message
    assert 'provider_reason=RATE_LIMIT_EXCEEDED' in message
    assert 'quota_failure=true' in message
    assert 'retry_after=2' in message
    assert 'request_id=request-123' in message
    assert 'private input' not in message


@pytest.mark.asyncio
async def test_429_is_retried(cfg, context, monkeypatch):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={'retry-after': '0.1'}, json={
                'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED',
                          'message': 'Resource exhausted, please try again later.'}})
        return httpx.Response(200, json={
            'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ok'}]}}]})

    sleep = AsyncMock()
    monkeypatch.setattr('dialects.gemini_generate.asyncio.sleep', sleep)
    async with httpx.AsyncClient(
            base_url='https://example.test/', transport=httpx.MockTransport(handler)) as client:
        result = await get_dialect('gemini_generate').call(
            client, cfg, [], None, context, AsyncMock())

    assert result.message.content == 'ok'
    assert attempts == 3
    assert sleep.await_count == 2


@pytest.mark.parametrize('stream', [False, True])
async def test_flex_headers_for_both_generation_methods(cfg, context, stream):
    cfg.service_tier = 'flex'
    cfg.stream = stream

    def handler(request):
        assert request.headers['X-Vertex-AI-LLM-Request-Type'] == 'shared'
        assert request.headers['X-Vertex-AI-LLM-Shared-Request-Type'] == 'flex'
        payload = {'modelVersion': 'gemini-server-version',
                   'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'synthetic ok'}]}}]}
        if stream:
            return httpx.Response(200, content='data: ' + json.dumps(payload) + '\n\n')
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(base_url='https://example.test/', transport=httpx.MockTransport(handler)) as client:
        result = await get_dialect('gemini_generate').call(client, cfg, [], None, context, AsyncMock())
    assert result.message.content == 'synthetic ok'
    assert result.response_model == 'gemini-server-version'


async def test_flex_retries_never_change_to_priority(cfg, context, monkeypatch):
    cfg.service_tier = 'flex'
    cfg.max_retries = 1
    tiers = []

    def handler(request):
        tiers.append(request.headers['X-Vertex-AI-LLM-Shared-Request-Type'])
        return httpx.Response(429, json={'error': {'status': 'RESOURCE_EXHAUSTED'}})

    monkeypatch.setattr('dialects.gemini_generate.asyncio.sleep', AsyncMock())
    async with httpx.AsyncClient(base_url='https://example.test/', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await get_dialect('gemini_generate').call(client, cfg, [], None, context, AsyncMock())
    assert tiers == ['flex', 'flex']


async def test_flex_rejects_regional_endpoint_without_sending(cfg, context):
    cfg.service_tier = 'flex'
    cfg.location = 'us-central1'
    async with httpx.AsyncClient(base_url='https://example.test/') as client:
        with pytest.raises(ValueError, match='global'):
            await get_dialect('gemini_generate').call(client, cfg, [], None, context, AsyncMock())


@pytest.mark.asyncio
async def test_request_preserves_native_content_and_maps_tools(cfg, context):
    cfg.tool_names = ['web_search']
    cfg.response_format = {'type': 'json_schema', 'json_schema': {'schema': {'type': 'object'}}}
    raw = {'role': 'model', 'parts': [
        {'text': 'thinking', 'thought': True, 'thoughtSignature': 'signed'},
        {'functionCall': {'id': 'call-1', 'name': 'add', 'args': {'a': 1}}},
    ]}
    messages = [
        {'role': 'system', 'content': 'Be concise'},
        {'role': 'user', 'content': [{'type': 'image', 'local_id': 'image'}]},
        {'role': 'assistant', 'content': None, 'gemini_content': raw,
         'tool_calls': [{'id': 'call-1', 'function': {'name': 'add', 'arguments': '{"a":1}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': '2'},
    ]
    tools = [{'type': 'function', 'function': {
        'name': 'add', 'description': 'Add', 'parameters': {'type': 'object'}}}]
    body = await get_dialect('gemini_generate')._request(cfg, messages, tools, context)
    assert body['systemInstruction']['parts'][0]['text'] == 'Be concise'
    assert body['contents'][0]['parts'][0] == {
        'inlineData': {'mimeType': 'image/png', 'data': 'aGVsbG8='}}
    assert body['contents'][1] == {
        'role': 'model',
        'parts': [
            {'thoughtSignature': 'signed'},
            {'functionCall': {'id': 'call-1', 'name': 'add', 'args': {'a': 1}}},
        ],
    }
    assert body['contents'][2]['parts'][0]['functionResponse'] == {
        'id': 'call-1', 'name': 'add', 'response': {'output': '2'}}
    assert body['tools'] == [
        {'functionDeclarations': [tools[0]['function']]}, {'googleSearch': {}}]
    assert body['generationConfig']['responseJsonSchema'] == {'type': 'object'}
    assert body['generationConfig']['thinkingConfig']['thinkingLevel'] == 'HIGH'

    gcs = await get_dialect('gemini_generate')._image(
        {'type': 'image', 'file_id': 'gs://private-bucket/images/test.png',
         'mime': 'image/png'}, cfg, Context(
             '', [], '', 'test', image_file_resolver=AsyncMock(
                 return_value='gs://private-bucket/images/test.png')))
    assert gcs == {'fileData': {
        'mimeType': 'image/png', 'fileUri': 'gs://private-bucket/images/test.png'}}
    context.image_resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_nonstream_response_usage_and_persistence(cfg, context):
    response_content = {'role': 'model', 'parts': [
        {'text': 'Think', 'thought': True, 'thoughtSignature': 'signed'},
        {'functionCall': {'id': 'call-1', 'name': 'add', 'args': {'a': 17}}},
    ]}

    def handler(request):
        assert request.headers['X-Vertex-AI-LLM-Shared-Request-Type'] == 'priority'
        assert request.url.path.endswith(
            '/projects/cloud-project/locations/global/publishers/google/models/'
            'gemini-3.8-flash:generateContent')
        return httpx.Response(200, json={
            'candidates': [{'content': response_content}],
            'usageMetadata': {'promptTokenCount': 5, 'candidatesTokenCount': 2,
                              'thoughtsTokenCount': 3, 'totalTokenCount': 10,
                              'cachedContentTokenCount': 1},
        })

    class Credentials:
        valid = True

        def apply(self, headers):
            headers['authorization'] = 'Bearer token'

    async with GeminiClient(
        base_url=cfg.base_url, auth_mode='adc', project=cfg.project,
        credentials=Credentials(), transport=httpx.MockTransport(handler),
    ) as client:
        result = await get_dialect('gemini_generate').call(
            client, cfg, [{'role': 'user', 'content': 'add'}], None, context, AsyncMock())
    assert result.message.reasoning_content == 'Think'
    assert result.message.tool_calls[0].function.arguments == '{"a": 17}'
    assert result.usage.completion_tokens == 5
    assert result.usage.reasoning_tokens == 3
    assert message_to_dict(result.message)['gemini_content'] == {
        'role': 'model',
        'parts': [
            {'thoughtSignature': 'signed'},
            {'functionCall': {'id': 'call-1', 'name': 'add', 'args': {'a': 17}}},
        ],
    }


@pytest.mark.asyncio
async def test_stream_text_thought_and_usage(cfg, context):
    cfg.stream = True
    chunks = [
        {'candidates': [{'content': {'role': 'model', 'parts': [
            {'text': 'Th', 'thought': True}]}}]},
        {'candidates': [{'content': {'role': 'model', 'parts': [
            {'text': 'ink', 'thought': True}]}}]},
        {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'Do'}]}}]},
        {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ne'}]}}]},
        {'candidates': [{'content': {'role': 'model', 'parts': [
            {'text': '', 'thoughtSignature': 'signed'}]}}],
         'usageMetadata': {'promptTokenCount': 3, 'candidatesTokenCount': 1,
                           'thoughtsTokenCount': 2, 'totalTokenCount': 6}},
    ]
    wire = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)

    class Credentials:
        valid = True

        def apply(self, headers):
            headers['authorization'] = 'Bearer token'

    async with GeminiClient(
        base_url=cfg.base_url, auth_mode='adc', project=cfg.project,
        credentials=Credentials(), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=wire)),
    ) as client:
        emit = AsyncMock()
        result = await get_dialect('gemini_generate').call(
            client, cfg, [{'role': 'user', 'content': 'Hi'}], None, context, emit)
    assert result.message.content == 'Done'
    assert result.message.reasoning_content == 'Think'
    assert result.usage.total_tokens == 6
    assert message_to_dict(result.message)['gemini_content'] == {
        'role': 'model',
        'parts': [{'text': 'Done', 'thoughtSignature': 'signed'}],
    }
    assert [(c.args[0].type, c.args[0].data['content']) for c in emit.call_args_list] == [
        (EventType.THINKING_DELTA, 'Th'),
        (EventType.THINKING_DELTA, 'ink'),
        (EventType.TEXT_DELTA, 'Do'),
        (EventType.TEXT_DELTA, 'ne'),
    ]
