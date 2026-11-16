#!/usr/bin/env python3
"""Independent real API smoke test; no Tentacle runtime, database or history.

.venv/bin/python scripts/test_gemini.py
.venv/bin/python scripts/test_gemini.py --model gemini-3.8-flash --case all
"""

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import struct
import sys
import zlib

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dialects import get_dialect, message_to_dict
from dialects.gemini import GeminiClient
from attachments.manager import GeminiFilesUploader
from agent.protocol import Context, ModelConfig


def red_png():
    """Generate a 32x32 solid red PNG in memory using the standard library."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    pixels = (b'\x00' + b'\xff\x00\x00' * 32) * 32
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


async def main(args):
    if args.stateful and args.api_style != 'interactions':
        raise ValueError('--stateful 仅用于 --api-style interactions')
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    key = os.environ.get('GEMINI_API_KEY', '')
    project = args.project or os.environ.get('GOOGLE_CLOUD_PROJECT')
    location = args.location or os.environ.get('GOOGLE_CLOUD_LOCATION', 'global')
    if args.auth == 'api_key' and not key:
        raise RuntimeError('请设置 GEMINI_API_KEY')
    if args.auth == 'adc' and not project:
        raise RuntimeError('请设置 GOOGLE_CLOUD_PROJECT 或传入 --project')
    cfg = ModelConfig(name=args.model, base_url=args.base_url, api_key=key,
                      api_style=args.api_style, provider='google', vision=True,
                      reasoning_effort=args.thinking_level, auth=args.auth,
                      project=project, location=location, interactions_stateful=args.stateful)
    context = Context('', [], '', 'gemini-smoke')
    dialect = get_dialect(args.api_style)
    data_url = 'data:image/png;base64,' + base64.b64encode(red_png()).decode()

    async def resolve(block):
        return data_url
    context.image_resolver = resolve
    events = []

    async def emit(event):
        events.append(event)

    async with GeminiClient(key, args.base_url, proxy=args.proxy, auth_mode=args.auth,
                            project=project, location=location) as client:
        async def call(messages, tools=None):
            result = await dialect.call(client, cfg, messages, tools, context, emit)
            print('  text:', result.message.content or '(tool call)')
            print('  usage:', result.usage)
            if result.usage and result.usage.prompt_tokens:
                print(f'  cache hit rate: {result.usage.cached_tokens / result.usage.prompt_tokens:.1%}')
            assert result.usage and result.usage.total_tokens > 0, '缺少 usage'
            return result

        cases = ['text', 'stream', 'json', 'tools', 'image', 'search'] if args.case == 'all' else [args.case]
        for case in cases:
            print(f'[{case}]', flush=True)
            cfg.stream = case in ('stream', 'tools', 'image')
            cfg.response_format = None
            events.clear()
            if case in ('text', 'stream'):
                result = await call([{'role': 'user', 'content': 'Reply with exactly GEMINI_OK.'}])
                assert 'GEMINI_OK' in (result.message.content or '')
                if cfg.stream:
                    assert ''.join(e.data['content'] for e in events if e.type.value == 'text_delta') == result.message.content
            elif case == 'multiturn':
                cfg.stream = True
                history = [{'role': 'user', 'content':
                            'Solve this puzzle briefly: three boxes labeled apples, oranges, mixed are all mislabeled. '
                            'Which box should I draw one fruit from to identify every box?'}]
                for turn in range(3):
                    result = await call(history)
                    assert result.message.content
                    history.append(message_to_dict(result.message))
                    # Simulate a local JSON save/reload before continuing.
                    history = json.loads(json.dumps(history))
                    if args.stateful:
                        assert history[-1].get('gemini_interaction'), '缺少远端续接指针'
                    history.append({'role': 'user', 'content':
                                    'Explain the next deduction in one sentence.' if turn == 0 else 'Summarize in one sentence.'})
            elif case == 'search':
                cfg.stream = True
                cfg.tool_names = ['web_search']
                history = [{'role': 'user', 'content':
                            'Use Google Search to find the official Eiffel Tower website. Reply briefly with its URL.'}]
                result = await call(history)
                assert result.web_search_calls, '未调用 Google Search'
                history.append(message_to_dict(result.message))
                history.append({'role': 'user', 'content': 'Which city is it in? Reply with just the city.'})
                result = await call(history)
                assert 'paris' in (result.message.content or '').lower()
            elif case == 'cache':
                # Synthetic stable prefix exceeds the 4096-token caching threshold.
                prefix = '\n'.join(
                    f'Record {i:04d}: station alpha has temperature 21, humidity 45, status normal.'
                    for i in range(500)
                )
                hits = []
                for attempt in range(3):
                    cfg.stream = attempt != 0
                    result = await call([{'role': 'user', 'content': prefix + '\nReply with exactly CACHE_OK.'}])
                    assert 'CACHE_OK' in (result.message.content or '')
                    assert result.usage.prompt_tokens >= 4096, '前缀不足缓存门槛'
                    assert 0 <= result.usage.cached_tokens <= result.usage.prompt_tokens
                    hits.append(result.usage.cached_tokens)
                if not any(hits):
                    print('[cache] INCONCLUSIVE: usage 有效，但服务端本次未命中隐式缓存', flush=True)
                    continue
            elif case == 'json':
                cfg.response_format = {'type': 'json_object'}
                result = await call([{'role': 'user', 'content': 'Return JSON with ok set to true.'}])
                assert json.loads(result.message.content)['ok'] is True
            elif case == 'tools':
                tools = [{'type': 'function', 'function': {
                    'name': 'smoke_add', 'description': 'Add two integers. Use this to calculate sums.',
                    'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
                                   'required': ['a', 'b']}}}]
                history = [{'role': 'user', 'content': 'You must call smoke_add with a=17 and b=25, then report its result.'}]
                result = await call(history, tools)
                assert result.message.tool_calls, '未调用工具'
                history.append(message_to_dict(result.message))
                for tc in result.message.tool_calls:
                    assert tc.function.name == 'smoke_add'
                    values = json.loads(tc.function.arguments)
                    history.append({'role': 'tool', 'tool_call_id': tc.id, 'content': str(values['a'] + values['b'])})
                result = await call(history, tools)
                assert not result.message.tool_calls and '42' in (result.message.content or '')
            else:
                if case == 'upload' and args.auth != 'api_key':
                    raise RuntimeError('Files API 仅支持 Developer API；Cloud ADC 请使用 image 用例验证内联图片')
                uploader = GeminiFilesUploader(args.base_url, key, 0, proxy=args.proxy)
                uri = None
                try:
                    image = {'type': 'image', 'local_id': 'synthetic-red'}
                    if case == 'upload':
                        uri, _ = await uploader.upload(red_png(), 'tentacle-smoke-red.png', 'image/png')
                        image = {'type': 'image', 'file_id': uri, 'mime': 'image/png'}
                    result = await call([{'role': 'user', 'content': [
                        {'type': 'text', 'text': 'What is the dominant color? Reply with one English color word.'}, image]}])
                    assert 'red' in (result.message.content or '').lower()
                finally:
                    if uri:
                        await uploader.delete(uri)
            print(f'[{case}] PASS', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default=None, help='显式 HTTP 代理 URL；空字符串强制直连')
    parser.add_argument('--thinking-level', choices=['minimal', 'low', 'medium', 'high'], default='low')
    parser.add_argument('--model', default='gemini-3.8-flash')
    parser.add_argument('--auth', choices=['adc', 'api_key'], default='adc')
    parser.add_argument('--api-style', choices=['gemini_generate', 'interactions'],
                        default='gemini_generate')
    parser.add_argument('--stateful', action='store_true', help='Interactions 远端续接；本地 history 仍完整保留')
    parser.add_argument('--project', help='Google Cloud project ID（默认 GOOGLE_CLOUD_PROJECT）')
    parser.add_argument('--location', default=None, help='Google Cloud location（默认 GOOGLE_CLOUD_LOCATION 或 global）')
    parser.add_argument('--base-url', default='https://aiplatform.googleapis.com/v1beta1')
    parser.add_argument('--case', choices=['all', 'text', 'stream', 'json', 'tools', 'image', 'upload', 'cache', 'search', 'multiturn'], default='all')
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception as exc:
        # No request headers / keys are printed.
        print(f'FAIL: {type(exc).__name__}: {exc}', file=sys.stderr)
        if isinstance(exc, httpx.HTTPStatusError):
            detail = exc.response.text.replace(os.environ.get('GEMINI_API_KEY', '') or '\0', '[REDACTED]')
            print(detail[:2000], file=sys.stderr)
        sys.exit(1)
