"""Synthetic-only cache API tests; no credentials, messages or real HTTP calls."""

import asyncio
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent.session import AgentSession
from agent.commands import run_command
from dialects import get_dialect
from dialects.base import NormalizedUsage
from dialects.gemini import GeminiClient, GeminiAPIError
from dialects.gemini_cache import GeminiContextCache
from agent.protocol import Context, LoopResult, ModelConfig


class CacheAPI:
    def __init__(self):
        self.now = datetime.fromisoformat('2026-10-01T14:55:00+00:00').timestamp()
        self.resources = {}
        self.requests = []
        self.serial = 0
        self.fail_delete = False
        self.fail_create = False
        self.reject_reference = False

    @staticmethod
    def count(body):
        system = body.get('systemInstruction', {}).get('parts', [])
        parts = system + [p for item in body.get('contents', []) for p in item['parts']]
        return sum(len(p.get('text', '')) for p in parts)

    def expiration(self, ttl):
        return datetime.fromtimestamp(self.now + int(ttl[:-1]), timezone.utc).isoformat()

    def handle(self, request):
        payload = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, payload))
        path = request.url.path
        if path.endswith(':countTokens'):
            return httpx.Response(200, json={'totalTokens': self.count(payload)})
        if path.endswith('/cachedContents'):
            if request.method == 'GET':
                return httpx.Response(200, json={'cachedContents': list(self.resources.values())})
            if self.fail_create:
                return httpx.Response(403, json={'error': {'status': 'PERMISSION_DENIED'}})
            self.serial += 1
            name = f'projects/test-project/locations/global/cachedContents/{self.serial}'
            entry = {'name': name, 'displayName': payload['displayName'],
                     'expireTime': payload.get('expireTime') or self.expiration(payload['ttl']),
                     'usageMetadata': {'totalTokenCount': self.count(payload)}}
            self.resources[name] = entry
            return httpx.Response(200, json=entry)
        if '/cachedContents/' in path:
            name = path.split('/v1beta1/', 1)[1]
            if name not in self.resources:
                return httpx.Response(404, json={'error': {'status': 'NOT_FOUND'}})
            if request.method == 'DELETE':
                if self.fail_delete:
                    return httpx.Response(503, json={'error': {'status': 'UNAVAILABLE'}})
                del self.resources[name]
                return httpx.Response(200, json={})
            assert request.url.params.get('updateMask') == 'expireTime'
            self.resources[name]['expireTime'] = payload.get('expireTime') or self.expiration(payload['ttl'])
            return httpx.Response(200, json=self.resources[name])
        if path.endswith(':generateContent'):
            if self.reject_reference and payload.get('cachedContent'):
                return httpx.Response(404, json={'error': {'status': 'NOT_FOUND'}})
            return httpx.Response(200, json={
                'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ok'}]}}],
                'usageMetadata': {
                    'promptTokenCount': self.count(payload) + (
                        self.resources[payload['cachedContent']]['usageMetadata']['totalTokenCount']
                        if payload.get('cachedContent') else 0),
                    'cachedContentTokenCount': (
                        self.resources[payload['cachedContent']]['usageMetadata']['totalTokenCount']
                        if payload.get('cachedContent') else 0),
                    'candidatesTokenCount': 1,
                }})
        raise AssertionError((request.method, path))


@pytest.fixture
def cfg():
    return ModelConfig('gemini-3.8-flash', 'https://example.test/v1beta1',
                       auth='adc', project='test-project', location='global',
                       api_style='gemini_generate', stream=False, max_retries=0)


@pytest.fixture
def body():
    return {'systemInstruction': {'parts': [{'text': 's' * 5000}]},
            'tools': [{'functionDeclarations': [{'name': 'test_tool'}]}],
            'generationConfig': {'thinkingConfig': {'thinkingLevel': 'MEDIUM'}},
            'contents': [
                {'role': role, 'parts': [{'text': str(i) * 1000}]}
                for i, role in enumerate(['user', 'model'] * 3 + ['user'])]}


@pytest.fixture
async def setup_cache(tmp_path):
    api = CacheAPI()
    async with httpx.AsyncClient(
        base_url='https://example.test/v1beta1/projects/test-project/locations/global/',
        transport=httpx.MockTransport(api.handle),
    ) as client:
        config = {'state_path': str(tmp_path / 'cache.json')}
        cache = GeminiContextCache(client, config, clock=lambda: api.now)
        cache.note_user_input()
        cache.note_user_input()
        yield api, client, cache, config


@pytest.mark.asyncio
async def test_prefix_reuse_preserves_full_history_and_signed_parts(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    body['contents'][1]['parts'][0]['thoughtSignature'] = 'synthetic-signature'
    original = copy.deepcopy(body)
    request, used = await cache.prepare(body, cfg, 'session-a')
    assert used and body == original
    assert request['contents'] == original['contents'][6:]
    assert 'tools' not in request and 'systemInstruction' not in request
    create = next(p for m, path, p in api.requests if m == 'POST' and path.endswith('/cachedContents'))
    assert create['contents'] == original['contents'][:6]
    assert create['contents'][1]['parts'][0]['thoughtSignature'] == 'synthetic-signature'
    assert request['generationConfig'] == original['generationConfig']
    persisted = cache.path.read_text()
    assert 'synthetic-signature' not in persisted and 'ssssss' not in persisted
    before = len(api.requests)
    await cache.prepare(body, cfg, 'session-a')
    assert len(api.requests) == before  # No counting/creation on normal reuse.


@pytest.mark.asyncio
async def test_only_new_user_input_renews_and_restart_retires(setup_cache, cfg, body):
    api, client, cache, config = setup_cache
    first, _ = await cache.prepare(body, cfg, 'a')
    expiry = cache.state['active']['expires']
    assert expiry == api.now + 600
    api.now += 100
    await cache.prepare(body, cfg, 'a')
    assert cache.state['active']['expires'] == expiry
    assert not any(method == 'PATCH' for method, _, _ in api.requests)
    cache.note_user_input()
    second, _ = await cache.prepare(body, cfg, 'a')
    assert second['cachedContent'] == first['cachedContent']
    assert cache.state['active']['expires'] == api.now + 600
    assert api.serial == 1
    restarted = GeminiContextCache(client, config, clock=lambda: api.now)
    third, used = await restarted.prepare(body, cfg, 'a')
    assert not used and third == body
    assert not api.resources


@pytest.mark.asyncio
async def test_active_rebuild_requires_20_completed_uses_and_deletes_old(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    first, _ = await cache.prepare(body, cfg, 'a')
    grown = copy.deepcopy(body)
    grown['contents'] += [{'role': 'model', 'parts': [{'text': 'm' * 8000}]},
                          {'role': 'user', 'parts': [{'text': 'new'}]}]
    cache.note_user_input()
    api.now += 60
    cache.note_user_input()
    usage = NormalizedUsage(prompt_tokens=api.count(grown), completion_tokens=1,
                            total_tokens=api.count(grown) + 1,
                            cached_tokens=cache.state['active']['tokens'])
    for _ in range(19):
        await cache.record_use(first['cachedContent'], usage)
    await cache.prepare(grown, cfg, 'a')
    assert api.serial == 1
    await cache.record_use(first['cachedContent'], usage)
    second, _ = await cache.prepare(grown, cfg, 'a')
    assert api.serial == 2 and first['cachedContent'] not in api.resources
    assert second['cachedContent'] in api.resources
    assert cache.state['active']['reuse_count'] == 0
    await cache.prepare(grown, cfg, 'a')
    assert api.serial == 2
    api.now += 3600
    await cache.prepare(grown, cfg, 'a')
    assert api.serial == 2


@pytest.mark.asyncio
async def test_eligible_active_cache_keeps_high_coverage_without_counting(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    body['contents'][-1]['parts'][0]['text'] = 'q'
    first, _ = await cache.prepare(body, cfg, 'a')
    cache.note_user_input()
    api.now += 1
    cache.note_user_input()
    usage = NormalizedUsage(prompt_tokens=api.count(body), completion_tokens=1,
                            total_tokens=api.count(body) + 1,
                            cached_tokens=cache.state['active']['tokens'])
    for _ in range(20):
        await cache.record_use(first['cachedContent'], usage)
    before = len(api.requests)
    await cache.prepare(body, cfg, 'a')
    assert all(method == 'PATCH' for method, _, _ in api.requests[before:])
    assert api.serial == 1


async def record_reuses(api, cache, body, count=20):
    entry = cache.state['active']
    usage = NormalizedUsage(prompt_tokens=api.count(body), completion_tokens=1,
                            total_tokens=api.count(body) + 1, cached_tokens=entry['tokens'])
    for _ in range(count):
        await cache.record_use(entry['name'], usage)


def test_activity_requires_real_user_arrivals_and_expires(tmp_path):
    now = [1000.0]
    cache = GeminiContextCache(None, {'state_path': str(tmp_path / 'cache.json')}, clock=lambda: now[0])
    cache.note_user_input()
    assert not cache.is_active()
    now[0] += 301
    cache.note_user_input()
    assert not cache.is_active()
    now[0] += 300
    cache.note_user_input()
    assert cache.is_active()
    now[0] += 500
    cache.note_user_input()  # Already active: refresh even if the gap exceeded 5 minutes.
    now[0] += 600
    assert cache.is_active()
    now[0] += 1
    assert not cache.is_active()
    cache.note_user_input()
    assert not cache.is_active()


@pytest.mark.asyncio
async def test_idle_low_coverage_retires_instead_of_renewing(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    await cache.prepare(body, cfg, 'a')
    grown = copy.deepcopy(body)
    grown['contents'] += [{'role': 'model', 'parts': [{'text': 'm' * 8000}]},
                          {'role': 'user', 'parts': [{'text': 'new'}]}]
    await record_reuses(api, cache, grown)
    api.now += 601
    request, used = await cache.prepare(grown, cfg, 'a')
    assert not used and request == grown
    assert api.serial == 1 and cache.state['active'] is None
    assert all(datetime.fromisoformat(r['expireTime']).timestamp() <= api.now for r in api.resources.values())
    assert not any(method == 'PATCH' for method, _, _ in api.requests)


@pytest.mark.asyncio
async def test_reuse_count_survives_restart_but_activity_does_not(setup_cache, cfg, body):
    api, client, cache, config = setup_cache
    await cache.prepare(body, cfg, 'a')
    await record_reuses(api, cache, body, 21)
    cache.note_user_input()
    cache.note_user_input()
    assert cache.is_active()
    restarted = GeminiContextCache(client, config, clock=lambda: api.now)
    restarted._load()
    assert restarted.state['active']['reuse_count'] == 21
    assert not restarted.is_active()


@pytest.mark.asyncio
async def test_legacy_cache_metadata_starts_with_zero_known_reuses(setup_cache, cfg, body):
    api, client, cache, config = setup_cache
    await cache.prepare(body, cfg, 'a')
    old_state = json.loads(cache.path.read_text())
    old_state['active'].pop('reuse_count')
    old_state['active'].pop('last_prompt_tokens')
    old_state['checkpoint'] = '2026-09-30'
    cache.path.write_text(json.dumps(old_state))
    restarted = GeminiContextCache(client, config, clock=lambda: api.now)
    restarted._load()
    assert '成功复用 0 次' in restarted.status()
    assert 'checkpoint' not in restarted.state
    assert api.serial == 1


@pytest.mark.asyncio
async def test_no_billable_rebuild_if_no_new_complete_boundary(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    first, _ = await cache.prepare(body, cfg, 'a')
    body['contents'][-1]['parts'][0]['text'] += 'x' * 20000
    await record_reuses(api, cache, body)
    cache.note_user_input()
    cache.note_user_input()
    second, _ = await cache.prepare(body, cfg, 'a')
    assert second['cachedContent'] == first['cachedContent']
    assert api.serial == 1


@pytest.mark.asyncio
async def test_manual_rebuild_bypasses_activity_and_reuse_count(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    first, _ = await cache.prepare(body, cfg, 'a')
    second, _ = await cache.prepare(body, cfg, 'a', force=True)
    assert api.serial == 2 and first['cachedContent'] not in api.resources
    assert second['cachedContent'] != first['cachedContent']
    assert cache.state['active']['reuse_count'] == 0


@pytest.mark.asyncio
async def test_invalidation_bypasses_reuse_gate_and_resets_count(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    await cache.prepare(body, cfg, 'a')
    await record_reuses(api, cache, body, 3)
    body['systemInstruction']['parts'][0]['text'] += 'new-system'
    await cache.prepare(body, cfg, 'a')
    assert api.serial == 2
    assert cache.state['active']['reuse_count'] == 0


@pytest.mark.asyncio
async def test_usage_only_counts_confirmed_current_resource_and_save_failure_is_nonfatal(
        setup_cache, cfg, body, monkeypatch):
    api, _, cache, _ = setup_cache
    first, _ = await cache.prepare(body, cfg, 'a')
    entry = cache.state['active']
    valid = NormalizedUsage(prompt_tokens=api.count(body), completion_tokens=1,
                            total_tokens=api.count(body) + 1, cached_tokens=entry['tokens'])
    await cache.record_use(first['cachedContent'], None)
    await cache.record_use(first['cachedContent'], NormalizedUsage(
        prompt_tokens=api.count(body), completion_tokens=1, total_tokens=api.count(body) + 1))
    await cache.record_use('obsolete-resource', valid)
    assert entry['reuse_count'] == 0
    monkeypatch.setattr(cache, '_save', Mock(side_effect=OSError('synthetic-save-failure')))
    await cache.record_use(first['cachedContent'], valid)
    assert entry['reuse_count'] == 1


@pytest.mark.asyncio
async def test_successful_generations_count_once_each_without_activating_user_burst(setup_cache, cfg):
    api, client, cache, _ = setup_cache
    client.context_cache = cache
    messages = [{'role': 'system', 'content': 's' * 5000},
                {'role': 'user', 'content': 'u' * 3000},
                {'role': 'assistant', 'content': 'a' * 1000},
                {'role': 'user', 'content': 'q' * 1000}]
    for _ in range(20):
        await get_dialect('gemini_generate').call(
            client, cfg, messages, None, Context('', [], '', 'test', session_id='a'), AsyncMock())
    assert cache.state['active']['reuse_count'] == 20
    assert json.loads(cache.path.read_text())['active']['reuse_count'] == 20
    assert cache.is_active()  # API uses do not extend the original activity deadline.
    assert api.serial == 1


@pytest.mark.asyncio
async def test_only_real_rounds_activate_burst_not_heartbeat_or_slash_commands(
        setup_cache, cfg, monkeypatch):
    api, client, cache, _ = setup_cache
    client.context_cache = cache
    cache.last_user_at = None
    cache.active_until = 0
    agent = AgentSession.__new__(AgentSession)
    agent._lock = asyncio.Lock()
    agent.platform = 'test'
    agent._system_prompt = 'synthetic-system'
    session = SimpleNamespace(session_id='a', start_time=1, history=[])
    agent.session_manager = SimpleNamespace(
        maybe_compress_daily=AsyncMock(), get_or_create_active_session=lambda: session,
        record_input_time=Mock(), context_tokens=0)
    agent._loop = SimpleNamespace(client=client, model_config=cfg, run=AsyncMock(
        side_effect=lambda messages, context: LoopResult(
            full_messages=messages, final_answer='synthetic-answer', total_usage={})))
    agent._update_history_and_archive = AsyncMock()
    agent._save_rollback_state = Mock()
    agent._clear_rollback_state = Mock()
    monkeypatch.setattr('agent.session.FileManager', lambda: SimpleNamespace(configured=False))
    monkeypatch.setattr('agent.session.time.time', lambda: api.now)
    await agent.run_round('heartbeat')  # Literal user text is still a real user arrival.
    assert cache.last_user_at == api.now and not cache.is_active()
    api.now += 60
    await agent.run_round(source="heartbeat", injection_messages=[{'role': 'user', 'content': 'synthetic-beat'}], trigger_time=api.now)
    await run_command('cache status', agent)
    await agent.run_round('/cache status')  # Direct host callers also cannot mark commands active.
    assert not cache.is_active()
    await agent.run_round('synthetic-user')
    assert cache.is_active()
    last_user = cache.last_user_at
    api.now += 601
    await agent.run_round(source="heartbeat", injection_messages=[{'role': 'user', 'content': 'synthetic-beat'}], trigger_time=api.now)
    assert not cache.is_active() and cache.last_user_at == last_user


@pytest.mark.asyncio
async def test_retry_failure_counts_only_final_success(setup_cache, cfg, monkeypatch):
    api, _, _, config = setup_cache
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path.endswith(':generateContent'):
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, json={'error': {'status': 'RESOURCE_EXHAUSTED'}})
        return api.handle(request)

    cfg.max_retries = 1
    monkeypatch.setattr('dialects.gemini_generate.asyncio.sleep', AsyncMock())
    async with httpx.AsyncClient(
        base_url='https://example.test/v1beta1/projects/test-project/locations/global/',
        transport=httpx.MockTransport(handler),
    ) as client:
        client.context_cache = GeminiContextCache(client, config, clock=lambda: api.now)
        client.context_cache.note_user_input()
        client.context_cache.note_user_input()
        result = await get_dialect('gemini_generate').call(
            client, cfg, [{'role': 'system', 'content': 's' * 5000},
                          {'role': 'user', 'content': 'synthetic-user'}],
            None, Context('', [], '', 'test', session_id='a'), AsyncMock())
        assert result.message.content == 'ok' and attempts == 2
        assert client.context_cache.state['active']['reuse_count'] == 1


@pytest.mark.asyncio
async def test_stream_success_with_usage_records_reuse(setup_cache, cfg):
    api, _, _, config = setup_cache
    cfg.stream = True

    def handler(request):
        if request.url.path.endswith(':streamGenerateContent'):
            payload = json.loads(request.content)
            cached = api.resources[payload['cachedContent']]['usageMetadata']['totalTokenCount']
            chunk = {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ok'}]}}],
                     'usageMetadata': {'promptTokenCount': cached + api.count(payload),
                                       'cachedContentTokenCount': cached, 'candidatesTokenCount': 1}}
            return httpx.Response(200, content='data: ' + json.dumps(chunk) + '\n\n')
        return api.handle(request)

    async with httpx.AsyncClient(
        base_url='https://example.test/v1beta1/projects/test-project/locations/global/',
        transport=httpx.MockTransport(handler),
    ) as client:
        client.context_cache = GeminiContextCache(client, config, clock=lambda: api.now)
        client.context_cache.note_user_input()
        client.context_cache.note_user_input()
        result = await get_dialect('gemini_generate').call(
            client, cfg, [{'role': 'system', 'content': 's' * 5000},
                          {'role': 'user', 'content': 'synthetic-user'}],
            None, Context('', [], '', 'test', session_id='a'), AsyncMock())
        assert result.message.content == 'ok'
        assert client.context_cache.state['active']['reuse_count'] == 1


@pytest.mark.asyncio
async def test_exact_90_percent_keeps_cache_and_fresh_count_confirms_low_signal(setup_cache, cfg):
    api, _, cache, _ = setup_cache
    body = {'systemInstruction': {'parts': [{'text': 's' * 5000}]},
            'contents': [{'role': 'user', 'parts': [{'text': 'u' * 1000}]},
                         {'role': 'model', 'parts': [{'text': 'm' * 3000}]},
                         {'role': 'user', 'parts': [{'text': 'q' * 1000}]}]}
    first, _ = await cache.prepare(body, cfg, 'a')
    assert cache.state['active']['tokens'] == 9000
    await record_reuses(api, cache, body)
    cache.note_user_input()
    cache.note_user_input()
    before = len(api.requests)
    await cache.prepare(body, cfg, 'a')
    assert len(api.requests) == before and api.serial == 1
    large = copy.deepcopy(body)
    large['contents'][-1]['parts'][0]['text'] += 'q' * 3000
    await record_reuses(api, cache, large, 1)
    # Last successful usage was low, but current suffix can shrink without touching prefix.
    second, _ = await cache.prepare(body, cfg, 'a')
    assert second['cachedContent'] == first['cachedContent'] and api.serial == 1
    assert api.requests[-1][1].endswith(':countTokens')


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['system', 'tools', 'session', 'rollback', 'image'])
async def test_invalid_prefix_is_deleted_before_replacement(setup_cache, cfg, body, change):
    api, _, cache, _ = setup_cache
    body['contents'][0]['parts'].append({'fileData': {'fileUri': 'gs://synthetic/a'}})
    old, _ = await cache.prepare(body, cfg, 'a')
    session = 'a'
    if change == 'system':
        body['systemInstruction']['parts'][0]['text'] += 'changed'
    elif change == 'tools':
        body['tools'][0]['functionDeclarations'][0]['name'] = 'changed'
    elif change == 'session':
        session = 'b'
    elif change == 'rollback':
        body['contents'] = body['contents'][:2]
    else:
        body['contents'][0]['parts'][-1]['fileData']['fileUri'] = 'gs://synthetic/b'
    await cache.prepare(body, cfg, session)
    assert old['cachedContent'] not in api.resources


@pytest.mark.asyncio
async def test_delete_failure_is_persisted_retried_and_not_renewed(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    old, _ = await cache.prepare(body, cfg, 'a')
    api.fail_delete = True
    await cache.prepare(body, cfg, 'a', force=True)
    assert len(api.resources) == 2
    assert cache.state['retired'][0]['name'] == old['cachedContent']
    assert json.loads(cache.path.read_text())['retired']
    old_expiry = api.resources[old['cachedContent']]['expireTime']
    with pytest.raises(RuntimeError, match='旧缓存删除未完成'):
        await cache.prepare(body, cfg, 'a', force=True)
    assert api.serial == 2  # Do not accumulate billable caches while cleanup fails.
    api.now += 301
    await cache.prepare(body, cfg, 'a')
    assert api.resources[old['cachedContent']]['expireTime'] == old_expiry
    api.now += 61
    api.fail_delete = False
    await cache.prepare(body, cfg, 'a')
    assert len(api.resources) == 1
    assert not cache.state['retired']


@pytest.mark.asyncio
async def test_failed_rebuild_preserves_valid_old_cache(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    old, _ = await cache.prepare(body, cfg, 'a')
    api.fail_create = True
    with pytest.raises(RuntimeError, match='缓存创建失败'):
        await cache.prepare(body, cfg, 'a', force=True)
    assert old['cachedContent'] in api.resources
    assert cache.state['active']['name'] == old['cachedContent']
    request, used = await cache.prepare(body, cfg, 'a')
    assert used and request['cachedContent'] == old['cachedContent']


@pytest.mark.asyncio
async def test_orphan_cleanup_is_limited_to_our_owner(setup_cache, cfg, body):
    api, client, cache, config = setup_cache
    await cache.prepare(body, cfg, 'a')
    owner = cache.state['owner']
    for name, label in [('orphan', 'tentacle-cache-' + owner), ('foreign', 'another-app')]:
        resource = f'projects/test-project/locations/global/cachedContents/{name}'
        api.resources[resource] = {'name': resource, 'displayName': label,
                                   'expireTime': api.expiration('7200s')}
    restarted = GeminiContextCache(client, config, clock=lambda: api.now)
    await restarted.prepare(body, cfg, 'a')
    assert any(name.endswith('/foreign') for name in api.resources)
    assert not any(name.endswith('/orphan') for name in api.resources)


@pytest.mark.asyncio
async def test_tool_cycle_is_not_split(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    body['contents'] = [
        {'role': 'user', 'parts': [{'text': 'u' * 3000}]},
        {'role': 'model', 'parts': [{'functionCall': {'name': 'test_tool', 'args': {}},
                                    'thoughtSignature': 'signed'}]},
        {'role': 'user', 'parts': [{'functionResponse': {'name': 'test_tool', 'response': {}}}]},
        {'role': 'model', 'parts': [{'text': 'done'}]},
        {'role': 'user', 'parts': [{'text': 'q' * 1000}]},
    ]
    await cache.prepare(body, cfg, 'a')
    assert cache.state['active']['count'] == 4
    create = next(p for m, path, p in api.requests if m == 'POST' and path.endswith('/cachedContents'))
    assert create['contents'][:3] == body['contents'][:3]


@pytest.mark.asyncio
async def test_unavailable_cache_falls_back_to_entire_request(setup_cache, cfg, body):
    api, client, cache, _ = setup_cache
    client.context_cache = cache
    dialect = get_dialect('gemini_generate')
    messages = [{'role': 'system', 'content': 's' * 5000},
                {'role': 'user', 'content': 'u' * 3000},
                {'role': 'assistant', 'content': 'a' * 1000},
                {'role': 'user', 'content': 'q' * 1000}]
    context = Context('', [], '', 'test', session_id='a')
    api.reject_reference = True
    result = await dialect.call(client, cfg, messages, None, context, AsyncMock())
    assert result.message.content == 'ok'
    generates = [p for _, path, p in api.requests if path.endswith(':generateContent')]
    assert len(generates) == 2 and 'cachedContent' in generates[0]
    full = await dialect._request(cfg, messages, None, context)
    assert generates[1] == full
    assert not api.resources


@pytest.mark.asyncio
async def test_small_context_and_parallel_requests_do_not_create_duplicates(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    small = {'contents': [{'role': 'user', 'parts': [{'text': 'small'}]}]}
    original, used = await cache.prepare(small, cfg, 'a')
    assert original == small and not used and not api.resources
    api.now += 301
    await asyncio.gather(*(cache.prepare(body, cfg, 'a') for _ in range(4)))
    assert api.serial == 1


@pytest.mark.asyncio
async def test_slash_cache_creates_without_generation_and_other_models_refused(setup_cache, cfg, monkeypatch):
    api, client, cache, _ = setup_cache
    client.context_cache = cache
    agent = AgentSession.__new__(AgentSession)
    agent._lock = asyncio.Lock()
    agent.platform = 'test'
    agent._system_prompt = 's' * 5000
    session = SimpleNamespace(session_id='a', history=[
        {'role': 'user', 'content': 'u' * 3000},
        {'role': 'assistant', 'content': 'a' * 1000},
        {'role': 'user', 'content': 'q' * 1000}])
    agent.session_manager = SimpleNamespace(get_or_create_active_session=lambda: session)
    agent._loop = SimpleNamespace(client=client, model_config=cfg,
                                  dialect=get_dialect('gemini_generate'))
    monkeypatch.setattr('agent.session.FileManager', lambda: SimpleNamespace(configured=False))
    history_before = copy.deepcopy(session.history)
    assert '已创建' in await run_command('cache', agent)
    assert session.history == history_before
    assert not any(path.endswith(':generateContent') for _, path, _ in api.requests)
    assert 'tokens' in await run_command('cache status', agent)
    assert '已清理' in await run_command('cache clear', agent)
    assert not api.resources
    cfg.api_style = 'chat'
    before = len(api.requests)
    assert '不适用于其他厂商' in await run_command('cache', agent)
    assert len(api.requests) == before


@pytest.mark.asyncio
async def test_stream_cache_rejection_falls_back_before_emitting(setup_cache, cfg):
    api, _, _, config = setup_cache
    cfg.stream = True
    generated = []

    def handler(request):
        if request.url.path.endswith(':streamGenerateContent'):
            payload = json.loads(request.content)
            generated.append(payload)
            if payload.get('cachedContent'):
                return httpx.Response(404, json={'error': {'status': 'NOT_FOUND'}})
            chunk = {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ok'}]}}]}
            return httpx.Response(200, content='data: ' + json.dumps(chunk) + '\n\n',
                                  headers={'content-type': 'text/event-stream'})
        return api.handle(request)

    async with httpx.AsyncClient(
        base_url='https://example.test/v1beta1/projects/test-project/locations/global/',
        transport=httpx.MockTransport(handler),
    ) as client:
        client.context_cache = GeminiContextCache(client, config, clock=lambda: api.now)
        client.context_cache.note_user_input()
        client.context_cache.note_user_input()
        messages = [{'role': 'system', 'content': 's' * 5000},
                    {'role': 'user', 'content': 'u' * 3000},
                    {'role': 'assistant', 'content': 'a' * 1000},
                    {'role': 'user', 'content': 'q' * 1000}]
        context = Context('', [], '', 'test', session_id='a')
        emit = AsyncMock()
        dialect = get_dialect('gemini_generate')
        result = await dialect.call(client, cfg, messages, None, context, emit)
        assert result.message.content == 'ok'
        assert len(generated) == 2
        assert generated[1] == await dialect._request(cfg, messages, None, context)
        assert emit.await_count == 1
        assert not api.resources


@pytest.mark.parametrize('style,auth,name,enabled,expected', [
    ('gemini_generate', 'adc', 'gemini-3.8-flash', True, True),
    ('gemini_generate', 'adc', 'gemini-3.8-flash', False, False),
    ('gemini_generate', 'api_key', 'gemini-3.8-flash', True, False),
    ('interactions', 'api_key', 'gemini-3.8-flash', True, False),
    ('chat', 'api_key', 'deepseek-v4-flash', True, False),
    ('responses', 'api_key', 'gpt-5', True, False),
])
def test_cache_is_gated_to_enabled_cloud_gemini(monkeypatch, style, auth, name, enabled, expected):
    from agent.loop import AgentLoop

    monkeypatch.setattr('agent.loop.GeminiClient', lambda **kw: SimpleNamespace(context_cache=None))
    monkeypatch.setattr('agent.loop.AsyncOpenAI', lambda **kw: SimpleNamespace())
    factory = Mock()
    monkeypatch.setattr('dialects.gemini_cache.GeminiContextCache', factory)
    config = ModelConfig(name, 'https://example.test', api_style=style, auth=auth,
                         explicit_cache={'enabled': enabled})
    AgentLoop(config, AsyncMock())
    assert factory.called == expected


@pytest.mark.asyncio
async def test_client_shutdown_removes_owned_resources(tmp_path, cfg):
    api = CacheAPI()

    class Credentials:
        valid = True

        def apply(self, headers):
            headers['authorization'] = 'Bearer synthetic-token'

    client = GeminiClient(
        base_url=cfg.base_url, auth_mode='adc', project=cfg.project,
        credentials=Credentials(), transport=httpx.MockTransport(api.handle))
    client.context_cache = GeminiContextCache(
        client, {'state_path': str(tmp_path / 'cache.json')}, clock=lambda: api.now)
    body = {'systemInstruction': {'parts': [{'text': 's' * 5000}]},
            'contents': [{'role': 'user', 'parts': [{'text': 'q' * 1000}]}]}
    client.context_cache.note_user_input()
    client.context_cache.note_user_input()
    await client.context_cache.prepare(body, cfg, 'a')
    assert api.resources
    await client.close()
    assert not api.resources and client.is_closed


@pytest.mark.asyncio
async def test_stream_failure_after_output_does_not_replay(setup_cache, cfg):
    api, _, _, config = setup_cache
    cfg.stream = True
    generated = []

    def handler(request):
        if request.url.path.endswith(':streamGenerateContent'):
            generated.append(json.loads(request.content))
            chunk = {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'partial'}]}}]}
            return httpx.Response(200, content='data: ' + json.dumps(chunk) + '\n\n')
        return api.handle(request)

    async with httpx.AsyncClient(
        base_url='https://example.test/v1beta1/projects/test-project/locations/global/',
        transport=httpx.MockTransport(handler),
    ) as client:
        client.context_cache = GeminiContextCache(client, config, clock=lambda: api.now)
        client.context_cache.note_user_input()
        client.context_cache.note_user_input()
        messages = [{'role': 'system', 'content': 's' * 5000},
                    {'role': 'user', 'content': 'q' * 1000}]
        emit = AsyncMock(side_effect=GeminiAPIError('callback failed', status=400))
        with pytest.raises(GeminiAPIError):
            await get_dialect('gemini_generate').call(
                client, cfg, messages, None, Context('', [], '', 'test', session_id='a'), emit)
        assert len(generated) == 1 and generated[0].get('cachedContent')


async def test_inactive_first_input_uses_implicit_then_active_creates(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    cache.last_user_at = None
    cache.active_until = 0
    cache.note_user_input()
    original, used = await cache.prepare(body, cfg, 'a')
    assert original == body and not used and not api.requests
    api.now += 60
    cache.note_user_input()
    request, used = await cache.prepare(body, cfg, 'a')
    assert used and request['cachedContent']
    assert cache.state['active']['expires'] == api.now + 600


async def test_delayed_tool_round_never_extends_expiry_or_uses_nearly_expired_cache(setup_cache, cfg, body):
    api, _, cache, _ = setup_cache
    await cache.prepare(body, cfg, 'a')
    expiry = cache.state['active']['expires']
    api.now += 550
    original, used = await cache.prepare(body, cfg, 'a')
    assert original == body and not used
    assert cache.state['active']['expires'] == expiry
    assert api.serial == 1 and not any(m == 'PATCH' for m, _, _ in api.requests)
    api.now += 51
    original, used = await cache.prepare(body, cfg, 'a')
    assert original == body and not used and cache.state['active'] is None
    assert api.serial == 1


async def test_old_two_hour_config_is_still_bounded_by_activity(setup_cache, cfg, body):
    api, client, _, config = setup_cache
    cache = GeminiContextCache(client, {**config, 'ttl_seconds': 7200}, clock=lambda: api.now)
    cache.note_user_input()
    cache.note_user_input()
    api.now += 120  # Work preceding the actual request consumes part of the window.
    await cache.prepare(body, cfg, 'a')
    assert cache.state['active']['expires'] == api.now + 480


async def test_heartbeat_sends_full_history_without_referencing_or_renewing(setup_cache, cfg):
    api, client, cache, _ = setup_cache
    client.context_cache = cache
    messages = [{'role': 'system', 'content': 's' * 5000},
                {'role': 'user', 'content': 'u' * 3000},
                {'role': 'assistant', 'content': 'synthetic answer'},
                {'role': 'user', 'content': 'synthetic heartbeat'}]
    dialect = get_dialect('gemini_generate')
    context = Context('', [], '', 'test', session_id='a', source='heartbeat')
    body = await dialect._request(cfg, messages, None, context)
    await cache.prepare(body, cfg, 'a')
    expiry = cache.state['active']['expires']
    api.now += 100
    before = len(api.requests)
    await dialect.call(client, cfg, messages, None, context, AsyncMock())
    assert api.requests[before:] == [('POST', api.requests[-1][1], body)]
    assert cache.state['active']['expires'] == expiry
    assert cache.state['active']['reuse_count'] == 0


async def test_idle_cleanup_failure_does_not_block_full_request(setup_cache, cfg, body, monkeypatch):
    api, _, cache, _ = setup_cache
    await cache.prepare(body, cfg, 'a')
    api.now += 601
    monkeypatch.setattr(cache, '_save', Mock(side_effect=OSError('synthetic disk failure')))
    original, used = await cache.prepare(body, cfg, 'a')
    assert original == body and not used
