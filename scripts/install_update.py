#!/usr/bin/env python3
"""Install or refresh the root-owned host tools from a verified checkout."""

import argparse
import ast
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

if __package__:
    from . import deploy, release_package, update_runtime as runtime
else:
    import deploy
    import release_package
    import update_runtime as runtime


HOST_FILES = deploy.UPDATER_FILES
_UPDATER_FILENAME_RE = re.compile(r'[A-Za-z0-9_]+\.py\Z')


def _read_ast_assign(source_text, filename, name):
    try:
        tree = ast.parse(source_text, filename=filename)
    except SyntaxError as error:
        raise ValueError(f'Release 中的 {filename} 语法无效') from error
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, RecursionError) as error:
                raise ValueError(f'Release 中的 {filename} 定义 {name} 格式无效') from error
    return None


def parse_updater_protocol(source_bytes, origin='scripts/update.py'):
    value = _read_ast_assign(source_bytes.decode('utf-8'), origin, 'UPDATER_PROTOCOL')
    if type(value) is not int or value < 1:
        raise ValueError('Release 中的维护协议版本格式无效')
    return value


def parse_updater_files(source_bytes, origin='scripts/deploy.py'):
    value = _read_ast_assign(source_bytes.decode('utf-8'), origin, 'UPDATER_FILES')
    if value is None:
        return deploy.UPDATER_FILES
    if not isinstance(value, (tuple, list)) or not value or any(
        not isinstance(item, str) or not _UPDATER_FILENAME_RE.fullmatch(item) for item in value
    ):
        raise ValueError('Release 中的维护脚本清单格式无效')
    return tuple(value)


def archive_updater_protocol(directory):
    """Inspect a validated release archive's UPDATER_PROTOCOL without executing release code."""
    checked = release_package.check(directory)
    with tarfile.open(directory / checked['archive'], 'r:gz') as archive:
        try:
            member = archive.getmember('app/scripts/update.py')
        except KeyError:
            return None
        with archive.extractfile(member) as source:
            data = source.read()
    return parse_updater_protocol(data)


def has_release_updater(release_dir):
    return (release_dir / 'scripts/update.py').is_file()


def _target_specs(prefix, updater_files):
    base = prefix / 'libexec/tentacle-update'
    specs = {base / name: ('scripts/' + name, 0o644) for name in updater_files}
    specs[base / 'runtime/maintenance_protocol.py'] = ('runtime/maintenance_protocol.py', 0o644)
    specs[prefix / 'bin/tentacle'] = ('scripts/tentacle.sh', 0o755)
    return specs


def _build_code_payloads(specs, reader):
    payloads = {}
    for destination, (origin, mode) in specs.items():
        content = reader(origin)
        if destination.suffix == '.py':
            compile(content, origin, 'exec')
        payloads[destination] = (content, mode)
    return payloads


def _payloads_changed(prefix, file_payloads):
    for destination, expected in file_payloads.items():
        if deploy.current(prefix, destination.relative_to(prefix).as_posix()) != expected:
            return True
    return False


def _apply_payloads(prefix, payloads, *, check_dirs=False):
    backup_root = prefix / 'libexec/tentacle-update-backups'
    if check_dirs:
        for directory in {backup_root, *(path.parent for path in payloads)}:
            runtime.root_directory(directory, 0o700 if directory == backup_root else 0o755)
    originals = {path: deploy.current(prefix, path.relative_to(prefix).as_posix()) for path in payloads}
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='host-', dir=backup_root))
    for path, (data, mode) in originals.items():
        if data is not None:
            deploy.write_file(backup / path.relative_to(prefix), data, mode)
    applied = []
    try:
        for destination, (data, mode) in payloads.items():
            if isinstance(data, dict):
                runtime.write_json(destination.parent, destination.name, data, mode)
            else:
                deploy.write_file(destination, data, mode)
            applied.append(destination)
    except BaseException:
        for destination in reversed(applied):
            data, mode = originals[destination]
            if data is None:
                destination.unlink()
            else:
                deploy.write_file(destination, data, mode)
        raise
    return backup


def install(source, prefix, metadata=None, host=None):
    def reader(origin):
        if metadata is not None:
            return deploy.git(source, 'cat-file', 'blob', metadata['source_commit'] + ':' + origin)
        return (source / origin).read_bytes()

    base = prefix / 'libexec/tentacle-update'
    updater_files = parse_updater_files(reader('scripts/deploy.py'))
    payloads = {}
    if metadata is not None:
        installed = runtime.read_json(base, 'install.json')
        if installed:
            if installed['protocol'] > metadata['protocol']:
                raise ValueError('已安装维护工具协议较新，拒绝降级')
            previous = installed['source_commit']
            if previous != metadata['source_commit']:
                deploy.git(source, 'merge-base', '--is-ancestor', previous, metadata['source_commit'])
        payloads[base / 'install.json'] = (metadata, 0o644)
    if host is not None:
        runtime.validate_host(host)
        path = prefix / 'share/tentacle/host.json'
        if path.exists() and runtime.host_config(path.parent) != host:
            raise ValueError('已有主机参数与安装参数不符')
        payloads[path] = (host, 0o644)
    payloads.update(_build_code_payloads(_target_specs(prefix, updater_files), reader))

    # Validate each destination before any replacement. Keep the old tool set if a write fails.
    backup = _apply_payloads(prefix, payloads, check_dirs=metadata is not None)
    return {'installed': True, 'backup': str(backup), 'service_changed': False}


def install_from_release(release_dir, prefix, metadata):
    """Refresh root-owned host tools from a verified, active release directory."""
    if not isinstance(metadata, dict) or 'protocol' not in metadata or 'source_commit' not in metadata:
        raise ValueError('维护工具安装元数据不完整')

    def reader(origin):
        path = deploy.checked_path(release_dir, origin)
        if not path.is_file():
            raise ValueError('Release 缺少维护工具文件：' + origin)
        return path.read_bytes()

    base = prefix / 'libexec/tentacle-update'
    updater_files = parse_updater_files(reader('scripts/deploy.py'))
    release_protocol = parse_updater_protocol(reader('scripts/update.py'))
    if release_protocol != metadata['protocol']:
        raise ValueError('Release 中的维护协议版本与当前更新器不符')
    installed = runtime.read_json(base, 'install.json')
    if installed and installed['protocol'] > metadata['protocol']:
        raise ValueError('已安装维护工具协议较新，拒绝降级')

    payloads = _build_code_payloads(_target_specs(prefix, updater_files), reader)
    if not _payloads_changed(prefix, payloads):
        return {'installed': False, 'changed': False, 'service_changed': False}
    payloads[base / 'install.json'] = (metadata, 0o644)
    backup = _apply_payloads(prefix, payloads, check_dirs=True)
    return {'installed': True, 'changed': True, 'backup': str(backup), 'service_changed': False}


def main(argv=None):
    if __package__:
        from . import maintenance, update
    else:
        import maintenance
        import update

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('刷新维护工具需要 sudo')
    os.umask(0o022)
    try:
        prefix = Path('/usr/local')
        status = prefix / 'share/tentacle'
        host = runtime.host_config(status)
        source = args.source.resolve(strict=True)
        if deploy.git(source, 'status', '--porcelain', '--untracked-files=normal').strip():
            raise ValueError('安装源须为通过 CI 的干净 Git 检出目录')
        with deploy.deployment_lock(runtime.root_directory(Path(host['backups']), 0o700)):
            update.read_state(host, status)
            install(source, prefix, {'protocol': update.UPDATER_PROTOCOL, 'source_commit': deploy.revision(source, 'HEAD'),
                                     'installed_at': datetime.now(timezone.utc).isoformat()})
            maintenance.install_socket(host, prefix)
        print('维护工具已刷新')
        return 0
    except (ValueError, OSError, KeyError, TypeError, SyntaxError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, ValueError) else type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
