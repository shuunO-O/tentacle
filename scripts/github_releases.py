"""GitHub Release 只读下载；令牌仅发送给 api.github.com，不随 CDN 重定向转发。"""

import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from . import release_package
else:
    import release_package


API = 'https://api.github.com'
MAX_DOWNLOAD = 64 * 1024 * 1024


def version_key(value):
    match = release_package.VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError('版本须为 v1.0、v1.0.1 或 v1.1')
    return tuple(int(part or 0) for part in match.groups())


def read_token(path):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('GitHub 令牌路径不能包含符号链接')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ValueError(f'请先在 {path} 配置仅限该私有仓库 Contents:read 的令牌') from None
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in {0, os.geteuid()}:
            raise ValueError('GitHub 令牌文件须由当前维护用户或 root 所有，权限 600')
        token = source.read(4097).strip()
    if not token or len(token) > 4096 or not re.fullmatch(rb'[A-Za-z0-9_]+', token):
        raise ValueError('GitHub 令牌文件为空或格式不符；请在主机填写，不在聊天中发送')
    return token.decode('ascii')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, repository, token_file=None, timeout=20):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository):
            raise ValueError('GitHub 仓库名格式不符')
        self.repository = repository
        self.token_file = Path(token_file) if token_file is not None else None
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, url, limit, binary=False, authenticated=True, redirect=False):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError('下载 URL 格式不符')
        if authenticated and (parsed.hostname != 'api.github.com' or not parsed.path.startswith(f'/repos/{self.repository}/')):
            raise ValueError('拒绝向非指定 GitHub API 发送认证')
        if not authenticated and not (parsed.hostname or '').endswith('.githubusercontent.com'):
            raise ValueError('附件 CDN 主机不在允许范围')
        headers = {'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
                   'User-Agent': 'Tentacle-Updater/2', 'X-GitHub-Api-Version': '2022-11-28'}
        if authenticated and self.token_file is not None:
            headers['Authorization'] = 'Bearer ' + read_token(self.token_file)
        try:
            response = self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)
        except urllib.error.HTTPError as error:
            try:
                location = error.headers.get('Location') if error.code == 302 and redirect else None
                status = error.code
            finally:
                error.close()
            if location:
                return self.request(location, limit, binary=True, authenticated=False)
            raise ValueError(f'GitHub 请求失败（HTTP {status}）；检查正式发布状态、网络或按需配置 --token-file') from None
        except (OSError, urllib.error.URLError):
            raise ValueError('GitHub 网络请求失败；未操作生产服务') from None
        with response:
            length = response.headers.get('Content-Length')
            if length and int(length) > limit:
                raise ValueError('GitHub 响应超过大小上限')
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('GitHub 响应超过大小上限')
        return data

    def api(self, path):
        return json.loads(self.request(f'{API}/repos/{self.repository}/{path}', 2 * 1024 * 1024))

    def select(self, version=None):
        if version:
            version_key(version)
            value = self.api('releases/tags/' + version)
        else:
            values = []
            for page in range(1, 11):
                batch = self.api(f'releases?per_page=100&page={page}')
                if not isinstance(batch, list):
                    raise ValueError('GitHub Release 列表格式不符')
                values.extend(batch)
                if len(batch) < 100:
                    break
            else:
                raise ValueError('版本列表过长，请明确指定版本')
            stable = [item for item in values if not item.get('draft') and not item.get('prerelease')
                      and item.get('published_at') and release_package.VERSION_RE.fullmatch(item.get('tag_name', ''))]
            if not stable:
                raise ValueError('仓库尚无已正式发布的稳定 Release；草稿不会用于生产更新')
            value = max(stable, key=lambda item: version_key(item['tag_name']))
        if value.get('draft') is not False or value.get('prerelease') is not False or not value.get('published_at'):
            raise ValueError('更新目标必须是已正式发布的稳定 Release')
        version_key(value['tag_name'])
        if version and value['tag_name'] != version:
            raise ValueError('GitHub 返回的版本与请求不符')
        if not isinstance(value.get('id'), int) or value['id'] <= 0:
            raise ValueError('Release ID 格式不符')
        return value

    def identity(self, release):
        version = release['tag_name']
        version_key(version)
        obj = self.api('git/ref/tags/' + version).get('object', {})
        if obj.get('type') != 'tag' or not re.fullmatch(r'[0-9a-f]{40}', obj.get('sha', '')):
            raise ValueError('远端版本必须是附注标签')
        tag = self.api('git/tags/' + obj['sha']).get('object', {})
        if tag.get('type') != 'commit' or not re.fullmatch(r'[0-9a-f]{40}', tag.get('sha', '')):
            raise ValueError('附注标签必须直接指向提交')
        return {'version': version, 'commit': tag['sha'], 'tag_object': obj['sha']}

    def snapshot(self, release):
        assets = release.get('assets', [])
        return {'repository': self.repository, 'release_id': release['id'], **self.identity(release),
                'published_at': release['published_at'], 'immutable': release.get('immutable', False),
                'assets': sorted(({'name': item['name'], 'id': item['id'], 'size': item['size'],
                                  'digest': item.get('digest'), 'state': item.get('state')} for item in assets),
                                 key=lambda item: item['name'])}

    def assert_unchanged(self, snapshot):
        if self.snapshot(self.select(snapshot['version'])) != snapshot:
            raise ValueError('GitHub Release、标签或附件在审阅后发生变化；未停服，请重新预览')

    def download(self, release, directory):
        if release.get('draft') is not False or release.get('prerelease') is not False or not release.get('published_at'):
            raise ValueError('只下载已正式发布的稳定 Release')
        snapshot = self.snapshot(release)
        version = snapshot['version']
        expected = release_package.METADATA | {'SHA256SUMS', f'tentacle-{version}.tar.gz'}
        assets = snapshot['assets']
        if len(assets) != len(expected) or {asset['name'] for asset in assets} != expected:
            raise ValueError('Release 附件集合不完整或包含额外文件')
        if sum(asset['size'] for asset in assets) > MAX_DOWNLOAD:
            raise ValueError('Release 下载总大小超过上限')
        directory.mkdir(mode=0o700)
        for asset in assets:
            if (not isinstance(asset['id'], int) or asset['id'] <= 0 or asset['state'] != 'uploaded'
                    or not isinstance(asset['size'], int) or not 0 < asset['size'] <= MAX_DOWNLOAD
                    or not isinstance(asset['digest'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset['digest'])):
                raise ValueError('Release 附件状态、大小或摘要格式不符')
            data = self.request(f'{API}/repos/{self.repository}/releases/assets/{asset["id"]}',
                                asset['size'], binary=True, redirect=True)
            if len(data) != asset['size'] or 'sha256:' + release_package.digest(data) != asset['digest']:
                raise ValueError('Release 附件与 GitHub 摘要不符')
            path = directory / asset['name']
            path.write_bytes(data)
            path.chmod(0o600)
        checked = release_package.check(directory)
        if checked['kind'] != 'release' or checked['version'] != version or checked['commit'] != snapshot['commit']:
            raise ValueError('附件中的版本与 GitHub 标签不符')
        manifest = json.loads((directory / 'manifest.json').read_bytes())
        if manifest['tag_object'] != snapshot['tag_object']:
            raise ValueError('附件中的标签对象与 GitHub 不符')
        self.assert_unchanged(snapshot)
        return snapshot
