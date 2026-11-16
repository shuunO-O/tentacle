#!/usr/bin/env python3
"""从确切 Git 提交导出干净的应用源码并打包；不读取工作区配置、运行数据或凭据。"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

if __package__:
    from . import deploy
else:
    import deploy


VERSION_RE = re.compile(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?\Z')
# 保留旧版清单的路径识别；已移除的目录在当前源码树中没有可打包文件。
RUNTIME_DIRS = {'agent', 'attachments', 'config', 'runtime', 'dialects', 'memory', 'plugins', 'qqbot', 'subconscious', 'subagents', 'tools', 'web'}
HOST_SCRIPTS = {
    'scripts/rollback.py',
    'scripts/device_token.py',
    'scripts/configure.py',
    'scripts/memory_sources.py',
    'scripts/backup_home.py',
    'scripts/tentacle.sh',
    *('scripts/' + name for name in deploy.UPDATER_FILES),
}
METADATA = {'manifest.json', 'requirements.txt', 'RELEASE_NOTES.md'}
PUBLIC_PROMPTS = {'prompt/guild.example.md', 'prompt/system_prompt_compressor.md',
                  'prompt/system_prompt_memory_extractor.md', 'prompt/system_prompt_subagent.md'}
PUBLIC_PROMPT_RE = re.compile(r'prompt/(?:system_prompt_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+\.example)\.md\Z')
DATA_EPOCH_FILE = 'runtime/data_epoch.txt'
MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 4096
SCHEMA = 4
# Host-side security invariants preserved regardless of which dirs/scripts future versions add.
FORBIDDEN_TOP_DIRS = frozenset({
    'data', 'skills', 'workspace', 'logs', 'certs', 'tmp',
    'tests', 'dev', 'docs', 'frontend', 'brand', '__pycache__',
})
FORBIDDEN_SEGMENTS = frozenset({'__pycache__', 'node_modules', 'tests'})
FORBIDDEN_NAMES = frozenset({'config.yaml', 'context.md', 'AGENTS.md'})
PACKAGE_SUFFIXES = deploy.CODE_SUFFIXES | {'.yaml', '.yml', '.md', '.txt', '.json'}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def application_complete(files):
    """新目录与旧版发布包共用校验；入口保持不变，协议模块支持迁移前后路径。"""
    return 'run_web.py' in files and bool({'protocol.py', 'agent/protocol.py'} & set(files))


def safe_name(name):
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and path.as_posix() == name
            and '\\' not in name and not any(part.startswith('.') for part in path.parts))


def is_public_prompt(name):
    return name in PUBLIC_PROMPTS or bool(PUBLIC_PROMPT_RE.fullmatch(name))


def package_name(name, *, origin='git'):
    """Host-side security boundaries for files allowed into app/, independent of build allowlists."""
    if not safe_name(name):
        return False
    path = PurePosixPath(name)
    if any(part in FORBIDDEN_SEGMENTS for part in path.parts):
        return False
    if origin == 'frontend-build':
        return name.startswith('web/static/') and deploy.managed(name)
    if origin != 'git':
        return False
    if name in PUBLIC_PROMPTS | {DATA_EPOCH_FILE}:
        return True
    if len(path.parts) == 1:
        if name in FORBIDDEN_NAMES:
            return False
        return (
            (path.suffix == '.py' and not name.startswith(('test_', 'probe_', 'benchmark_')))
            or name in {'start.sh', 'config.defaults.yaml'}
        )
    top = path.parts[0]
    if top in FORBIDDEN_TOP_DIRS:
        return False
    if top == 'prompt':
        return is_public_prompt(name)
    if top == 'scripts':
        return (
            not path.name.startswith(('test_', 'probe_', 'benchmark_'))
            and path.suffix in {'.py', '.sh'}
        )
    if path.name in FORBIDDEN_NAMES or path.name.startswith(('test_', 'probe_', 'benchmark_')):
        return False
    return path.suffix in PACKAGE_SUFFIXES


def runtime_name(name):
    if name in PUBLIC_PROMPTS | {DATA_EPOCH_FILE}:
        return True
    if not safe_name(name) or not deploy.managed(name):
        return False
    path = PurePosixPath(name)
    if len(path.parts) == 1:
        return (path.suffix == '.py' and not name.startswith(('test_', 'probe_', 'benchmark_'))) or name in {'start.sh', 'config.defaults.yaml'}
    return path.parts[0] in RUNTIME_DIRS or name in HOST_SCRIPTS


def resolve_tag(repo, version):
    if not VERSION_RE.fullmatch(version):
        raise ValueError('版本须为 v1.0、v1.0.1 或 v1.1')
    ref = 'refs/tags/' + version
    if deploy.git(repo, 'cat-file', '-t', ref).strip() != b'tag':
        raise ValueError('正式发布必须使用已存在的附注标签')
    obj = deploy.git(repo, 'rev-parse', ref).decode().strip()
    header = deploy.git(repo, 'cat-file', 'tag', obj).split(b'\n\n', 1)[0]
    if b'\ntype commit\n' not in header + b'\n':
        raise ValueError('标签必须直接指向提交')
    return {'version': version, 'commit': deploy.revision(repo, ref), 'tag_object': obj}


def git_entries(repo, commit):
    result = {}
    for record in deploy.git(repo, 'ls-tree', '-rz', commit).split(b'\0'):
        if record:
            meta, name = record.split(b'\t', 1)
            mode, kind, oid = meta.decode().split()
            result[name.decode()] = (mode, kind, oid)
    return result


def release_notes(identity, files, changes=''):
    version = identity.get('version') or '候选预览'
    intro = '由主分支 CI 验证后的纯净 Git 源码构建的应用包。'
    return (f'# Tentacle {version}\n\n{intro}\n\n'
            + (f'## 本次说明\n\n{changes.strip()}\n\n' if changes.strip() else '')
            + '## 发布资料\n\n'
            f'- 源码提交：`{identity["commit"]}`\n'
            f'- 运行文件：{len(files)} 个。\n'
            '- SHA256SUMS 校验各发布文件，manifest.json 记录逐文件校验值与权限。\n'
            '- requirements.txt 固定版本虚拟环境的运行依赖。\n'
            '- 包含公开模板；私有提示词、技能、配置、数据及凭据由应用 Home 保存。\n\n'
            '使用 Linux 安装器完成首次安装；已部署实例可通过 WebUI 或 `tentacle update` 更新。\n').encode()


def read_frontend_dist(static_dir):
    """收集构建好的前端静态目录产物，拒绝符号链接和未受管后缀。"""
    if static_dir is None:
        return []
    if not static_dir.is_dir():
        raise ValueError('前端构建产物目录不存在')
    result = []
    for current_dir, dirnames, filenames in os.walk(static_dir):
        dirnames.sort()
        cur = Path(current_dir)
        for dname in dirnames:
            if (cur / dname).is_symlink():
                raise ValueError('前端构建目录禁止包含符号链接')
        for fname in sorted(filenames):
            target = cur / fname
            if target.is_symlink() or not target.is_file():
                raise ValueError('前端构建产物包含非普通文件')
            rel = 'web/static/' + target.relative_to(static_dir).as_posix()
            if not safe_name(rel) or not deploy.managed(rel):
                raise ValueError(f'前端构建产物路径或后缀不受白名单允许：{rel}')
            data = target.read_bytes()
            result.append((rel, data, 0o644))
    if not any(rel == 'web/static/index.html' for rel, _, _ in result):
        raise ValueError('前端构建产物缺少 web/static/index.html')
    return sorted(result, key=lambda item: item[0])


def build(repo, ref, output, candidate=False, static_dir=None):
    identity = ({'version': None, 'commit': deploy.revision(repo, ref), 'tag_object': None}
                if candidate else resolve_tag(repo, ref))
    commit = identity['commit']
    entries = git_entries(repo, commit)
    payload, files = {}, []
    for name, (mode, kind, oid) in sorted(entries.items()):
        if not runtime_name(name):
            continue
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ValueError(f'发布范围包含非普通文件：{name}')
        data = deploy.git(repo, 'cat-file', 'blob', oid)
        permission = 0o755 if mode == '100755' else 0o644
        payload['app/' + name] = (data, permission)
        files.append({'path': name, 'sha256': digest(data), 'size': len(data),
                      'mode': permission, 'git_blob': oid, 'origin': 'git'})
    for rel, data, mode in read_frontend_dist(static_dir):
        if ('app/' + rel) in payload:
            raise ValueError(f'前端构建产物与跟踪文件冲突：{rel}')
        payload['app/' + rel] = (data, mode)
        files.append({'path': rel, 'sha256': digest(data), 'size': len(data),
                      'mode': mode, 'git_blob': None, 'origin': 'frontend-build'})
    files.sort(key=lambda item: item['path'])
    if not application_complete({file['path'] for file in files}):
        raise ValueError('候选缺少应用入口或协议模块')
    requirement = entries.get('requirements.txt')
    if not requirement or requirement[0:2] != ('100644', 'blob'):
        raise ValueError('缺少普通文件 requirements.txt')
    dependencies = deploy.git(repo, 'cat-file', 'blob', requirement[2])
    manifest = {'schema': SCHEMA, 'kind': 'candidate' if candidate else 'release', **identity,
                'files': files, 'references': {'requirements.txt': digest(dependencies)}}
    changes = ''
    if not candidate:
        notes_entry = entries.get(f'docs/releases/{identity["version"]}.md')
        if notes_entry:
            if notes_entry[0:2] != ('100644', 'blob'):
                raise ValueError('版本说明必须是普通 Markdown 文件')
            changes = deploy.git(repo, 'cat-file', 'blob', notes_entry[2]).decode()
        else:
            annotation = deploy.git(repo, 'cat-file', 'tag', identity['tag_object']).partition(b'\n\n')[2]
            changes = annotation.decode().split('-----BEGIN PGP SIGNATURE-----', 1)[0]
    if len(changes.encode()) > 64 * 1024:
        raise ValueError('版本说明超过长度上限')
    notes = release_notes(identity, files, changes)
    manifest['references']['RELEASE_NOTES.md'] = digest(notes)
    metadata = {'manifest.json': json_bytes(manifest), 'requirements.txt': dependencies, 'RELEASE_NOTES.md': notes}
    payload.update({name: (data, 0o644) for name, data in metadata.items()})
    if sum(len(data) for data, _ in payload.values()) > MAX_BYTES or len(payload) > MAX_FILES:
        raise ValueError('发布包超过大小或文件数量上限')
    if output.exists() or output.is_symlink():
        raise ValueError('输出目录已存在；拒绝覆盖已有产物')
    output.parent.mkdir(parents=True, exist_ok=True)
    label = identity['version'] or 'candidate-' + commit[:12]
    archive_name = f'tentacle-{label}.tar.gz'
    epoch = int(deploy.git(repo, 'show', '-s', '--format=%ct', commit).strip())
    with tempfile.TemporaryDirectory(prefix='.tentacle-package-', dir=output.parent) as tmp:
        directory = Path(tmp) / 'bundle'
        directory.mkdir()
        with (directory / archive_name).open('wb') as raw:
            with gzip.GzipFile(fileobj=raw, filename='', mode='wb', mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                    for name, (data, permission) in sorted(payload.items()):
                        member = tarfile.TarInfo(name)
                        member.size, member.mode, member.mtime = len(data), permission, epoch
                        archive.addfile(member, io.BytesIO(data))
        for name, data in metadata.items():
            (directory / name).write_bytes(data)
        checksums = ''.join(f'{digest(path.read_bytes())}  {path.name}\n' for path in sorted(directory.iterdir()))
        (directory / 'SHA256SUMS').write_text(checksums)
        checked = check(directory)
        if not candidate and resolve_tag(repo, ref) != identity:
            raise ValueError('构建期间标签已变化')
        directory.rename(output)
    return {**checked, 'directory': str(output)}


def check(directory):
    """不解压执行代码，独立检查附件、清单、包内路径及逐文件内容。"""
    names = {path.name for path in directory.iterdir()}
    archives = sorted(name for name in names if re.fullmatch(r'tentacle-(?:v\d+\.\d+(?:\.\d+)?|candidate-[0-9a-f]{12})\.tar\.gz', name))
    if len(archives) != 1 or names != METADATA | {'SHA256SUMS', archives[0]}:
        raise ValueError('发布附件集合不完整或包含额外文件')
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError('附件必须是大小受限的普通文件')
    sums = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in sums:
            raise ValueError('SHA256SUMS 格式或重复项错误')
        sums[match[2]] = match[1]
    if set(sums) != names - {'SHA256SUMS'}:
        raise ValueError('校验清单未覆盖全部附件')
    for name, expected in sums.items():
        if digest((directory / name).read_bytes()) != expected:
            raise ValueError('附件校验失败：' + name)
    manifest = json.loads((directory / 'manifest.json').read_bytes())
    if manifest.get('schema') != SCHEMA or manifest.get('kind') not in {'candidate', 'release'}:
        raise ValueError('发布清单格式不符')
    if not re.fullmatch(r'[0-9a-f]{40}', manifest.get('commit', '')):
        raise ValueError('提交格式不符')
    version = manifest.get('version')
    if manifest['kind'] == 'release':
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version) or not re.fullmatch(r'[0-9a-f]{40}', manifest.get('tag_object', '')):
            raise ValueError('正式发布缺少附注标签标识')
    elif version is not None or manifest.get('tag_object') is not None:
        raise ValueError('候选预览不能携带正式版本标识')
    label = version or 'candidate-' + manifest['commit'][:12]
    if archives[0] != f'tentacle-{label}.tar.gz':
        raise ValueError('归档文件名与版本不符')
    if set(manifest.get('references', {})) != METADATA - {'manifest.json'}:
        raise ValueError('清单缺少附件引用')
    for name, expected in manifest['references'].items():
        if sums[name] != expected:
            raise ValueError('清单引用与附件不符：' + name)
    files = {}
    for item in manifest['files']:
        name = item['path']
        origin = item.get('origin', 'git')
        if not package_name(name, origin=origin) or name in files or item['mode'] not in {0o644, 0o755}:
            raise ValueError('应用文件路径、权限或重复项错误')
        if origin == 'frontend-build' and (not name.startswith('web/static/') or item.get('git_blob') is not None):
            raise ValueError('前端构建条目格式不符')
        files[name] = item
    built = {name for name, item in files.items() if item.get('origin') == 'frontend-build'}
    if built and 'web/static/index.html' not in built:
        raise ValueError('归档前端构建产物缺少 web/static/index.html')
    if not application_complete(files):
        raise ValueError('发布清单缺少应用入口')
    seen, total = set(), 0
    with tarfile.open(directory / archives[0], 'r:gz') as archive:
        for member in archive:
            if (not member.isfile() or not safe_name(member.name) or member.name in seen
                    or member.size < 0 or member.mode not in {0o644, 0o755} or len(seen) >= MAX_FILES):
                raise ValueError('归档包含不安全路径、重复项或非普通文件')
            total += member.size
            if total > MAX_BYTES:
                raise ValueError('归档展开大小超出上限')
            seen.add(member.name)
            if member.name in METADATA:
                expected_data = (directory / member.name).read_bytes()
                expected_hash, expected_size, expected_mode = digest(expected_data), len(expected_data), 0o644
            elif member.name.startswith('app/') and member.name[4:] in files:
                item = files[member.name[4:]]
                expected_hash, expected_size, expected_mode = item['sha256'], item['size'], item['mode']
            else:
                raise ValueError('归档包含清单之外的文件：' + member.name)
            with archive.extractfile(member) as stream:
                actual = digest(stream.read())
            if (actual, member.size, member.mode) != (expected_hash, expected_size, expected_mode):
                raise ValueError('归档内容与清单不符：' + member.name)
    if seen != METADATA | {'app/' + name for name in files}:
        raise ValueError('归档缺少清单文件')
    return {'ok': True, 'version': version, 'commit': manifest['commit'], 'kind': manifest['kind'],
            'archive': archives[0], 'sha256': sums[archives[0]], 'files': len(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    build_parser = modes.add_parser('build')
    target = build_parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--tag')
    target.add_argument('--candidate', help='仅供 CI 检查打包，不可上传为正式 Release')
    build_parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    build_parser.add_argument('--static-dir', type=Path)
    build_parser.add_argument('--output', type=Path, required=True)
    modes.add_parser('check').add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    try:
        value = (check(args.directory) if args.command == 'check' else
                 build(args.source.resolve(), args.tag or args.candidate, args.output.absolute(),
                       candidate=args.candidate is not None,
                       static_dir=args.static_dir.absolute() if args.static_dir else None))
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ValueError) else type(error).__name__},
                         ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
