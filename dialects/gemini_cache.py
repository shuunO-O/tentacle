"""Owned Vertex Gemini caches: immutable prefixes, renewal and bounded cleanup.

Only resource metadata and hashes are persisted; prompts remain in session history.
The manager belongs to a Gemini client, never to the stateless dialect instance.
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from runtime.timezone import local_datetime

from dialects.gemini import _check_response

logger = logging.getLogger(__name__)
PREFIX_FIELDS = ('systemInstruction', 'tools', 'toolConfig')
RESOURCE = re.compile(r'projects/[\w-]+/locations/[\w-]+/cachedContents/[\w-]+')


class CleanupPending(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode()).hexdigest()


class GeminiContextCache:
    def __init__(self, client, config, *, clock=time.time):
        self.client = client
        self.clock = clock
        self.path = Path(config.get('state_path', 'data/gemini_explicit_cache.json'))
        self.ttl = int(config.get('ttl_seconds', 600))
        self.threshold = float(config.get('rebuild_below', .90))
        self.target = float(config.get('target_ratio', .99))
        self.minimum = int(config.get('min_tokens', 4096))
        self.min_reuses = int(config.get('min_reuses', 20))
        self.active_window = int(config.get('active_window_seconds', 300))
        self.idle_after = int(config.get('idle_after_seconds', 600))
        if not (60 <= self.ttl <= 86400 and 0 < self.threshold < self.target <= 1
                and self.minimum >= 4096 and self.min_reuses >= 1
                and 0 < self.active_window <= self.idle_after):
            raise ValueError('Gemini explicit_cache 配置无效')
        self.lock = asyncio.Lock()
        self.state = None
        self.reconciled = False
        self.retry_at = 0.0
        self.cleanup_at = 0.0
        # Real user arrivals only; do not infer activity from native user/tool parts.
        self.last_user_at = None
        self.active_until = 0.0

    def _load(self):
        if self.state is not None:
            return
        try:
            state = json.loads(self.path.read_text())
            if (state.get('version') != 1 or not re.fullmatch(r'[a-f0-9]{32}', state['owner'])
                    or not isinstance(state.get('retired'), list)):
                raise ValueError('invalid cache metadata')
            state.pop('checkpoint', None)  # Retire the old daily 23:00 policy.
            self.state = state
        except (OSError, ValueError, KeyError, TypeError):
            self.state = {'version': 1, 'owner': uuid.uuid4().hex,
                          'active': None, 'retired': []}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(self.state, handle)
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def note_user_input(self, timestamp=None):
        now = self.clock() if timestamp is None else timestamp
        if (self.last_user_at is not None and now >= self.last_user_at
                and (now <= self.active_until
                     or now - self.last_user_at <= self.active_window)):
            self.active_until = now + self.idle_after
        else:
            self.active_until = 0.0
        self.last_user_at = now

    def is_active(self):
        now = self.clock()
        return self.last_user_at is not None and self.last_user_at <= now <= self.active_until

    def _deadline(self, *, manual=False):
        # Even an old ttl_seconds=7200 config cannot keep automatic caches alive
        # beyond the existing user activity window. Tool calls never move it.
        if manual:
            return self.clock() + min(self.ttl, self.idle_after)
        return self.last_user_at + min(self.idle_after, self.ttl)

    @staticmethod
    def _expire_time(deadline):
        return datetime.fromtimestamp(deadline, ZoneInfo('UTC')).isoformat()

    async def record_use(self, name, usage):
        """Count completed generations reporting actual use of this resource."""
        if usage is None:
            return
        async with self.lock:
            self._load()
            entry = self.state['active']
            if (not entry or entry['name'] != name
                    or usage.cached_tokens < entry['tokens']
                    or usage.prompt_tokens < entry['tokens']):
                return
            entry['reuse_count'] = entry.get('reuse_count', 0) + 1
            entry['last_prompt_tokens'] = usage.prompt_tokens
            try:
                self._save()
            except OSError as exc:
                # Accounting failure must not hide a successful model response.
                logger.warning('Gemini 缓存复用计数保存失败: %s', type(exc).__name__)

    def _url(self, name):
        if not RESOURCE.fullmatch(name):
            raise ValueError('invalid cache resource name')
        return str(self.client.base_url).split('/projects/', 1)[0].rstrip('/') + '/' + name

    async def _api(self, method, path, **kwargs):
        response = await self.client.request(method, path, timeout=20, **kwargs)
        _check_response(response)
        return response

    def _retire(self, entry):
        if entry and not any(item['name'] == entry['name'] for item in self.state['retired']):
            self.state['retired'].append(dict(entry))

    def _event(self, operation, entry):
        """Resource accounting is separate from generation usage_logs."""
        event = {'time': self.clock(), 'operation': operation, 'name': entry['name'],
                 'tokens': entry.get('tokens', 0), 'expires': entry['expires'],
                 'reuse_count': entry.get('reuse_count', 0)}
        if operation in ('deleted', 'expired') and entry.get('started'):
            event['token_hours'] = entry.get('tokens', 0) * max(
                0, min(self.clock(), entry['expires']) - entry['started']) / 3600
        try:
            with self.path.with_suffix('.events.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(event) + '\n')
        except OSError as exc:
            logger.warning('Gemini 缓存成本记录失败: %s', type(exc).__name__)

    async def _cleanup(self, *, force=False):
        if not force and self.clock() < self.cleanup_at:
            return
        self.cleanup_at = self.clock() + 60
        remaining = []
        for entry in self.state['retired']:
            if self.state['active'] and entry['name'] == self.state['active']['name']:
                continue
            if entry['expires'] <= self.clock():
                self._event('expired', entry)
                continue
            try:
                response = await self.client.delete(self._url(entry['name']), timeout=20)
                if response.status_code not in (404, 410):
                    _check_response(response)
                self._event('deleted', entry)
            except Exception as exc:
                remaining.append(entry)
                logger.warning('Gemini 旧缓存删除待重试: %s', type(exc).__name__)
        self.state['retired'] = remaining
        self._save()

    async def _reconcile(self):
        """Find only caches owned by this installation, including crash orphans."""
        if self.reconciled:
            return
        self._save()  # Persist ownership BEFORE any billable creation.
        active = self.state['active']
        found = None
        page = None
        while True:
            params = {'pageSize': 100}
            if page:
                params['pageToken'] = page
            payload = (await self._api('GET', 'cachedContents', params=params)).json()
            for resource in payload.get('cachedContents', []):
                if resource.get('displayName') != 'tentacle-cache-' + self.state['owner']:
                    continue
                entry = {'name': resource['name'], 'expires': self._expires(resource)}
                if active and entry['name'] == active['name']:
                    found = entry
                else:
                    self._retire(entry)
            page = payload.get('nextPageToken')
            if not page:
                break
        if active:
            if found:
                active['expires'] = found['expires']
            else:
                self._retire(active)
                self.state['active'] = None
        self.reconciled = True
        await self._cleanup(force=True)

    @staticmethod
    def _expires(payload):
        return datetime.fromisoformat(payload['expireTime'].replace('Z', '+00:00')).timestamp()

    @staticmethod
    def _fixed(body):
        return {key: body[key] for key in PREFIX_FIELDS if key in body}

    def _model(self, cfg):
        return f'projects/{cfg.project}/locations/{cfg.location}/publishers/google/models/{cfg.name}'

    def _matches(self, entry, body, cfg, session_id):
        contents = body.get('contents', [])
        count = entry['count']
        return (entry['model'] == self._model(cfg) and entry['session'] == session_id
                and entry['fixed'] == digest(self._fixed(body))
                and count <= len(contents)
                and entry['prefix'] == digest(contents[:count]))

    async def _count(self, body, cfg):
        payload = {'model': self._model(cfg), **self._fixed(body),
                   'contents': body.get('contents', [])}
        # countTokens does not accept toolConfig.
        payload.pop('toolConfig', None)
        result = (await self._api(
            'POST', f'publishers/google/models/{cfg.name}:countTokens', json=payload)).json()
        return int(result['totalTokens'])

    async def _select(self, body, cfg, total):
        contents = body.get('contents', [])
        # Never split a tool cycle or freeze a user message that may be merged later.
        boundaries = [0] + [i + 1 for i, item in enumerate(contents)
                            if item['role'] == 'model'
                            and not any('functionCall' in p for p in item.get('parts', []))]
        low, high, selected = 0, len(boundaries) - 1, None
        counted = {}
        while low <= high:
            middle = (low + high) // 2
            count = boundaries[middle]
            candidate = {**self._fixed(body), 'contents': contents[:count]}
            tokens = await self._count(candidate, cfg)
            counted[count] = tokens
            if tokens <= total * self.target:
                if tokens >= self.minimum:
                    selected = (count, tokens, candidate)
                low = middle + 1
            else:
                high = middle - 1
        if selected is not None and low < len(boundaries):
            # The ratio is a target, not a hard cap. A whole stable message just
            # above 99% is preferable to leaving a large completed tool turn out.
            count = boundaries[low]
            candidate = {**self._fixed(body), 'contents': contents[:count]}
            tokens = counted.get(count)
            if tokens is None:
                tokens = await self._count(candidate, cfg)
            if (tokens >= self.minimum
                    and abs(tokens - total * self.target) < abs(selected[1] - total * self.target)):
                selected = (count, tokens, candidate)
        if selected is None:
            # A single large complete message/system block can exceed the target.
            # Keep whole boundaries rather than truncate signed/native content.
            for count in boundaries:
                candidate = {**self._fixed(body), 'contents': contents[:count]}
                tokens = counted.get(count)
                if tokens is None:
                    tokens = await self._count(candidate, cfg)
                if tokens >= self.minimum:
                    return count, tokens, candidate
        return selected

    async def _create(self, body, cfg, session_id, total, *, force=False):
        if self.state['retired']:
            raise CleanupPending('旧缓存删除未完成，暂缓创建新缓存以避免重复费用')
        selected = await self._select(body, cfg, total)
        if selected is None:
            return False
        count, tokens, candidate = selected
        previous = self.state['active']
        if not force and previous and count <= previous['count'] and tokens <= previous['tokens']:
            return False  # No new stable boundary: never pay to recreate the same prefix.
        deadline = self._deadline(manual=force)
        if deadline - self.clock() < 60:
            return False  # Do not create a resource too close to the idle boundary.
        payload = {**candidate, 'model': self._model(cfg), 'expireTime': self._expire_time(deadline),
                   'displayName': 'tentacle-cache-' + self.state['owner']}
        resource = (await self._api('POST', 'cachedContents', json=payload)).json()
        entry = {'name': resource['name'], 'expires': self._expires(resource),
                 'model': self._model(cfg), 'session': session_id,
                 'fixed': digest(self._fixed(body)), 'prefix': digest(candidate['contents']),
                 'count': count, 'started': self.clock(),
                 'manual': force,
                 'reuse_count': 0, 'last_prompt_tokens': total,
                 'tokens': int(resource.get('usageMetadata', {}).get('totalTokenCount', tokens))}
        self._url(entry['name'])  # Validate before committing metadata.
        previous = self.state['active']
        retired_before = list(self.state['retired'])
        self._retire(previous)
        self.state['active'] = entry
        try:
            self._save()
        except OSError:
            self.state['active'] = previous
            self.state['retired'] = retired_before
            self._retire(entry)
            # If metadata cannot be saved, delete the newly created billable resource.
            await self.client.delete(self._url(entry['name']), timeout=20)
            raise
        self._event('created', entry)
        await self._cleanup(force=True)
        logger.info('Gemini 显式缓存创建: tokens=%d, ttl=%ds', entry['tokens'], self.ttl)
        return True

    async def _renew(self, entry):
        if not self.is_active():
            return
        deadline = self._deadline()
        if deadline - self.clock() < 60 or abs(entry['expires'] - deadline) < 1:
            return
        payload = (await self._api('PATCH', self._url(entry['name']),
                                   params={'updateMask': 'expireTime'},
                                   json={'expireTime': self._expire_time(deadline)})).json()
        entry['expires'] = self._expires(payload)
        self._save()
        self._event('renewed', entry)

    @staticmethod
    def _reference(body, entry):
        result = copy.deepcopy(body)
        result['contents'] = result.get('contents', [])[entry['count']:]
        for key in PREFIX_FIELDS:
            result.pop(key, None)
        result['cachedContent'] = entry['name']
        return result

    def _usable(self, body, cfg, session_id):
        entry = self.state['active']
        if (entry and entry['expires'] - self.clock() >= 60
                and self._matches(entry, body, cfg, session_id)):
            return entry
        return None

    async def _invalidate(self):
        self._retire(self.state['active'])
        self.state['active'] = None
        self._save()
        await self._cleanup(force=True)

    async def clear(self):
        async with self.lock:
            self._load()
            try:
                await self._reconcile()
                await self._invalidate()
            except Exception as exc:
                # Keep names for next startup/request; TTL still caps orphan cost.
                logger.warning('Gemini 缓存清理失败: %s', type(exc).__name__)
                self._retire(self.state['active'])
                self.state['active'] = None
                try:
                    self._save()
                except OSError:
                    logger.warning('Gemini 缓存清理元数据无法保存')

    async def clear_if_idle(self):
        """Heartbeats may clean up, but cannot create, reference or renew a cache."""
        if not self.is_active():
            self._load()
            if self.path.exists() or self.state['active'] or self.state['retired']:
                await self.clear()

    async def prepare(self, body, cfg, session_id, *, force=False):
        """Return a cache reference plus suffix, or the original full request."""
        async with self.lock:
            self._load()
            entry = self.state['active']
            if not force and not self.is_active() and not (entry and entry.get('manual')):
                # Inactive first messages use implicit caching. Retire previous
                # policy resources as well, including after a process restart.
                if self.path.exists() or entry or self.state['retired']:
                    try:
                        await self._reconcile()
                        await self._invalidate()
                    except asyncio.CancelledError:
                        self.reconciled = False
                        raise
                    except Exception as exc:
                        self.reconciled = False
                        logger.warning('Gemini 空闲缓存清理失败，使用完整请求: %s', type(exc).__name__)
                return body, False
            if not force and self.clock() < self.retry_at:
                entry = self._usable(body, cfg, session_id)
                if entry:
                    return self._reference(body, entry), True
                if self.state['active']:
                    try:
                        await self._invalidate()
                    except OSError:
                        logger.warning('Gemini 缓存失效元数据无法保存')
                return body, False
            try:
                await self._reconcile()
                await self._cleanup()
                entry = self.state['active']
                if entry and (entry['expires'] <= self.clock()
                              or not self._matches(entry, body, cfg, session_id)):
                    await self._invalidate()
                    entry = None
                if not force and not self.is_active():
                    if entry and entry['expires'] - self.clock() >= 60:
                        return self._reference(body, entry), True
                    return body, False
                # Recent successful generation usage is a cheap coverage signal.
                # Confirm with countTokens only when a paid rebuild is eligible.
                check = (entry and self.is_active()
                         and entry.get('reuse_count', 0) >= self.min_reuses
                         and entry['tokens'] / max(1, entry.get('last_prompt_tokens', 0)) < self.threshold)
                if force or not entry or check:
                    total = await self._count(body, cfg)
                    rebuild = force or not entry or entry['tokens'] / max(1, total) < self.threshold
                    created = False
                    if rebuild and total >= self.minimum:
                        created = await self._create(body, cfg, session_id, total, force=force)
                    if not entry and not created:
                        self.retry_at = self.clock() + 300
                    if force and not created:
                        return body, False
                entry = self.state['active']
                if not entry:
                    return body, False
                await self._renew(entry)
                if entry['expires'] - self.clock() < 60:
                    return body, False
                return self._reference(body, entry), True
            except asyncio.CancelledError:
                # Creation can succeed server-side even if the response is cancelled.
                self.reconciled = False
                raise
            except Exception as exc:
                self.retry_at = self.clock() + 300
                self.reconciled = False
                logger.warning('Gemini 显式缓存暂不可用，使用完整请求: %s', type(exc).__name__)
                if force:
                    if isinstance(exc, CleanupPending):
                        raise RuntimeError(str(exc)) from None
                    raise RuntimeError('缓存创建失败，请检查缓存 API 权限或网络；正常对话仍可继续') from None
                entry = self._usable(body, cfg, session_id)
                if entry:
                    return self._reference(body, entry), True
                return body, False

    def status(self):
        self._load()
        entry = self.state['active']
        if not entry or entry['expires'] <= self.clock():
            return '没有有效的显式缓存'
        expiration = local_datetime(entry['expires'])
        activity = '活跃' if self.is_active() else '空闲'
        return (f"显式缓存 {entry['tokens']} tokens，成功复用 {entry.get('reuse_count', 0)} 次"
                f"（重建至少 {self.min_reuses} 次），交流状态：{activity}，"
                f"到期时间 {expiration:%m-%d %H:%M}（北京时间）")
