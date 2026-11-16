"""Release validation and switching with synthetic bundles and mock systemd."""

import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.error

import pytest

pytest.importorskip('pwd')

from scripts import (
    check_updates,
    deploy,
    github_releases,
    install_update,
    release_package,
    update,
    update_runtime,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    repo, prod, status, backups = tmp_path / 'repo', tmp_path / 'production', tmp_path / 'status', tmp_path / 'releases'
    for path in (repo, prod, status):
        path.mkdir()

    def git(*args):
        return deploy.git(repo, *args)

    git('init', '-q')
    git('config', 'user.name', 'Synthetic test')
    git('config', 'user.email', 'test@example.invalid')

    def put(name, text):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    for name, text in {'run_web.py': 'VALUE = "old"\n', 'protocol.py': 'VALUE = 2\n',
                       'steady.py': 'STEADY = 1\n', 'deleted.py': 'OLD = 1\n',
                       'requirements.txt': 'example==1.0\n', 'config.yaml': 'synthetic: template\n',
                       'prompt/example.md': 'synthetic prompt', 'subagents/Dockerfile': 'FROM synthetic\n',
                       'subagents/.dockerignore': 'synthetic-ignore\n'}.items():
        put(name, text)

    put('runtime/paths.py', '# synthetic application path module\n')
    put(release_package.DATA_EPOCH_FILE, '1\n')
    put('config.defaults.yaml', 'synthetic: true\n')
    for name in release_package.PUBLIC_PROMPTS:
        put(name, 'public fixture\n')

    def make(version):
        git('add', '.')
        git('commit', '--allow-empty', '-qm', version)
        git('tag', '-a', version, '-m', version)
        directory = tmp_path / ('bundle-' + version)
        release_package.build(repo, version, directory)
        manifest = json.loads((directory / 'manifest.json').read_bytes())
        snapshot = {'repository': 'example/repo', 'release_id': 1, **update.identity(manifest),
                    'published_at': '2026-10-17T00:00:00Z', 'immutable': True, 'assets': []}
        return SimpleNamespace(directory=directory, manifest=manifest, snapshot=snapshot)

    def root_directory(path, mode=0o755):
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(update_runtime, 'root_directory', root_directory)
    build = Mock()
    monkeypatch.setattr(update, 'check_python', Mock())
    monkeypatch.setattr(update, 'run_build', build)
    host = {'production': str(prod), 'backups': str(backups), 'python': '/usr/bin/python3',
            'user': 'tentacle', 'home': str(tmp_path / 'home'), 'config': str(tmp_path / 'config/update.json'),
            'schema': 2, 'service': 'tentacle.service', 'health_port': 8420}
    base = make('v1.0')
    current = update.bundle_record(base.directory, base.snapshot)
    update.build_release(host, base.directory, current)
    update.switch_release(prod, current)
    before = {'schema': update.UPDATER_PROTOCOL, 'current': current}
    update.publish_state(backups, status, before)
    build.reset_mock()
    home = Path(host['home'])
    home.mkdir()
    (home / '.env').write_bytes(b'SYNTHETIC_PRIVATE=preserved\n')
    control, healthy = Mock(), Mock()
    monkeypatch.setattr(update_runtime, 'require_running', Mock())
    monkeypatch.setattr(update_runtime, 'require_stopped', Mock())
    monkeypatch.setattr(update_runtime, 'systemctl', control)
    monkeypatch.setattr(update_runtime, 'wait_healthy', healthy)
    monkeypatch.setattr(update_runtime, 'http_status', lambda _: 403)

    def prepare(bundle):
        target = update.bundle_record(bundle.directory, bundle.snapshot)
        update.build_release(host, bundle.directory, target)
        return target

    return SimpleNamespace(repo=repo, prod=prod, status=status, backups=backups, host=host, home=home,
                           git=git, put=put, make=make, base=base, before=before, prepare=prepare,
                           build=build, control=control, healthy=healthy, tmp=tmp_path)


def test_build_new_dependencies_before_switch_and_revert_offline(workspace):
    w = workspace
    w.put('run_web.py', 'VALUE = "new"\n')
    w.put('requirements.txt', 'example==2.0\n')
    target = w.prepare(w.make('v1.1'))
    assert w.build.call_count == 3
    w.control.assert_not_called()
    assert (w.prod / 'current/run_web.py').read_text() == 'VALUE = "old"\n'
    assert update.activate(w.host, w.status, w.before, target)['version'] == 'v1.1'
    assert (w.prod / 'current/run_web.py').read_text() == 'VALUE = "new"\n'
    assert (w.home / '.env').read_bytes() == b'SYNTHETIC_PRIVATE=preserved\n'
    state = update.read_state(w.host, w.status)
    assert state['previous'] == w.before['current']
    update.activate(w.host, w.status, state, state['previous'])
    assert update.public_status(w.status)['version'] == 'v1.0'
    assert not (w.backups / update_runtime.PENDING_FILE).exists()


def test_build_failure_leaves_running_version_untouched_and_can_retry(workspace):
    w = workspace
    bundle = w.make('v1.1')
    w.build.side_effect = ValueError('synthetic dependency failure')
    with pytest.raises(ValueError, match='dependency failure'):
        w.prepare(bundle)
    assert update.read_state(w.host, w.status) == w.before
    w.control.assert_not_called()
    w.build.side_effect = None
    target = w.prepare(bundle)
    assert update.installed_release(w.prod, target).is_dir()


def test_failed_candidate_start_restores_original_release(workspace):
    w = workspace
    target = w.prepare(w.make('v1.1'))
    w.healthy.side_effect = [ValueError('synthetic startup failure'), None]
    with pytest.raises(ValueError, match='已恢复先前版本'):
        update.activate(w.host, w.status, w.before, target)
    assert update.read_state(w.host, w.status) == w.before
    assert w.healthy.call_count == 2


def test_failed_restoration_can_be_recovered_offline(workspace):
    w = workspace
    target = w.prepare(w.make('v1.1'))
    w.healthy.side_effect = ValueError('synthetic startup failure')
    with pytest.raises(ValueError, match='--recover'):
        update.activate(w.host, w.status, w.before, target)
    with pytest.raises(ValueError, match='--recover'):
        update.read_state(w.host, w.status)
    w.healthy.side_effect = None
    assert update.recover_locked(w.host, w.status)['recovered']
    assert update.read_state(w.host, w.status) == w.before
    assert update.recover_locked(w.host, w.status) == {'recovered': False}


def test_cross_epoch_switch_is_blocked_before_stop(workspace):
    w = workspace
    w.put(release_package.DATA_EPOCH_FILE, '2\n')
    target = w.prepare(w.make('v2.0'))
    with pytest.raises(ValueError, match='数据兼容代号'):
        update.activate(w.host, w.status, w.before, target)
    w.control.assert_not_called()


def test_unpinned_requirements_cannot_be_installed(workspace):
    w = workspace
    w.put('requirements.txt', 'example>=2.0\n')
    bundle = w.make('v1.1')
    with pytest.raises(ValueError, match='固定版本'):
        w.prepare(bundle)
    w.build.assert_not_called()
    w.control.assert_not_called()


def test_existing_version_cannot_be_replaced_with_different_release(workspace):
    w = workspace
    with pytest.raises(ValueError, match='版本目录或部署记录'):
        update.build_release(w.host, w.base.directory, {**w.before['current'], 'bundle_sha256': 'a' * 64})
    assert update.read_state(w.host, w.status) == w.before


def test_version_and_checker_never_activate(workspace, monkeypatch, capsys):
    w = workspace
    assert update.main(['version', '--status-root', str(w.status)]) == 0
    assert capsys.readouterr().out.strip() == 'v1.0'
    bundle = w.make('v1.1')
    client = Mock()
    client.snapshot.return_value = bundle.snapshot

    def download(_, directory):
        shutil.copytree(bundle.directory, directory)
        return bundle.snapshot

    client.download.side_effect = download
    monkeypatch.setattr(check_updates.github_releases, 'Client', lambda *_: client)
    monkeypatch.setattr(check_updates.runtime, 'host_config', lambda _: w.host)
    monkeypatch.setattr(update, 'load_config', lambda _: {'repository': 'example/repo', 'token_file': 'unused'})
    result = check_updates.check(w.status)
    assert result['ok'] and result['available'] and result['data_compatible']
    w.build.assert_not_called()
    w.control.assert_not_called()
    assert update.read_state(w.host, w.status) == w.before


@pytest.mark.parametrize('bad', ['world_readable', 'empty', 'symlink'])
def test_token_file_requires_private_regular_nonempty_file(tmp_path, bad):
    path = tmp_path / 'token'
    path.write_text('github_pat_synthetic')
    path.chmod(0o600)
    if bad == 'world_readable':
        path.chmod(0o644)
    elif bad == 'empty':
        path.write_text('')
    else:
        link = tmp_path / 'link'
        link.symlink_to(path)
        path = link
    with pytest.raises(ValueError):
        github_releases.read_token(path)


def test_client_without_token_omits_authorization_header_and_keeps_host_checks():
    client = github_releases.Client('example/repo')
    requests = []

    def request(req, timeout):
        requests.append(req)
        response = io.BytesIO(b'[]')
        response.headers = {'Content-Length': '2'}
        return response

    with patch.object(client.opener, 'open', side_effect=request):
        assert client.api('releases?per_page=100&page=1') == []
    assert requests[0].get_header('Authorization') is None
    with pytest.raises(ValueError, match='GitHub API'):
        client.request('https://evil.example/repos/example/repo/releases', 100)


def test_redirect_drops_authorization_and_rejects_non_github_cdn(tmp_path):
    path = tmp_path / 'token'
    path.write_text('github_pat_synthetic')
    path.chmod(0o600)
    client = github_releases.Client('example/repo', path)
    requests = []

    def request(req, timeout):
        requests.append(req)
        if len(requests) == 1:
            raise urllib.error.HTTPError(req.full_url, 302, 'redirect',
                                         {'Location': 'https://release-assets.githubusercontent.com/synthetic'}, io.BytesIO())
        response = io.BytesIO(b'package')
        response.headers = {'Content-Length': '7'}
        return response
    with patch.object(client.opener, 'open', side_effect=request):
        assert client.request('https://api.github.com/repos/example/repo/releases/assets/1', 7, binary=True, redirect=True) == b'package'
    assert requests[0].get_header('Authorization') == 'Bearer github_pat_synthetic'
    assert requests[1].get_header('Authorization') is None
    redirect = urllib.error.HTTPError('https://api.github.com/synthetic', 302, 'redirect',
                                      {'Location': 'https://evil.example/token'}, io.BytesIO())
    with patch.object(client.opener, 'open', side_effect=redirect) as opener:
        with pytest.raises(ValueError, match='CDN'):
            client.request('https://api.github.com/repos/example/repo/releases/assets/1', 7, binary=True, redirect=True)
        assert opener.call_count == 1


def test_latest_uses_numeric_versions_and_excludes_drafts_and_prereleases(tmp_path):
    client = github_releases.Client('example/repo', tmp_path / 'unused-token')
    releases = [{'id': index + 1, 'tag_name': version, 'draft': draft, 'prerelease': prerelease,
                 'published_at': '2026-10-17T00:00:00Z'} for index, (version, draft, prerelease) in enumerate([
                     ('v1.9', False, False), ('v1.10', False, False), ('v2.0', True, False), ('v3.0', False, True)])]
    with patch.object(client, 'api', return_value=releases):
        assert client.select()['tag_name'] == 'v1.10'
    with patch.object(client, 'api', return_value=releases[2]):
        with pytest.raises(ValueError, match='正式发布'):
            client.select('v2.0')


@pytest.mark.parametrize('failure', [None, 'asset', 'tag', 'draft', 'commit'])
def test_download_validates_real_bundle_against_simulated_github_source(workspace, failure):
    w = workspace
    bundle = w.base
    assets = [{'name': p.name, 'id': i + 1, 'size': p.stat().st_size,
               'digest': 'sha256:' + hashlib.sha256(p.read_bytes()).hexdigest(), 'state': 'uploaded'}
              for i, p in enumerate(sorted(bundle.directory.iterdir()))]
    release = {'id': 123, 'tag_name': 'v1.0', 'draft': failure == 'draft', 'prerelease': False,
               'published_at': '2026-10-17T00:00:00Z', 'assets': assets, 'immutable': True}
    client = github_releases.Client('example/repo', w.tmp / 'unused-token')

    def api(path):
        if path == 'git/ref/tags/v1.0':
            return {'object': {'type': 'tag', 'sha': '0' * 40 if failure == 'tag' else bundle.manifest['tag_object']}}
        if path.startswith('git/tags/'):
            return {'object': {'type': 'commit', 'sha': '0' * 40 if failure == 'commit' else bundle.manifest['commit']}}
        if path == 'releases/tags/v1.0':
            return release
        raise AssertionError(path)

    def request(url, *args, **kwargs):
        index = int(url.rsplit('/', 1)[1])
        data = (bundle.directory / assets[index - 1]['name']).read_bytes()
        return b'broken' if failure == 'asset' else data
    with patch.object(client, 'api', side_effect=api), patch.object(client, 'request', side_effect=request):
        if failure:
            with pytest.raises(ValueError):
                client.download(release, w.tmp / 'download')
        else:
            result = client.download(release, w.tmp / 'download')
            assert result['commit'] == bundle.manifest['commit']
            assert release_package.check(w.tmp / 'download')['ok']
    w.control.assert_not_called()


def _rewrite_bundle_archive_and_hashes(bundle_dir, extra_git_files=None):
    manifest_path = bundle_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    additions = extra_git_files or {}
    for path, data in additions.items():
        manifest['files'].append({
            'path': path,
            'sha256': release_package.digest(data),
            'size': len(data),
            'mode': 0o644,
            'git_blob': 'b' * 40,
            'origin': 'git',
        })
    manifest_bytes = release_package.json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)

    archive_path = next(bundle_dir.glob('*.tar.gz'))
    members = []
    with tarfile.open(archive_path) as archive:
        for member in archive:
            with archive.extractfile(member) as stream:
                data = manifest_bytes if member.name == 'manifest.json' else stream.read()
            member.size = len(data)
            members.append((member, data))
    for path, data in additions.items():
        member = tarfile.TarInfo('app/' + path)
        member.mode = 0o644
        member.size = len(data)
        members.append((member, data))
    with tarfile.open(archive_path, 'w:gz') as archive:
        for member, data in members:
            archive.addfile(member, io.BytesIO(data))
    (bundle_dir / 'SHA256SUMS').write_text(''.join(
        f'{release_package.digest(path.read_bytes())}  {path.name}\n'
        for path in sorted(bundle_dir.iterdir()) if path.name != 'SHA256SUMS'
    ))
    return manifest


def test_download_accepts_frontend_and_future_paths_while_rejecting_sensitive_files(workspace, tmp_path):
    w = workspace
    static_dir = tmp_path / 'static'
    static_dir.mkdir()
    (static_dir / 'index.html').write_bytes(b'<!doctype html><html><body>ok</body></html>\n')
    out = tmp_path / 'bundle-v1.1'
    release_package.build(w.repo, 'v1.0', out, static_dir=static_dir)

    future_dir = tmp_path / 'future-bundle'
    shutil.copytree(out, future_dir)
    manifest = _rewrite_bundle_archive_and_hashes(
        future_dir,
        extra_git_files={
            'future_feature/handler.py': b'VALUE = 7\n',
            'scripts/future_cli.py': b'CLI = 7\n',
        },
    )

    def make_client(target_dir):
        assets = [{'name': p.name, 'id': i + 1, 'size': p.stat().st_size,
                   'digest': 'sha256:' + hashlib.sha256(p.read_bytes()).hexdigest(), 'state': 'uploaded'}
                  for i, p in enumerate(sorted(target_dir.iterdir()))]
        release = {'id': 200, 'tag_name': 'v1.0', 'draft': False, 'prerelease': False,
                   'published_at': '2026-10-17T00:00:00Z', 'assets': assets, 'immutable': True}
        client = github_releases.Client('example/repo', w.tmp / 'unused-token-future')

        def api(path):
            if path == 'git/ref/tags/v1.0':
                return {'object': {'type': 'tag', 'sha': manifest['tag_object']}}
            if path.startswith('git/tags/'):
                return {'object': {'type': 'commit', 'sha': manifest['commit']}}
            if path == 'releases/tags/v1.0':
                return release
            raise AssertionError(path)

        def request(url, *args, **kwargs):
            index = int(url.rsplit('/', 1)[1])
            return (target_dir / assets[index - 1]['name']).read_bytes()

        return client, release, api, request

    client, release, api, request = make_client(future_dir)
    with patch.object(client, 'api', side_effect=api), patch.object(client, 'request', side_effect=request):
        result = client.download(release, tmp_path / 'dl-future-ok')
        assert result['commit'] == manifest['commit']

    bad_dir = tmp_path / 'bad-sensitive'
    shutil.copytree(w.base.directory, bad_dir)
    _rewrite_bundle_archive_and_hashes(bad_dir, extra_git_files={'config.yaml': b'leak: true\n'})
    client_bad, release_bad, api_bad, req_bad = make_client(bad_dir)
    with patch.object(client_bad, 'api', side_effect=api_bad), patch.object(client_bad, 'request', side_effect=req_bad):
        with pytest.raises(ValueError, match='应用文件路径'):
            client_bad.download(release_bad, tmp_path / 'dl-bad-sensitive')


def test_change_version_syncs_host_tools_and_blocks_protocol_bump(workspace, tmp_path, monkeypatch):
    w = workspace
    source_root = Path(__file__).resolve().parents[1]
    host_prefix = tmp_path / 'usr-local'
    install_update.install(source_root, host_prefix)
    backups_initial = list((host_prefix / 'libexec/tentacle-update-backups').iterdir())
    update_runtime.write_json(
        host_prefix / 'libexec/tentacle-update',
        'install.json',
        {'protocol': update.UPDATER_PROTOCOL, 'source_commit': w.base.manifest['commit']},
        0o644,
    )

    for name in deploy.UPDATER_FILES:
        w.put('scripts/' + name, (source_root / 'scripts' / name).read_text(encoding='utf-8'))
    w.put('runtime/maintenance_protocol.py', (source_root / 'runtime/maintenance_protocol.py').read_text(encoding='utf-8'))
    w.put('scripts/update.py', (w.repo / 'scripts/update.py').read_text(encoding='utf-8') + '\n# v1.1-host-update\n')
    w.put('scripts/tentacle.sh', (source_root / 'scripts/tentacle.sh').read_text(encoding='utf-8') + '\n# v1.1-host-update\n')
    (w.repo / 'scripts/tentacle.sh').chmod(0o755)
    v1_1 = w.make('v1.1')

    w.put('run_web.py', 'VALUE = "v1.2"\n')
    v1_2 = w.make('v1.2')

    w.put(
        'scripts/update.py',
        (w.repo / 'scripts/update.py').read_text(encoding='utf-8').replace(
            f'UPDATER_PROTOCOL = {update.UPDATER_PROTOCOL}',
            f'UPDATER_PROTOCOL = {update.UPDATER_PROTOCOL + 1}',
            1,
        ),
    )
    v1_3 = w.make('v1.3')

    bundles = {'v1.1': v1_1, 'v1.2': v1_2, 'v1.3': v1_3}

    class FakeClient:
        def __init__(self, *args):
            pass

        def select(self, version=None):
            return version or 'v1.3'

        def snapshot(self, version):
            return bundles[version].snapshot

        def download(self, version, directory):
            shutil.copytree(bundles[version].directory, directory)
            return bundles[version].snapshot

        def assert_unchanged(self, snapshot):
            pass

    monkeypatch.setattr(update.github_releases, 'Client', FakeClient)
    monkeypatch.setattr(update, 'load_config', lambda _: {'repository': 'example/repo', 'token_file': 'unused'})
    monkeypatch.setattr(check_updates.github_releases, 'Client', FakeClient)
    monkeypatch.setattr(check_updates.runtime, 'host_config', lambda _: w.host)

    result_1 = update.change_version(w.status, w.host, 'update', 'v1.1', confirm=lambda _: True, host_prefix=host_prefix)
    assert result_1['version'] == 'v1.1' and result_1['host_tools_updated'] is True
    assert '# v1.1-host-update' in (host_prefix / 'libexec/tentacle-update/update.py').read_text(encoding='utf-8')
    assert '# v1.1-host-update' in (host_prefix / 'bin/tentacle').read_text(encoding='utf-8')
    assert update_runtime.read_json(host_prefix / 'libexec/tentacle-update', 'install.json')['version'] == 'v1.1'
    backups_after_1 = list((host_prefix / 'libexec/tentacle-update-backups').iterdir())
    assert len(backups_after_1) == len(backups_initial) + 1

    result_2 = update.change_version(w.status, w.host, 'update', 'v1.2', confirm=lambda _: True, host_prefix=host_prefix)
    assert result_2['version'] == 'v1.2' and 'host_tools_updated' not in result_2
    assert list((host_prefix / 'libexec/tentacle-update-backups').iterdir()) == backups_after_1

    checked = check_updates.check(w.status)
    assert checked['ok'] is True and checked['available'] is True and checked['updater_compatible'] is False
    w.control.reset_mock()
    w.build.reset_mock()
    with pytest.raises(ValueError, match='维护协议'):
        update.change_version(w.status, w.host, 'update', 'v1.3', confirm=lambda _: True, host_prefix=host_prefix)
    w.control.assert_not_called()
    w.build.assert_not_called()
