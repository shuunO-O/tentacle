from types import SimpleNamespace
from unittest.mock import AsyncMock

from dialects.base import message_to_dict
from dialects.responses import ResponsesDialect
from agent.protocol import Context, ModelConfig


async def test_encrypted_reasoning_survives_serialization_and_request_replay():
    dialect = ResponsesDialect()
    cfg = ModelConfig('gpt-test', 'https://example.test', provider='openai', api_style='responses')
    output = [SimpleNamespace(type='reasoning', id='r1', encrypted_content='synthetic-ciphertext', content=[]),
              SimpleNamespace(type='function_call', call_id='c1', name='echo', arguments='{}')]
    msg, _, _ = dialect._parse_response_output(SimpleNamespace(output=output))
    saved = message_to_dict(msg)
    request = await dialect._to_responses_input(
        [saved, {'role': 'tool', 'tool_call_id': 'c1', 'content': 'done'}], cfg, Context('', [], '', 'test'))
    assert request[0]['encrypted_content'] == 'synthetic-ciphertext'
    assert request[1]['call_id'] == request[2]['call_id'] == 'c1'


async def test_streamed_reasoning_is_retained_even_without_visible_thoughts():
    dialect = ResponsesDialect()
    async def stream():
        yield SimpleNamespace(type='response.output_item.done', item=SimpleNamespace(
            type='reasoning', id='r1', encrypted_content='synthetic-ciphertext', summary=[]))
        yield SimpleNamespace(type='response.output_text.delta', delta='Answer')
        yield SimpleNamespace(type='response.completed', response=SimpleNamespace(usage=None))
    result = await dialect._process_responses_stream(stream(), Context('', [], '', 'test'), AsyncMock())
    saved = message_to_dict(result.message)
    assert saved['responses_output'][0]['encrypted_content'] == 'synthetic-ciphertext'
    assert saved['responses_output'][1]['content'][0]['text'] == 'Answer'


async def test_responses_images_resolve_only_target_vendor_ids():
    dialect = ResponsesDialect()
    cfg = ModelConfig('deepseek-test', 'https://ds.test', provider='deepseek', vision=True)
    context = Context('', [], '', 'test', image_file_resolver=AsyncMock(return_value='file-target'),
                      image_resolver=AsyncMock(return_value='data:image/png;base64,synthetic'))
    block = {'type': 'image', 'local_id': 'local-1', 'file_id': 'gs://wrong'}
    assert await dialect._convert_content_blocks([block], cfg, context) == [
        {'type': 'input_image', 'file_id': 'file-target'}]
    cfg.provider = 'openai'
    assert await dialect._convert_content_blocks([block], cfg, context) == [
        {'type': 'input_image', 'image_url': 'data:image/png;base64,synthetic'}]
    assert context.image_file_resolver.await_count == 1


async def test_declared_openai_connection_requests_stateless_reasoning():
    dialect = ResponsesDialect()
    cfg = ModelConfig('gpt-test', 'https://example.test', provider='openai_compatible',
                      provider_type='openai', api_style='responses', stream=False)
    create = AsyncMock(return_value=SimpleNamespace(output=[], usage=None))
    await dialect.call(SimpleNamespace(responses=SimpleNamespace(create=create)), cfg,
                       [{'role': 'user', 'content': 'Synthetic'}], None,
                       Context('', [], '', 'test'), AsyncMock())
    assert create.call_args.kwargs['store'] is False
    assert create.call_args.kwargs['include'] == ['reasoning.encrypted_content']


async def test_chat_preserves_an_observed_empty_reasoning_field():
    from dialects.openai_chat import OpenAIChatDialect
    async def stream():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content='Answer', tool_calls=None, reasoning_content=''))], usage=None)
    result = await OpenAIChatDialect()._process_stream(stream(), Context('', [], '', 'test'), AsyncMock())
    assert message_to_dict(result.message)['reasoning_content'] == ''


async def test_both_gemini_protocols_mark_missing_images_explicitly():
    from dialects.gemini import GeminiInteractionsDialect
    from dialects.gemini_generate import GeminiGenerateContentDialect
    cfg = ModelConfig('gemini-test', 'https://example.test', provider='google', vision=True)
    context = Context('', [], '', 'test', image_resolver=AsyncMock(return_value=None))
    for dialect in (GeminiInteractionsDialect(), GeminiGenerateContentDialect()):
        part = await dialect._image({'type': 'image', 'local_id': 'missing'}, cfg, context)
        assert part['text'] == '[图片不可用]'
