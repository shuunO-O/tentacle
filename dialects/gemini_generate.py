"""Gemini Enterprise Agent Platform generateContent dialect.

Keeps Tentacle's canonical Chat Completions messages while using the native
Gemini Content/Part schema. Raw model content is retained on assistant messages
so thought signatures and function calls survive tool rounds and persistence.
"""

import copy
import asyncio
import json
import logging
import mimetypes
import random
import re
from urllib.parse import urlparse

from dialects.base import AdapterResult, DialectAdapter, NormalizedUsage
from dialects.gemini import GeminiAPIError, _check_response
from agent.protocol import Event, EventType

logger = logging.getLogger(__name__)


def _usage(raw):
    if not raw:
        return None
    prompt = raw.get('promptTokenCount', 0)
    completion = raw.get('candidatesTokenCount', 0)
    thought = raw.get('thoughtsTokenCount', 0)
    return NormalizedUsage(
        prompt_tokens=prompt,
        completion_tokens=completion + thought,
        total_tokens=raw.get('totalTokenCount', prompt + completion + thought),
        cached_tokens=raw.get('cachedContentTokenCount', 0),
        reasoning_tokens=thought,
    )


def _normalize_content(content):
    """Merge streamed text fragments and strip human-facing thought summaries."""
    if not isinstance(content, dict):
        return {}
    result = copy.deepcopy(content)
    if 'parts' not in result:
        return result
    parts = []
    for part in result.get('parts') or ():
        if not isinstance(part, dict):
            continue
        if not part.get('thoughtSignature'):
            part.pop('thoughtSignature', None)
        if part.get('thought'):
            cleaned = {k: v for k, v in part.items() if k not in ('text', 'thought')}
            if cleaned:
                parts.append(cleaned)
            continue
        if set(part.keys()) == {'text'} and not part.get('text'):
            continue
        if (parts and 'text' in part and set(part.keys()) <= {'text', 'thoughtSignature'}
                and 'text' in parts[-1] and set(parts[-1].keys()) == {'text'}):
            parts[-1]['text'] += part['text']
            if 'thoughtSignature' in part:
                parts[-1]['thoughtSignature'] = part['thoughtSignature']
            continue
        parts.append(part)
    result['parts'] = parts
    return result


class GeminiGenerateContentDialect(DialectAdapter):
    name = 'gemini_generate'

    async def _media(self, block, cfg, context):
        from dialects.gemini import GeminiInteractionsDialect
        part = await GeminiInteractionsDialect()._media(block, cfg, context)
        if part['type'] == 'text':
            return {'text': part['text']}
        return {'fileData': {'mimeType': part['mime_type'], 'fileUri': part['uri']}}

    async def _image(self, block, cfg, context):
        if not cfg.vision:
            return {'text': '[图片]'}
        file_id = ''
        resolver = getattr(context, 'image_file_resolver', None)
        if resolver:
            file_id = await resolver(block, ('gcs',)) or ''
        if file_id.startswith('gs://'):
            mime = block.get('mime') or mimetypes.guess_type(file_id)[0] or 'image/jpeg'
            return {'fileData': {'mimeType': mime, 'fileUri': file_id}}
        url = block.get('image_url') or block.get('uri')
        if isinstance(url, dict):
            url = url.get('url')
        if not url and context.image_resolver:
            url = await context.image_resolver(block)
        if not url:
            return {'text': '[图片不可用]'}
        if url.startswith('data:'):
            header, data = url.split(',', 1)
            return {'inlineData': {'mimeType': header[5:].split(';')[0], 'data': data}}
        mime = block.get('mime') or mimetypes.guess_type(urlparse(url).path)[0] or 'image/jpeg'
        return {'fileData': {'mimeType': mime, 'fileUri': url}}

    async def _content_parts(self, content, cfg, context):
        if isinstance(content, str):
            return [{'text': content}] if content else []
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind in ('text', 'input_text'):
                parts.append({'text': block.get('text', '')})
            elif kind in ('image', 'input_image', 'image_url'):
                parts.append(await self._image(block, cfg, context))
            elif kind in ('audio', 'video'):
                parts.append(await self._media(block, cfg, context))
        return parts

    async def _request(self, cfg, messages, tools, context):
        contents, system, call_names = [], [], {}
        for msg in messages:
            role = msg.get('role')
            if role in ('system', 'developer'):
                content = msg.get('content') or ''
                if isinstance(content, list):
                    content = ''.join(b.get('text', '') for b in content if isinstance(b, dict))
                system.append(content)
                continue
            if not role:
                continue
            for tc in msg.get('tool_calls') or []:
                call_names[tc.get('id')] = (tc.get('function') or {}).get('name', '')
            if role == 'assistant' and msg.get('gemini_content'):
                normalized = _normalize_content(msg['gemini_content'])
                if normalized.get('parts'):
                    contents.append(normalized)
                    continue
            if role == 'tool':
                call_id = msg.get('tool_call_id', '')
                name = call_names.get(call_id, '')
                output = msg.get('content') or ''
                response = {'id': call_id, 'name': name, 'response': {'output': output}}
                if isinstance(output, list):
                    parts = await self._content_parts(output, cfg, context)
                    # FunctionResponse accepts media only in its nested parts,
                    # never canonical local_id/file_id blocks inside response JSON.
                    response['response']['output'] = '\n'.join(p['text'] for p in parts if 'text' in p)
                    media = [p for p in parts if 'inlineData' in p or 'fileData' in p]
                    if media:
                        response['parts'] = media
                contents.append({'role': 'user', 'parts': [{'functionResponse': response}]})
                continue

            content = msg.get('content') or ''
            parts = await self._content_parts(content, cfg, context)
            for tc in msg.get('tool_calls') or []:
                func = tc.get('function') or {}
                parts.append({'functionCall': {
                    'id': tc.get('id', ''), 'name': func.get('name', ''),
                    'args': json.loads(func.get('arguments') or '{}'),
                }})
            if parts:
                contents.append({'role': 'model' if role == 'assistant' else 'user', 'parts': parts})

        merged = []
        for content in contents:
            if merged and merged[-1]['role'] == content['role']:
                merged[-1]['parts'].extend(content['parts'])
            else:
                merged.append(content)
        body = {'contents': merged}
        if system:
            body['systemInstruction'] = {'parts': [{'text': '\n\n'.join(system)}]}
        generation = {'thinkingConfig': {
            'includeThoughts': True,
            'thinkingLevel': cfg.reasoning_effort.upper(),
        }}
        if cfg.temperature is not None:
            generation['temperature'] = cfg.temperature
        if cfg.response_format:
            generation['responseMimeType'] = 'application/json'
            if cfg.response_format.get('type') == 'json_schema':
                generation['responseJsonSchema'] = cfg.response_format['json_schema']['schema']
        body['generationConfig'] = generation
        declarations = [t['function'] for t in tools or []]
        native_tools = []
        if declarations:
            native_tools.append({'functionDeclarations': declarations})
        if 'web_search' in (cfg.tool_names or []) and not any(
            t.get('name') == 'web_search' for t in declarations
        ):
            native_tools.append({'googleSearch': {}})
        if native_tools:
            body['tools'] = native_tools
        return body

    def _parse(self, payload):
        candidates = payload.get('candidates') or []
        raw_content = candidates[0].get('content', {}) if candidates else {}
        text, reasoning, calls = [], [], {}
        for part in raw_content.get('parts', []):
            value = part.get('text', '')
            if value:
                (reasoning if part.get('thought') else text).append(value)
            call = part.get('functionCall')
            if call:
                index = len(calls)
                calls[index] = {'id': call.get('id') or f'gemini_call_{index}', 'function': {
                    'name': call.get('name', ''),
                    'arguments': json.dumps(call.get('args', {}), ensure_ascii=False),
                }}
        message = self._build_message(''.join(text), ''.join(reasoning), calls)
        content = _normalize_content(raw_content)
        if content.get('parts'):
            object.__setattr__(message, 'gemini_content', content)
        searches = []
        metadata = candidates[0].get('groundingMetadata', {}) if candidates else {}
        queries = metadata.get('webSearchQueries') or []
        if queries:
            searches.append({'type': 'web_search_call', 'id': 'gemini_grounding',
                             'status': 'completed', 'action': {'type': 'search', 'queries': queries}})
        return AdapterResult(message, _usage(payload.get('usageMetadata')), searches,
                             response_model=payload.get('modelVersion', ''))

    async def call(self, client, cfg, full_messages, tools, context, emit):
        full_body = await self._request(cfg, full_messages, tools, context)
        cache = getattr(client, 'context_cache', None)
        body = full_body
        if cache is not None and context.source != 'heartbeat':
            body, _ = await cache.prepare(full_body, cfg, context.session_id)
        elif cache is not None:
            # Heartbeats send the full canonical history and never renew/create caches.
            # Also retire long-lived resources left by the previous always-on policy.
            await cache.clear_if_idle()
        path = f'publishers/google/models/{cfg.name}'
        for attempt in range(cfg.max_retries + 1):
            try:
                try:
                    result = await self._call_once(client, cfg, body, path, context, emit)
                    if cache is not None and 'cachedContent' in body:
                        await cache.record_use(body['cachedContent'], result.usage)
                    return result
                except GeminiAPIError as exc:
                    if (cache is None or 'cachedContent' not in body
                            or exc.status not in (400, 403, 404, 410)
                            or not getattr(exc, 'before_output', False)):
                        raise
                    # No stream events are emitted for an HTTP rejection. Retry once
                    # with the ORIGINAL complete prompt; never drop cached history.
                    await cache.clear()
                    cache.retry_at = cache.clock() + 300
                    body = full_body
                    return await self._call_once(client, cfg, body, path, context, emit)
            except GeminiAPIError as exc:
                if not exc.retryable or attempt >= cfg.max_retries:
                    raise
                delay = self._retry_delay(exc.retry_after, cfg.retry_base_seconds, attempt)
                logger.warning(
                    "Gemini 请求被限流或暂时不可用，%.1f 秒后重试 (%d/%d): %s",
                    delay, attempt + 1, cfg.max_retries, exc,
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _check_before_output(response):
        try:
            _check_response(response)
        except GeminiAPIError as exc:
            exc.before_output = True
            raise

    @staticmethod
    def _retry_delay(retry_after, base, attempt):
        if retry_after:
            match = re.fullmatch(r'(\d+(?:\.\d+)?)(?:s)?', retry_after)
            if match:
                return min(float(match.group(1)), 30.0)
        return min(base * (2 ** attempt) + random.uniform(0, base), 30.0)

    async def _call_once(self, client, cfg, body, path, context, emit):
        headers = None
        if cfg.service_tier == 'priority':
            headers = {'X-Vertex-AI-LLM-Shared-Request-Type': 'priority'}
        elif cfg.service_tier == 'flex':
            if cfg.auth != 'adc' or cfg.location != 'global':
                raise ValueError('Flex PayGo 需要 Cloud ADC 和 global 端点')
            headers = {'X-Vertex-AI-LLM-Request-Type': 'shared',
                       'X-Vertex-AI-LLM-Shared-Request-Type': 'flex'}
        if not cfg.stream:
            response = await client.post(path + ':generateContent', json=body, headers=headers)
            self._check_before_output(response)
            return self._parse(response.json())

        response_payload = {'candidates': [{'content': {'role': 'model', 'parts': []}}]}
        async with client.stream(
                'POST', path + ':streamGenerateContent?alt=sse', json=body,
                headers=headers) as response:
            if response.is_error:
                await response.aread()
            self._check_before_output(response)
            data = []
            async for line in response.aiter_lines():
                if line.startswith('data:'):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    chunk = json.loads('\n'.join(data))
                    data = []
                    await self._merge_chunk(response_payload, chunk, context, emit)
            if data:
                await self._merge_chunk(response_payload, json.loads('\n'.join(data)), context, emit)
        return self._parse(response_payload)

    async def _merge_chunk(self, target, chunk, context, emit):
        if chunk.get('modelVersion'):
            target['modelVersion'] = chunk['modelVersion']
        if chunk.get('usageMetadata'):
            target['usageMetadata'] = chunk['usageMetadata']
        candidates = chunk.get('candidates') or []
        if not candidates:
            return
        candidate = candidates[0]
        target_candidate = target['candidates'][0]
        if candidate.get('groundingMetadata'):
            target_candidate['groundingMetadata'] = candidate['groundingMetadata']
        for part in candidate.get('content', {}).get('parts', []):
            target_candidate['content']['parts'].append(copy.deepcopy(part))
            text = part.get('text', '')
            if text:
                kind = EventType.THINKING_DELTA if part.get('thought') else EventType.TEXT_DELTA
                await emit(Event(kind, {'content': text}, context))
