"""Gemini Interactions REST API (Api-Revision: 2026-05-20).

Complete steps history is always kept locally. Optional server-side chaining
only changes the outgoing request, never the canonical history.
"""

import copy
import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import time
from urllib.parse import urlparse

import httpx

from dialects.base import AdapterResult, DialectAdapter, NormalizedUsage, message_to_dict
from agent.protocol import Event, EventType

logger = logging.getLogger(__name__)


def _history_hashes(messages):
    """Hash canonical prefixes, excluding transport metadata and UI-only records."""
    digest = hashlib.sha256()
    for msg in messages:
        if not msg.get('role'):
            # AgentLoop also appends UI-only web_search_call records, which are
            # already represented by the signed gemini_steps on the assistant.
            yield digest.hexdigest()
            continue
        value = {k: v for k, v in msg.items() if k != 'gemini_interaction'}
        digest.update(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8'))
        digest.update(b'\n')
        yield digest.hexdigest()


def _chain_scope(client, cfg, context, tools):
    # Tier/stream can change between user and heartbeat calls. System messages
    # are already included in the prefix hash; all turn-scoped options are resent.
    value = [str(client.base_url), cfg.auth, cfg.name, context.session_id, cfg.vision,
             cfg.reasoning_effort, cfg.response_format, cfg.tool_names, tools]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _previous_interaction(messages, scope, auth, now):
    candidate = None
    # Renew the whole chain before its oldest ancestor can expire. Developer
    # free tier retains for one day; Cloud retains for seven days.
    retention = (7 if auth == 'adc' else 1) * 86400 - 60
    for index, (msg, prefix_hash) in enumerate(zip(messages, _history_hashes(messages))):
        metadata = msg.get('gemini_interaction')
        if msg.get('role') != 'assistant' or not isinstance(metadata, dict):
            continue
        started = metadata.get('chain_started_at')
        if (metadata.get('scope') == scope and metadata.get('prefix_hash') == prefix_hash
                and isinstance(metadata.get('id'), str) and metadata['id']
                and isinstance(started, (int, float)) and 0 <= now - started < retention):
            candidate = (index + 1, metadata)
    return candidate


class _GoogleADCAuth(httpx.Auth):
    requires_response_body = False

    def __init__(self, credentials, refresh_request):
        self.credentials = credentials
        self.refresh_request = refresh_request
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request):
        async with self._lock:
            if not self.credentials.valid:
                await asyncio.to_thread(self.credentials.refresh, self.refresh_request)
            headers = {}
            self.credentials.apply(headers)
        request.headers.update(headers)
        yield request


class GeminiClient(httpx.AsyncClient):
    def __init__(self, api_key='', base_url='https://generativelanguage.googleapis.com/v1beta',
                 proxy=None, auth_mode='api_key', project=None, location='global',
                 credentials=None, credentials_env='', **kwargs):
        headers = {'Api-Revision': '2026-05-20'}
        auth = None
        self._refresh_request = None
        self.context_cache = None
        if auth_mode == 'adc':
            if not project:
                raise ValueError('Google Cloud Interactions 需要 project')
            from config.google_auth import load_credentials, make_refresh_request
            if credentials is None:
                credentials = load_credentials(credentials_env, project)
            self._refresh_request = make_refresh_request(proxy)
            auth = _GoogleADCAuth(credentials, self._refresh_request)
            base_url = (base_url.rstrip('/') + f'/projects/{project}/locations/{location}/')
        else:
            headers['x-goog-api-key'] = api_key
        super().__init__(
            base_url=base_url.rstrip('/') + '/',
            headers=headers, auth=auth, timeout=300, proxy=proxy or None,
            trust_env=proxy is None, **kwargs,
        )

    async def close(self):
        await self.aclose()

    async def aclose(self):
        if self.context_cache is not None:
            await self.context_cache.clear()
        await super().aclose()
        if self._refresh_request is not None:
            self._refresh_request.close()


def _text(parts):
    return ''.join(p.get('text', '') for p in parts if p.get('type') == 'text')


def _normalize_search_steps(steps):
    """Normalize steps for outbound replay and local persistence.

    1. SSE omits google_search_call.search_type; replay needs it to match the
       signed tool context.
    2. Thought summaries are for human display (reasoning_content) only; strip
       summary from thought steps so summaries are not echoed back as model input.
    """
    steps = copy.deepcopy(steps)
    for step in steps:
        if step.get('type') == 'google_search_call':
            step.setdefault('search_type', 'web_search')
        elif step.get('type') == 'thought':
            step.pop('summary', None)
    return steps


class GeminiAPIError(RuntimeError):
    def __init__(self, message, status=None, retry_after=None, interaction_unavailable=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.interaction_unavailable = interaction_unavailable

    @property
    def retryable(self):
        return self.status in (429, 500, 502, 503, 504)


def _safe_label(value, limit=160):
    """Keep provider-controlled metadata useful without allowing echoed prompt text."""
    value = str(value or '')[:limit]
    return value if re.fullmatch(r'[A-Za-z0-9_.:/-]+', value) else ''


def _api_error(error, status=None, headers=None):
    """Only expose allowlisted diagnostic metadata: provider messages may echo inputs."""
    message = str(error.get('message', '')).lower() if isinstance(error, dict) else ''
    provider_status = _safe_label(error.get('status')) if isinstance(error, dict) else ''
    known_statuses = {'INVALID_ARGUMENT', 'PERMISSION_DENIED', 'NOT_FOUND', 'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION',
                      'INTERNAL', 'UNAVAILABLE', 'UNAUTHENTICATED', 'DEADLINE_EXCEEDED'}
    code = provider_status if provider_status in known_statuses else 'unknown'
    if code == 'unknown' and status == 429:
        code = 'RESOURCE_EXHAUSTED'
    # Cloud returns this generic HTTP 400 for a deleted previous_interaction_id
    # (verified with synthetic records). Only the chained caller retries it.
    precondition_failed = status == 400 and message.strip() == 'precondition check failed.'
    interaction_unavailable = precondition_failed or status in (404, 410) or (
        status in (None, 400) and code not in ('PERMISSION_DENIED', 'UNAUTHENTICATED', 'RESOURCE_EXHAUSTED')
        and 'interaction' in message and any(word in message for word in ('not found', 'expired', 'does not exist'))
    )
    if precondition_failed:
        reason = 'precondition_failed'
    elif interaction_unavailable and 'interaction' in message:
        reason = 'interaction_unavailable'
    elif 'tool type' in message and 'tool call context' in message:
        reason = 'tool_context_mismatch (check google_search_call.search_type)'
    elif 'signature' in message:
        reason = 'signature_validation_failed'
    elif 'token' in message and any(word in message for word in ('exceed', 'limit', 'maximum')):
        reason = 'token_limit_exceeded'
    elif 'file' in message and any(word in message for word in ('expired', 'not found', 'not active')):
        reason = 'file_unavailable'
    elif status == 429 and 'quota' in message:
        reason = 'quota_exhausted'
    elif status == 429:
        reason = 'capacity_or_rate_limit_exhausted'
    else:
        reason = 'provider_rejected_request (response text omitted for privacy)'

    diagnostics = []
    retry_after = None
    if headers:
        retry_after = _safe_label(headers.get('retry-after'), 32) or None
        request_id = (_safe_label(headers.get('x-request-id')) or
                      _safe_label(headers.get('x-goog-request-id')))
        if retry_after:
            diagnostics.append(f'retry_after={retry_after}')
        if request_id:
            diagnostics.append(f'request_id={request_id}')
    for detail in error.get('details', []) if isinstance(error, dict) else []:
        if not isinstance(detail, dict):
            continue
        detail_type = _safe_label(detail.get('@type'))
        if detail_type.endswith('ErrorInfo'):
            value = _safe_label(detail.get('reason'))
            if value:
                diagnostics.append(f'provider_reason={value}')
        elif detail_type.endswith('RetryInfo'):
            value = _safe_label(detail.get('retryDelay'), 32)
            if value:
                retry_after = retry_after or value
                diagnostics.append(f'retry_delay={value}')
        elif detail_type.endswith('QuotaFailure'):
            diagnostics.append('quota_failure=true')
    suffix = '; ' + '; '.join(dict.fromkeys(diagnostics)) if diagnostics else ''
    text = f'Gemini API error: HTTP {status or "stream"}; code={code}; reason={reason}{suffix}'
    return GeminiAPIError(text, status=status, retry_after=retry_after,
                          interaction_unavailable=interaction_unavailable)


def _check_response(response):
    if not response.is_error:
        return
    try:
        payload = response.json()
    except ValueError:
        # Google can return an SSE error body even with an HTTP 400 status.
        payload = {}
        for line in response.text.splitlines():
            if line.startswith('data:'):
                try:
                    payload = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(payload, dict) and 'error' in payload:
                    break
    error = payload.get('error', {}) if isinstance(payload, dict) else {}
    raise _api_error(error, response.status_code, response.headers)


class GeminiInteractionsDialect(DialectAdapter):
    name = 'interactions'

    async def _media(self, block, cfg, context):
        from attachments.media import supports_media
        kind = block['type']
        label = '音频' if kind == 'audio' else '视频'
        if not supports_media(cfg, kind):
            return {'type': 'text', 'text': f'[{label}：当前模型未启用此输入能力]'}
        resolver = getattr(context, 'image_file_resolver', None)
        file_id = await resolver(block, ('gcs',) if cfg.auth == 'adc' else ('gemini_files',)) if resolver else None
        if not file_id:
            raise ValueError(f'{label}远端引用不可用，请重新上传或检查文件上传配置')
        mime = block.get('mime') or mimetypes.guess_type(urlparse(file_id).path)[0]
        if not mime or not mime.startswith(kind + '/'):
            raise ValueError(f'{label}缺少有效 MIME 类型')
        return {'type': kind, 'uri': file_id, 'mime_type': mime}

    async def _image(self, block, cfg, context):
        if not cfg.vision:
            return {'type': 'text', 'text': '[图片]'}
        # Cloud uses private GCS; Developer API uses Gemini Files.
        file_id = ''
        file_resolver = getattr(context, 'image_file_resolver', None)
        if file_resolver:
            file_id = await file_resolver(block, ('gcs',) if cfg.auth == 'adc' else ('gemini_files',)) or ''
        if ((cfg.auth == 'adc' and file_id.startswith('gs://')) or
                (cfg.auth != 'adc' and file_id.startswith('https://generativelanguage.googleapis.com/'))):
            result = {'type': 'image', 'uri': file_id}
            result['mime_type'] = block.get('mime') or mimetypes.guess_type(urlparse(file_id).path)[0] or 'image/jpeg'
            return result
        url = block.get('image_url') or block.get('uri')
        if isinstance(url, dict):
            url = url.get('url')
        if not url and context.image_resolver:
            url = await context.image_resolver(block)
        if not url:
            return {'type': 'text', 'text': '[图片不可用]'}
        if url.startswith('data:'):
            header, data = url.split(',', 1)
            return {'type': 'image', 'mime_type': header[5:].split(';')[0], 'data': data}
        result = {'type': 'image', 'uri': url}
        mime = block.get('mime') or mimetypes.guess_type(urlparse(url).path)[0]
        if mime:
            result['mime_type'] = mime
        return result

    async def _content_parts(self, content, cfg, context):
        if isinstance(content, str):
            return [{'type': 'text', 'text': content}] if content else []
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind in ('text', 'input_text'):
                parts.append({'type': 'text', 'text': block.get('text', '')})
            elif kind in ('image', 'input_image', 'image_url'):
                parts.append(await self._image(block, cfg, context))
            elif kind in ('audio', 'video'):
                parts.append(await self._media(block, cfg, context))
        return parts

    async def _request(self, cfg, messages, tools, context, *, start=0):
        steps, system, names = [], [], {}
        for index, msg in enumerate(messages):
            role = msg.get('role')
            if not role:
                continue
            content = msg.get('content') or ''
            if role in ('system', 'developer'):
                system.append(content if isinstance(content, str) else _text(content))
                continue
            for tc in msg.get('tool_calls') or []:
                names[tc['id']] = tc['function']['name']
            if index < start:
                continue
            if role == 'assistant' and msg.get('gemini_steps'):
                steps.extend(_normalize_search_steps(msg['gemini_steps']))
                continue
            if role == 'tool':
                result = await self._content_parts(content, cfg, context) if isinstance(content, list) else content
                step = {'type': 'function_result', 'call_id': msg['tool_call_id'], 'result': result}
                if names.get(msg['tool_call_id']):
                    step['name'] = names[msg['tool_call_id']]
                steps.append(step)
                continue
            parts = await self._content_parts(content, cfg, context)
            if parts:
                steps.append({'type': 'model_output' if role == 'assistant' else 'user_input', 'content': parts})
            for tc in msg.get('tool_calls') or []:
                steps.append({'type': 'function_call', 'id': tc['id'],
                              'name': tc['function']['name'],
                              'arguments': json.loads(tc['function']['arguments'])})
        body = {'model': cfg.name, 'input': steps, 'stream': cfg.stream, 'store': cfg.interactions_stateful}
        if cfg.service_tier:
            body['service_tier'] = cfg.service_tier
        if system:
            body['system_instruction'] = '\n\n'.join(system)
        generation = {'thinking_level': cfg.reasoning_effort, 'thinking_summaries': 'auto'}
        # New Interactions schema does not expose temperature.
        body['generation_config'] = generation
        declarations = [{'type': 'function', **t['function']} for t in tools or []]
        if 'web_search' in (cfg.tool_names or []) and not any(
            t.get('name') == 'web_search' for t in declarations
        ):
            declarations.append({'type': 'google_search', 'search_types': ['web_search']})
        if declarations:
            body['tools'] = declarations
        if cfg.response_format:
            fmt = cfg.response_format
            if fmt.get('type') in ('json_object', 'json_schema'):
                fmt = {'type': 'text', 'mime_type': 'application/json'}
                if cfg.response_format['type'] == 'json_schema':
                    fmt['schema'] = cfg.response_format['json_schema']['schema']
            body['response_format'] = fmt
        return body

    async def call(self, client, cfg, full_messages, tools, context, emit):
        started = time.time()
        scope = _chain_scope(client, cfg, context, tools) if cfg.interactions_stateful else None
        previous = (_previous_interaction(full_messages, scope, cfg.auth, started)
                    if cfg.interactions_stateful else None)
        body = await self._request(cfg, full_messages, tools, context,
                                   start=previous[0] if previous else 0)
        if previous:
            body['previous_interaction_id'] = previous[1]['id']
        emitted = False

        async def tracked_emit(event):
            nonlocal emitted
            emitted = True
            await emit(event)

        async def send(request_body):
            if cfg.stream:
                value = await self._stream(client, request_body, context, tracked_emit)
            else:
                response = await client.post('interactions', json=request_body)
                _check_response(response)
                value = response.json()
            if value.get('status') not in ('completed', 'requires_action'):
                raise _api_error(value.get('error', {}))
            return value

        try:
            interaction = await send(body)
        except GeminiAPIError as exc:
            # Retry only a missing/expired chain, once, before any stream output.
            # Never replay local tools or retry permission/rate/connection errors.
            if not previous or not exc.interaction_unavailable or emitted:
                raise
            logger.info('Interactions 续接记录不可用，使用完整本地历史重建')
            previous = None
            body = await self._request(cfg, full_messages, tools, context)
            interaction = await send(body)
        raw_steps = interaction.get('steps', [])
        content, reasoning, calls, searches = [], [], {}, []
        for step in raw_steps:
            kind = step['type']
            if kind == 'model_output':
                content.append(_text(step.get('content', [])))
            elif kind == 'thought':
                reasoning.append(_text(step.get('summary', [])))
            elif kind == 'function_call':
                calls[len(calls)] = {'id': step['id'], 'function': {
                    'name': step['name'], 'arguments': json.dumps(step['arguments'], ensure_ascii=False)}}
            elif kind == 'google_search_call':
                searches.append({'type': 'web_search_call', 'id': step['id'], 'status': 'completed',
                                 'action': {'type': 'search', 'queries': step.get('arguments', {}).get('queries', [])}})
        steps = _normalize_search_steps(raw_steps)
        message = self._build_message(''.join(content), ''.join(reasoning), calls)
        object.__setattr__(message, 'gemini_steps', steps)
        if cfg.interactions_stateful and interaction.get('id'):
            # Persist the full response AND a verifiable pointer. The latter can
            # be discarded freely without losing any local conversation content.
            prefix_hash = None
            for prefix_hash in _history_hashes([*full_messages, message_to_dict(message)]):
                pass
            object.__setattr__(message, 'gemini_interaction', {
                'id': interaction['id'], 'scope': scope, 'prefix_hash': prefix_hash,
                'chain_started_at': previous[1]['chain_started_at'] if previous else started,
            })
        raw = interaction.get('usage')
        usage = None
        if raw:
            prompt = raw.get('total_input_tokens', 0)
            thought = raw.get('total_thought_tokens', 0)
            completion = raw.get('total_output_tokens', 0) + thought
            usage = NormalizedUsage(prompt_tokens=prompt, completion_tokens=completion,
                                    total_tokens=raw.get('total_tokens', prompt + completion),
                                    cached_tokens=raw.get('total_cached_tokens', 0), reasoning_tokens=thought)
        return AdapterResult(message, usage, searches, response_model=interaction.get('model', ''))

    async def _stream(self, client, body, context, emit):
        steps, arguments, interaction = {}, {}, {}

        async def emit_text(kind, value):
            if value:
                await emit(Event(kind, {'content': value}, context))

        async def consume(event):
            nonlocal interaction
            kind = event.get('event_type') or event.get('type')
            if kind == 'error':
                raise _api_error(event.get('error', {}))
            if kind.startswith('interaction.'):
                interaction.update(event.get('interaction', {}))
                if kind in ('interaction.completed', 'interaction.requires_action', 'interaction.failed',
                            'interaction.cancelled', 'interaction.incomplete'):
                    interaction['status'] = kind.split('.', 1)[1]
            elif kind == 'step.start':
                step = steps[event['index']] = copy.deepcopy(event['step'])
                if step['type'] == 'model_output':
                    await emit_text(EventType.TEXT_DELTA, _text(step.get('content', [])))
                elif step['type'] == 'thought':
                    await emit_text(EventType.THINKING_DELTA, _text(step.get('summary', [])))
            elif kind == 'step.delta':
                index = event['index']
                step, delta = steps[index], event['delta']
                dtype = delta['type']
                if dtype == 'text':
                    parts = step.setdefault('content', [])
                    if parts and parts[-1].get('type') == 'text':
                        parts[-1]['text'] = parts[-1].get('text', '') + delta['text']
                    else:
                        parts.append({'type': 'text', 'text': delta['text']})
                    await emit_text(EventType.TEXT_DELTA, delta['text'])
                elif dtype == 'arguments_delta':
                    arguments[index] = arguments.get(index, '') + delta.get('arguments', '')
                elif dtype == 'thought_summary':
                    part = delta.get('content') or {}
                    summary = step.setdefault('summary', [])
                    if part.get('type') == 'text' and summary and summary[-1].get('type') == 'text':
                        summary[-1]['text'] = summary[-1].get('text', '') + part.get('text', '')
                    elif part:
                        summary.append(copy.deepcopy(part))
                    await emit_text(EventType.THINKING_DELTA, _text([part]))
                elif dtype == 'thought_signature':
                    step['signature'] = delta['signature']
                elif dtype == 'text_annotation_delta':
                    texts = [p for p in step.get('content', []) if p.get('type') == 'text']
                    if texts:
                        texts[-1].setdefault('annotations', []).extend(delta.get('annotations', []))
                else:
                    step.update({k: v for k, v in delta.items() if k != 'type'})
                if event.get('metadata', {}).get('total_usage'):
                    interaction['usage'] = event['metadata']['total_usage']
            elif kind == 'step.stop':
                step = steps[event['index']]
                if step['type'] == 'google_search_call':
                    await emit(Event(EventType.WEB_SEARCH, {
                        'id': step['id'], 'status': 'completed',
                        'query': ' / '.join(step.get('arguments', {}).get('queries', [])),
                    }, context))

        async with client.stream('POST', 'interactions', json=body) as response:
            if response.is_error:
                await response.aread()
            _check_response(response)
            data = []
            async for line in response.aiter_lines():
                if line.startswith('data:'):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    payload = '\n'.join(data)
                    data = []
                    if payload != '[DONE]':
                        await consume(json.loads(payload))
            if data and '\n'.join(data) != '[DONE]':
                await consume(json.loads('\n'.join(data)))
        for index, value in arguments.items():
            steps[index]['arguments'] = json.loads(value)
        interaction['steps'] = [steps[i] for i in sorted(steps)]
        return interaction
