"""发布包使用临时 Git/合成内容；发布 API 全部模拟，不联网、不访问服务。"""

import io
import json
import subprocess
import tarfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import deploy, publish_release, release_package


@pytest.fixture
def candidate(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return deploy.git(repo, *args)

    git('init', '-q')
    git('config', 'user.name', 'Synthetic test')
    git('config', 'user.email', 'test@example.invalid')
    sources = {'run_web.py': 'VALUE = 1\n', 'protocol.py': 'VALUE = 2\n',
               'requirements.txt': 'example>=1\n', 'start.sh': '#!/bin/sh\nexit 0\n',
               'tools/example.py': 'VALUE = 3\n', 'web/static/app.js': 'const value = 1;\n',
               'scripts/rollback.py': '# synthetic maintenance entry\n',
               'config/files.py': '# synthetic configuration package\n',
               'attachments/documents.py': '# synthetic document worker\n',
               'agent/protocol.py': '# synthetic protocol types\n',
               'runtime/tool_registry.py': '# synthetic shared tools\n',
               'scripts/probe_secret.py': 'SYNTHETIC_PROBE = True\n',
               'dev/preview.py': '# synthetic development code\n',
               'config.defaults.yaml': 'synthetic: versioned-defaults\n',
               'config.yaml': 'synthetic: excluded-config\n', '.env': 'SYNTHETIC_SECRET=excluded-env\n',
               'prompt/example.md': 'excluded-prompt', 'context.md': 'excluded-context',
               'skills/example/SKILL.md': 'excluded-skill', 'data/example.txt': 'excluded-data',
               'subagents/Dockerfile': 'FROM synthetic\n'}
    for name, content in sources.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (repo / 'start.sh').chmod(0o755)
    git('add', '.')
    git('update-index', '--chmod=+x', 'start.sh')
    git('commit', '-qm', 'synthetic application')
    commit = git('rev-parse', 'HEAD').decode().strip()
    git('tag', '-a', 'v1.0', '-m', 'synthetic baseline')
    return SimpleNamespace(repo=repo, git=git, commit=commit, output=tmp_path / 'bundle')


def build(candidate, output=None):
    return release_package.build(candidate.repo, 'v1.0', output or candidate.output)


def rehash(directory):
    (directory / 'SHA256SUMS').write_text(''.join(
        f'{release_package.digest(path.read_bytes())}  {path.name}\n'
        for path in sorted(directory.iterdir()) if path.name != 'SHA256SUMS'))


def rewrite_archive(directory, mutate):
    path = next(directory.glob('*.tar.gz'))
    members = []
    with tarfile.open(path) as archive:
        for member in archive:
            with archive.extractfile(member) as stream:
                members.append((member, stream.read()))
    mutate(members)
    with tarfile.open(path, 'w:gz') as archive:
        for member, data in members:
            archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
    rehash(directory)


def test_package_uses_exact_blobs_and_excludes_private_and_development_files(candidate):
    (candidate.repo / 'run_web.py').write_text('DIRTY WORKTREE !!!\n')
    (candidate.repo / 'untracked.py').write_text('UNTRACKED !!!\n')
    result = build(candidate)
    assert result['commit'] == candidate.commit
    manifest = json.loads((candidate.output / 'manifest.json').read_text())
    assert {item['path'] for item in manifest['files']} == {
        'run_web.py', 'protocol.py', 'tools/example.py', 'web/static/app.js', 'start.sh', 'scripts/rollback.py',
        'config.defaults.yaml', 'config/files.py', 'attachments/documents.py', 'agent/protocol.py', 'runtime/tool_registry.py'}
    with tarfile.open(candidate.output / result['archive']) as archive:
        assert archive.extractfile('app/run_web.py').read() == b'VALUE = 1\n'
        assert archive.extractfile('app/config.defaults.yaml').read() == b'synthetic: versioned-defaults\n'
        assert archive.getmember('app/start.sh').mode == 0o755
        all_content = b''.join(archive.extractfile(item).read() for item in archive)
    for forbidden in (b'excluded-env', b'excluded-config', b'excluded-prompt', b'excluded-context',
                      b'excluded-skill', b'excluded-data', b'SYNTHETIC_PROBE'):
        assert forbidden not in all_content
    assert (candidate.repo / 'run_web.py').read_text() == 'DIRTY WORKTREE !!!\n'


def test_same_source_and_evidence_are_reproducible_and_output_is_never_overwritten(candidate, tmp_path):
    build(candidate)
    second = tmp_path / 'second'
    build(candidate, second)
    assert {p.name: p.read_bytes() for p in candidate.output.iterdir()} == {p.name: p.read_bytes() for p in second.iterdir()}
    with pytest.raises(ValueError, match='拒绝覆盖'):
        build(candidate)


def test_lightweight_tags_are_rejected(candidate):
    candidate.git('tag', 'v1.1')
    with pytest.raises(ValueError, match='附注标签'):
        release_package.resolve_tag(candidate.repo, 'v1.1')


def test_symlink_in_release_scope_is_rejected_without_following_it(candidate):
    try:
        (candidate.repo / 'tools/linked.py').symlink_to('/nonexistent-synthetic-target')
        candidate.git('add', 'tools/linked.py')
    except OSError:
        oid = subprocess.check_output(
            ['git', '-C', str(candidate.repo), 'hash-object', '-w', '--stdin'],
            input=b'/nonexistent-synthetic-target',
        ).decode().strip()
        candidate.git('update-index', '--add', '--cacheinfo', f'120000,{oid},tools/linked.py')
    candidate.git('commit', '-qm', 'synthetic link')
    candidate.git('tag', '-fa', 'v1.0', '-m', 'synthetic link tag')
    with pytest.raises(ValueError, match='非普通文件'):
        build(candidate)


@pytest.mark.parametrize('attack', ['traversal', 'symlink', 'duplicate', 'missing', 'extra', 'corrupt'])
def test_archive_validation_rejects_attacks_even_if_outer_checksums_are_recomputed(candidate, attack):
    build(candidate)

    def mutate(items):
        if attack == 'missing':
            items[:] = [(member, data) for member, data in items if member.name != 'app/run_web.py']
        elif attack == 'duplicate':
            items.append(items[0])
        elif attack == 'corrupt':
            index = next(i for i, (member, _) in enumerate(items) if member.name == 'app/run_web.py')
            member, _ = items[index]
            data = b'VALUE = 8\n'
            member.size = len(data)
            items[index] = (member, data)
        else:
            member = tarfile.TarInfo('../escape' if attack == 'traversal' else 'app/extra.py')
            member.mode = 0o644
            if attack == 'symlink':
                member.type, member.linkname = tarfile.SYMTYPE, '/synthetic-private'
            items.append((member, b''))
    rewrite_archive(candidate.output, mutate)
    with pytest.raises(ValueError):
        release_package.check(candidate.output)


def test_modified_sidecar_cannot_pass_verification(candidate):
    build(candidate)
    (candidate.output / 'requirements.txt').write_text('changed-dependency>=999\n')
    with pytest.raises(ValueError, match='附件校验失败'):
        release_package.check(candidate.output)


def test_preview_package_is_not_publishable(candidate, tmp_path):
    output = tmp_path / 'preview'
    result = release_package.build(candidate.repo, candidate.commit, output, candidate=True)
    assert result['kind'] == 'candidate' and result['version'] is None
    with patch.object(publish_release, 'gh') as gh:
        with pytest.raises(ValueError, match='候选预览'):
            publish_release.publish(candidate.repo, output, 'example/repo')
        gh.assert_not_called()


def fake_github(candidate, wrong_tag=False, failed_upload=False, corrupt_digest=False, draft=False):
    identity = release_package.resolve_tag(candidate.repo, 'v1.0')
    release_obj = {
        'draft': draft,
        'tag_name': 'v1.0',
        'html_url': 'https://github.com/example/repo/releases/tag/v1.0',
        'assets': [{'name': p.name, 'state': 'uploaded', 'size': p.stat().st_size,
                    'digest': 'sha256:' + ('0' * 64 if corrupt_digest else release_package.digest(p.read_bytes()))}
                   for p in candidate.output.iterdir()],
    }

    def call(*args):
        if args[0:2] == ('api', 'repos/example/repo/git/ref/tags/v1.0'):
            return json.dumps({'object': {'type': 'tag', 'sha': '0' * 40 if wrong_tag else identity['tag_object']}})
        if args[0:2] == ('api', 'repos/example/repo/git/tags/' + identity['tag_object']):
            return json.dumps({'object': {'type': 'commit', 'sha': identity['commit']}})
        if args[0:2] == ('release', 'create'):
            if failed_upload:
                raise ValueError('synthetic upload failure')
            return ''
        if args == ('api', 'repos/example/repo/releases/tags/v1.0'):
            return json.dumps(release_obj)
        if args == ('api', '--paginate', '--slurp', 'repos/example/repo/releases?per_page=100'):
            return json.dumps([[release_obj]])
        raise AssertionError('Unexpected GitHub command')
    return call


def test_publish_checks_remote_binding_and_publishes_by_default(candidate):
    build(candidate)
    with patch.object(publish_release, 'gh', side_effect=fake_github(candidate)) as gh:
        result = publish_release.publish(candidate.repo, candidate.output, 'example/repo')
    assert result['draft'] is False and result['assets'] == 5
    creation = [call.args for call in gh.call_args_list if call.args[0:2] == ('release', 'create')]
    assert len(creation) == 1 and '--latest' in creation[0] and '--verify-tag' in creation[0]

    with patch.object(publish_release, 'gh', side_effect=fake_github(candidate, draft=True)) as gh_draft:
        draft_result = publish_release.publish(candidate.repo, candidate.output, 'example/repo', draft=True)
    assert draft_result['draft'] is True
    creation_draft = [call.args for call in gh_draft.call_args_list if call.args[0:2] == ('release', 'create')]
    assert len(creation_draft) == 1 and '--draft' in creation_draft[0]


def test_publish_rejects_tag_identity_mismatch(candidate):
    build(candidate)
    manifest_path = candidate.output / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['commit'] = '0' * 40
    manifest_data = release_package.json_bytes(manifest)
    manifest_path.write_bytes(manifest_data)

    def mutate(items):
        for index, (member, data) in enumerate(items):
            if member.name == 'manifest.json':
                member.size = len(manifest_data)
                items[index] = (member, manifest_data)
                break
    rewrite_archive(candidate.output, mutate)
    assert release_package.check(candidate.output)['ok']
    with patch.object(publish_release, 'gh') as gh:
        with pytest.raises(ValueError, match='与本地标签不符'):
            publish_release.publish(candidate.repo, candidate.output, 'example/repo')
        gh.assert_not_called()


@pytest.mark.parametrize('failure', ['wrong_tag', 'failed_upload', 'corrupt_digest'])
def test_remote_tag_upload_and_asset_failures_never_publish_or_delete(candidate, failure):
    build(candidate)
    with patch.object(publish_release, 'gh', side_effect=fake_github(candidate, **{failure: True})) as gh:
        with pytest.raises(ValueError):
            publish_release.publish(candidate.repo, candidate.output, 'example/repo')
    assert not any(call.args[0:2] in {('release', 'edit'), ('release', 'delete')} for call in gh.call_args_list)
    if failure == 'wrong_tag':
        assert not any(call.args[0] == 'release' for call in gh.call_args_list)


def test_github_error_does_not_expose_response_or_token():
    with patch.object(publish_release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'SYNTHETIC_SECRET')):
        with pytest.raises(ValueError) as result:
            publish_release.gh('api', 'synthetic')
    assert 'SYNTHETIC_SECRET' not in str(result.value)


def test_frontend_dist_is_packaged_and_rejects_symlinks(tmp_path):
    repo = tmp_path / 'fe-repo'
    repo.mkdir()
    git = lambda *args: deploy.git(repo, *args)
    git('init', '-q')
    git('config', 'user.name', 'Synthetic test')
    git('config', 'user.email', 'test@example.invalid')
    for name, content in {
        'run_web.py': 'VALUE = 1\n',
        'agent/protocol.py': '# synthetic\n',
        'requirements.txt': 'example>=1\n',
        'frontend/package.json': '{"name":"synthetic"}\n',
    }.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git('add', '.')
    git('commit', '-qm', 'with frontend')
    git('tag', '-a', 'v1.0', '-m', 'v1.0')

    static_dir = tmp_path / 'built-static'
    (static_dir / 'assets').mkdir(parents=True)
    index_bytes = b'<html><script src="/static/assets/main-abc.js"></script></html>\n'
    js_bytes = b'console.log("synthetic");\n'
    (static_dir / 'index.html').write_bytes(index_bytes)
    (static_dir / 'assets' / 'main-abc.js').write_bytes(js_bytes)

    out = tmp_path / 'bundle'
    built = release_package.build(repo, 'v1.0', out, static_dir=static_dir)
    assert built['ok']
    manifest = json.loads((out / 'manifest.json').read_text())
    origins = {item['path']: item['origin'] for item in manifest['files']}
    assert origins['web/static/index.html'] == 'frontend-build'
    assert origins['web/static/assets/main-abc.js'] == 'frontend-build'
    assert origins['run_web.py'] == 'git'

    (static_dir / 'assets' / 'leak.exe').write_bytes(b'bad')
    with pytest.raises(ValueError, match='后缀不受白名单允许'):
        release_package.build(repo, 'v1.0', tmp_path / 'bad-suffix', static_dir=static_dir)
    (static_dir / 'assets' / 'leak.exe').unlink()

    try:
        (static_dir / 'assets' / 'leak').symlink_to('/etc/passwd')
    except OSError:
        pytest.skip('当前系统无权创建符号链接')
    with pytest.raises(ValueError, match='非普通文件'):
        release_package.build(repo, 'v1.0', tmp_path / 'bad-bundle', static_dir=static_dir)


def _rewrite_manifest_and_archive(directory, extra_files=None, mutate_manifest=None):
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    additions = extra_files or {}
    for path, content in additions.items():
        manifest['files'].append({
            'path': path,
            'sha256': release_package.digest(content),
            'size': len(content),
            'mode': 0o644,
            'git_blob': 'b' * 40,
            'origin': 'git',
        })
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    manifest_data = release_package.json_bytes(manifest)
    manifest_path.write_bytes(manifest_data)

    def mutate(items):
        for index, (member, data) in enumerate(items):
            if member.name == 'manifest.json':
                member.size = len(manifest_data)
                items[index] = (member, manifest_data)
                break
        for path, content in additions.items():
            member = tarfile.TarInfo('app/' + path)
            member.mode = 0o644
            member.size = len(content)
            items.append((member, content))

    rewrite_archive(directory, mutate)


def test_check_accepts_future_code_paths_while_rejecting_sensitive_paths(candidate, tmp_path):
    built_dir = tmp_path / 'future-ok'
    build(candidate, built_dir)
    _rewrite_manifest_and_archive(
        built_dir,
        extra_files={
            'future_pkg/worker.py': b'VALUE = 42\n',
            'scripts/future_cli.py': b'CLI = 1\n',
            'prompt/system_prompt_future.md': b'# template\n',
            'prompt/custom.example.md': b'# example template\n',
            'plugins/bundled/demo/config.json': b'{"enabled": true}\n',
        },
    )
    checked = release_package.check(built_dir)
    assert checked['ok'] is True

    for forbidden in (
        'config.yaml',
        'context.md',
        'AGENTS.md',
        'data/memory.lbdb',
        'skills/example/SKILL.md',
        'prompt/example.md',
        'prompt/soul.md',
        'tests/test_leak.py',
        'scripts/probe_leak.py',
        'scripts/test_leak.py',
        'dev/scratch.py',
        'frontend/src/App.tsx',
        'docs/README.md',
        'agent/binary.exe',
        'agent/AGENTS.md',
        'future_pkg/test_bad.py',
    ):
        bad_dir = tmp_path / ('bad-' + forbidden.replace('/', '_'))
        build(candidate, bad_dir)
        _rewrite_manifest_and_archive(bad_dir, extra_files={forbidden: b'bad\n'})
        with pytest.raises(ValueError, match='应用文件路径'):
            release_package.check(bad_dir)


def test_build_packages_host_updater_scripts(candidate, tmp_path):
    for name in deploy.UPDATER_FILES:
        target = candidate.repo / 'scripts' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'# synthetic {name}\n')
    (candidate.repo / 'scripts/tentacle.sh').write_text('#!/bin/sh\nexit 0\n')
    (candidate.repo / 'scripts/tentacle.sh').chmod(0o755)
    (candidate.repo / 'runtime/maintenance_protocol.py').write_text('ACTIONS = ("check",)\n')
    candidate.git('add', '.')
    candidate.git('update-index', '--chmod=+x', 'scripts/tentacle.sh')
    candidate.git('commit', '-qm', 'add host updater scripts')
    candidate.git('tag', '-fa', 'v1.0', '-m', 'v1.0')

    output = tmp_path / 'updater-bundle'
    result = build(candidate, output)
    manifest = json.loads((output / 'manifest.json').read_text())
    packaged = {item['path'] for item in manifest['files']}
    assert {'scripts/tentacle.sh', 'runtime/maintenance_protocol.py'} <= packaged
    assert {'scripts/' + name for name in deploy.UPDATER_FILES} <= packaged
    with tarfile.open(output / result['archive']) as archive:
        assert archive.getmember('app/scripts/tentacle.sh').mode == 0o755
