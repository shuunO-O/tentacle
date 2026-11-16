"""Synthetic Interactions trace import; no real Google requests or private data."""

from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from dialects.base import message_to_dict
from dialects.gemini_generate import GeminiGenerateContentDialect
from agent.protocol import Context, ModelConfig
from agent.handoff import interactions_to_generate, model_identity


def configs():
    source = ModelConfig('gemini-test', 'https://model.test', api_key='synthetic',
                         provider='google', api_style='interactions', vision=True, stream=False)
    return source, replace(source, api_style='gemini_generate')


def trace():
    return [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Question'},
                                   {'type': 'image', 'local_id': 'image-1', 'file_id': 'old-reference'}]},
        {'role': 'assistant', 'content': 'Checking', 'tool_calls': [
            {'id': call, 'type': 'function', 'function': {'name': 'lookup', 'arguments': json.dumps({'n': n})}}
            for n, call in enumerate(('a', 'b'))
        ], 'gemini_steps': [
            {'type': 'thought', 'signature': 'interactions-ciphertext', 'summary': []},
            {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Checking'}]},
            *[{'type': 'function_call', 'id': call, 'name': 'lookup', 'arguments': {'n': n}}
              for n, call in enumerate(('a', 'b'))],
        ], 'gemini_interaction': {'id': 'old-chain'}},
        {'role': 'tool', 'tool_call_id': 'a', 'content': '{"value":1}'},
        {'role': 'tool', 'tool_call_id': 'b', 'content': '{"value":2}'},
        {'role': 'assistant', 'content': 'Answer', 'gemini_steps': [
            {'type': 'thought', 'signature': 'another-ciphertext'},
            {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Answer'}]},
        ]},
    ]


def convert(history=None):
    source, target = configs()
    return interactions_to_generate(history if history is not None else trace(),
                                    model_identity(source), model_identity(target))


def test_supported_trace_preserves_tools_and_results_without_reusing_ciphertext():
    original = trace()
    saved = deepcopy(original)
    converted = convert(original)
    assert original == saved
    assert converted[2:4] == original[2:4]
    assert converted[1]['tool_calls'] == original[1]['tool_calls']
    parts = converted[1]['gemini_content']['parts']
    assert parts[0] == {'text': 'Checking'}
    assert parts[1]['thoughtSignature'] == 'skip_thought_signature_validator'
    assert 'thoughtSignature' not in parts[2]
    assert converted[0]['content'][1] == {'type': 'image', 'local_id': 'image-1'}
    for value in ('interactions-ciphertext', 'another-ciphertext', 'old-chain', 'gemini_steps', 'old-reference'):
        assert value not in json.dumps(converted)


@pytest.mark.parametrize('change', [
    {'name': 'gemini-other'}, {'base_url': 'https://other.test'}, {'api_key': 'different-account'},
    {'provider_id': 'different-connection'}, {'provider': 'openai'},
])
def test_import_only_applies_to_same_model_and_connection(change):
    source, target = configs()
    assert interactions_to_generate(trace(), model_identity(source), model_identity(replace(target, **change))) is None
    assert interactions_to_generate(trace(), None, model_identity(target)) is None
    assert interactions_to_generate(trace(), model_identity(target), model_identity(source)) is None


@pytest.mark.parametrize('problem', ['search', 'unknown', 'missing_result', 'orphan', 'mismatch', 'multimodal_result'])
def test_unsupported_history_falls_back_as_a_whole(problem):
    history = trace()
    if problem in ('search', 'unknown'):
        history[1]['gemini_steps'].insert(0, {'type': 'google_search_call' if problem == 'search' else 'future_step'})
    elif problem == 'missing_result':
        history.pop(2)
    elif problem == 'orphan':
        history[2]['tool_call_id'] = 'unknown-call'
    elif problem == 'mismatch':
        history[1]['tool_calls'][0]['function']['arguments'] = '{"n":999}'
    else:
        history[2]['content'] = [{'type': 'image', 'local_id': 'screenshot'}]
    assert convert(history) is None


async def test_wire_import_and_future_native_signatures_are_kept_separate():
    _, cfg = configs()
    converted = convert()
    context = Context('', [], '', 'test', image_resolver=AsyncMock(return_value='data:image/png;base64,eA=='))
    dialect = GeminiGenerateContentDialect()
    body = await dialect._request(cfg, converted, None, context)
    assert body['contents'][1]['parts'][1]['functionCall']['args'] == {'n': 0}
    assert [part['functionResponse']['name'] for part in body['contents'][2]['parts']] == ['lookup', 'lookup']
    assert body['contents'][0]['parts'][1]['inlineData']['data'] == 'eA=='

    def handler(request):
        wire = json.loads(request.content)
        assert wire['contents'][1]['parts'][1]['thoughtSignature'] == 'skip_thought_signature_validator'
        assert 'interactions-ciphertext' not in request.content.decode()
        return httpx.Response(200, json={'candidates': [{'content': {'role': 'model', 'parts': [
            {'functionCall': {'id': 'native', 'name': 'lookup', 'args': {'n': 3}},
             'thoughtSignature': 'fresh-generate-signature'}]}}]})
    async with httpx.AsyncClient(base_url='https://model.test/', transport=httpx.MockTransport(handler)) as client:
        result = await dialect.call(client, cfg, converted + [{'role': 'user', 'content': 'Next'}],
                                    None, context, AsyncMock())
    continued = converted + [{'role': 'user', 'content': 'Next'}, message_to_dict(result.message),
                             {'role': 'tool', 'tool_call_id': 'native', 'content': 'done'}]
    body = await dialect._request(cfg, continued, None, context)
    assert body['contents'][-2]['parts'][0]['thoughtSignature'] == 'fresh-generate-signature'
