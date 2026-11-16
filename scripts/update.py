#!/usr/bin/env python3
"""Build verified Linux releases and switch the systemd service between them."""

import argparse
from datetime import datetime, timezone
from functools import partial
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

if __package__:
    from . import deploy, github_releases, install_update, release_package, update_runtime as runtime
else:
    import deploy
    import github_releases
    import install_update
    import release_package
    import update_runtime as runtime


UPDATER_PROTOCOL = 6
DEFAULT_STATUS = Path('/usr/local/share/tentacle')
IDENTITY = ('version', 'commit', 'tag_object')
RELEASE_RECORD = '.release.json'


class ReleaseRolledBack(ValueError):
    pass


def identity(value):
    return {key: value[key] for key in IDENTITY}


def public_status(status):
    value = runtime.read_json(status, 'version.json')
    if value is None:
        raise ValueError('尚未初始化生产版本')
    return value


def load_config(path):
    info = path.stat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('更新配置必须由 root 独占')
    value = json.loads(path.read_bytes())
    raw_token = value.get('token_file')
    if raw_token is not None:
        token = Path(raw_token)
        if not token.is_absolute() or token.resolve() != token:
            raise ValueError('更新令牌路径无效')
    return value


def release_path(root, version):
    github_releases.version_key(version)
    return root / 'releases' / version


def installed_release(root, record):
    path = release_path(root, record['version'])
    if path.resolve() != path or runtime.read_json(path, RELEASE_RECORD) != record:
        raise ValueError('版本目录或部署记录与所选版本不符')
    runtime.root_directory(path)
    return path


def selected_release(root):
    link = root / 'current'
    if not link.is_symlink():
        raise ValueError('生产入口必须是 current 符号链接')
    return link.resolve(strict=True)


def switch_release(root, record):
    path = installed_release(root, record)
    link = root / 'current'
    if link.exists() and not link.is_symlink():
        raise ValueError('current 入口已被其他文件占用')
    with tempfile.TemporaryDirectory(prefix='.switch-', dir=root) as directory:
        staged = Path(directory) / 'current'
        staged.symlink_to(path.relative_to(root), target_is_directory=True)
        staged.replace(link)
    runtime.sync_directory(root)


def read_state(host, status):
    root, backups = (Path(host[key]) for key in ('production', 'backups'))
    if runtime.read_json(backups, runtime.PENDING_FILE) is not None:
        raise ValueError('存在中断的切换，请先运行 sudo tentacle update --recover')
    value = runtime.read_json(backups, runtime.STATE_FILE)
    if not value or value.get('schema') != UPDATER_PROTOCOL:
        raise ValueError('缺少新布局的部署记录')
    if selected_release(root) != installed_release(root, value['current']):
        raise ValueError('current 与部署记录不一致')
    if identity(value['current']) != identity(public_status(status)):
        raise ValueError('公开与私有版本记录不一致')
    return value


def publish_state(backups, status, value):
    runtime.write_json(backups, runtime.STATE_FILE, value)
    runtime.write_json(status, 'version.json',
                       {**identity(value['current']), 'updater_protocol': UPDATER_PROTOCOL}, 0o644)


def bundle_record(directory, snapshot):
    checked = release_package.check(directory)
    manifest = json.loads((directory / 'manifest.json').read_bytes())
    if checked['kind'] != 'release' or identity(manifest) != identity(snapshot):
        raise ValueError('必须使用匹配的正式 Release')
    expected = {'runtime/paths.py', release_package.DATA_EPOCH_FILE, 'config.defaults.yaml'}
    if not (expected | release_package.PUBLIC_PROMPTS) <= {item['path'] for item in manifest['files']}:
        raise ValueError('Release 缺少新布局所需的路径模块、配置或公开模板')
    requirements = (directory / 'requirements.txt').read_text(encoding='utf-8').splitlines()
    for line in (item.strip() for item in requirements):
        if line and not line.startswith('#') and not re.fullmatch(
            r'[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.+!_-]*(?:; sys_platform != "win32")?', line
        ):
            raise ValueError('Release 依赖必须是固定版本的包名')
    with tarfile.open(directory / checked['archive'], 'r:gz') as archive:
        with archive.extractfile('app/' + release_package.DATA_EPOCH_FILE) as source:
            epoch = source.read().strip()
    if not re.fullmatch(rb'[1-9][0-9]{0,8}', epoch):
        raise ValueError('Release 的数据兼容代号无效')
    return {**identity(manifest), 'data_epoch': int(epoch), 'bundle_sha256': checked['sha256']}


_DEFAULT_HOST_PREFIX = object()


def require_compatible(before, after):
    if before['data_epoch'] != after['data_epoch']:
        raise ValueError('版本间的数据兼容代号不同，须按该 Release 的数据迁移说明操作')


def require_updater_compatible(directory):
    target_protocol = install_update.archive_updater_protocol(directory)
    if target_protocol is not None and target_protocol != UPDATER_PROTOCOL:
        raise ValueError('目标版本调整了宿主维护协议，请按该 Release 的升级说明处理')


def sync_host_tools(release_path, host_prefix, target):
    if host_prefix is None or not (host_prefix / 'libexec/tentacle-update/install.json').is_file():
        return False
    if not install_update.has_release_updater(release_path):
        return False
    metadata = {
        'protocol': UPDATER_PROTOCOL,
        'source_commit': target['commit'],
        'version': target['version'],
        'installed_at': datetime.now(timezone.utc).isoformat(),
    }
    try:
        result = install_update.install_from_release(release_path, host_prefix, metadata)
    except (ValueError, OSError, KeyError, TypeError, SyntaxError) as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise ValueError(f'应用已切换至目标版本，但宿主维护工具刷新失败（已保留原维护工具）：{detail}') from error
    return bool(result.get('changed'))


def run_build(arguments, timeout=900, env=None):
    result = subprocess.run([str(item) for item in arguments], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                            cwd='/', timeout=timeout, env=env or {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    if result.returncode:
        raise ValueError(f'版本构建失败：{Path(str(arguments[0])).name}，退出码 {result.returncode}')


def check_python(python):
    if python.resolve() != python or not python.is_file():
        raise ValueError('Python 必须指向 root 管理的实际解释器文件')
    runtime.root_directory(python.parent)
    info = python.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Python 解释器必须由 root 管理')
    run_build([python, '-I', '-c', 'import sys; assert sys.version_info[:2] == (3, 13)'])


def build_release(host, directory, record):
    root = Path(host['production'])
    path = release_path(root, record['version'])
    runtime.root_directory(path.parent)
    if path.is_symlink() or path.resolve() != path:
        raise ValueError('版本目录不能是链接')
    existing = runtime.read_json(path, RELEASE_RECORD)
    if existing:
        return installed_release(root, record)
    if path.exists():
        # 构建中断只留下未登记的版本。正在运行的链接始终指向有记录的目录。
        if (root / 'current').resolve() == path:
            raise ValueError('current 指向不完整的版本，请先处理部署记录')
        runtime.root_directory(path)
        shutil.rmtree(path)
    path.mkdir(mode=0o755)
    checked = release_package.check(directory)
    with tarfile.open(directory / checked['archive'], 'r:gz') as archive:
        for member in archive:
            if member.name.startswith('app/'):
                target = deploy.checked_path(path, member.name[4:])
                with archive.extractfile(member) as source:
                    deploy.write_file(target, source.read(), member.mode)
    python = Path(host['python'])
    check_python(python)
    venv = path / '.venv'
    run_build([python, '-I', '-m', 'venv', venv])
    interpreter = venv / 'bin/python'
    run_build([interpreter, '-I', '-m', 'pip', '--isolated', 'install', '--no-cache-dir',
               '--disable-pip-version-check', '--only-binary=:all:', '--no-deps', '-r', directory / 'requirements.txt'])
    run_build(['/usr/sbin/runuser', '-u', host['user'], '--', interpreter, '-I', '-m', 'pip', 'check'])
    runtime.write_json(path, RELEASE_RECORD, record, 0o644)
    runtime.sync_directory(path.parent)
    return path


def recover_locked(host, status, timeout=90):
    root, backups = (Path(host[key]) for key in ('production', 'backups'))
    pending = runtime.read_json(backups, runtime.PENDING_FILE)
    if pending is None:
        return {'recovered': False}
    if pending.get('schema') != UPDATER_PROTOCOL:
        raise ValueError('中断记录版本不符，请按部署文档恢复')
    before = pending['before']
    original = installed_release(root, before['current'])
    candidate = installed_release(root, pending['target'])
    require_compatible(before['current'], pending['target'])
    if selected_release(root) not in {original, candidate}:
        raise ValueError('current 指向部署记录之外的版本，请人工核对')
    runtime.systemctl(host, 'stop')
    runtime.require_stopped(host)
    switch_release(root, before['current'])
    runtime.systemctl(host, 'start')
    runtime.wait_healthy(host, original, pending['http_status'], timeout)
    publish_state(backups, status, before)
    runtime.clear_pending(backups)
    return {'recovered': True, **identity(before['current'])}


def activate(host, status, before, target, timeout=90):
    root, backups = (Path(host[key]) for key in ('production', 'backups'))
    require_compatible(before['current'], target)
    original = installed_release(root, before['current'])
    candidate = installed_release(root, target)
    if selected_release(root) != original:
        raise ValueError('准备期间 current 已变化')
    runtime.require_running(host, original)
    expected = runtime.http_status(host)
    pending = {'schema': UPDATER_PROTOCOL, 'before': before, 'target': target, 'http_status': expected}
    runtime.write_json(backups, runtime.PENDING_FILE, pending)
    try:
        runtime.systemctl(host, 'stop')
        runtime.require_stopped(host)
        switch_release(root, target)
        runtime.systemctl(host, 'start')
        runtime.wait_healthy(host, candidate, expected, timeout)
        publish_state(backups, status, {'schema': UPDATER_PROTOCOL, 'current': target,
                                        'previous': before['current']})
        runtime.clear_pending(backups)
    except (Exception, KeyboardInterrupt):
        try:
            recover_locked(host, status, timeout)
        except (Exception, KeyboardInterrupt) as error:
            raise ValueError('切换与恢复未完成，请检查服务日志后执行 sudo tentacle update --recover') from error
        raise ReleaseRolledBack('版本切换失败，已恢复先前版本') from None
    return identity(target)


def approve(target, apply):
    print(json.dumps(target, ensure_ascii=False, indent=2))
    if not apply:
        return False
    if input('输入目标版本号确认切换：').strip() != target['target']['version']:
        raise ValueError('版本确认不匹配')
    return True


def change_version(status, host, action, version, *, confirm, recover=False, timeout=90,
                   expected_bundle=None, host_prefix=_DEFAULT_HOST_PREFIX):
    if host_prefix is _DEFAULT_HOST_PREFIX:
        host_prefix = Path('/usr/local') if status == DEFAULT_STATUS else None
    if recover:
        return recover_locked(host, status, timeout)
    before = read_state(host, status)
    current = before['current']
    if action == 'revert':
        target = before.get('previous')
        if not target or (version and target['version'] != version):
            raise ValueError('没有匹配的上一个成功版本')
        installed_release(Path(host['production']), target)
        if confirm({'current': identity(current), 'target': identity(target)}):
            return activate(host, status, before, target, timeout)
        return None
    config = load_config(Path(host['config']))
    client = github_releases.Client(config['repository'], config.get('token_file'))
    selected = client.select(version)
    with tempfile.TemporaryDirectory(prefix='tentacle-update-') as temporary:
        directory = Path(temporary) / 'bundle'
        snapshot = client.download(selected, directory)
        target = bundle_record(directory, snapshot)
        if current['version'] == target['version'] and current != target:
            raise ValueError('当前版本对应的 Release 内容发生变化')
        if github_releases.version_key(target['version']) <= github_releases.version_key(current['version']):
            print(json.dumps({'current': identity(current), 'available': False}, ensure_ascii=False))
            return None
        require_compatible(current, target)
        require_updater_compatible(directory)
        if expected_bundle is not None and target['bundle_sha256'] != expected_bundle:
            raise ValueError('Release 包已变化，请重新检查更新')
        if confirm({'current': identity(current), 'target': identity(target)}):
            release_path = build_release(host, directory, target)
            client.assert_unchanged(snapshot)
            activated = activate(host, status, before, target, timeout)
            host_updated = sync_host_tools(release_path, host_prefix, target)
            return {**activated, 'host_tools_updated': True} if host_updated else activated
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('version', 'host-service', 'update', 'revert'))
    parser.add_argument('version', nargs='?')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--status-root', type=Path, default=DEFAULT_STATUS)
    parser.add_argument('--timeout', type=int, default=90)
    args = parser.parse_args(argv)
    if (args.recover and (args.action != 'update' or args.version or args.apply)) or args.timeout < 1:
        parser.error('恢复使用 tentacle update --recover')
    try:
        if args.action == 'version':
            print(public_status(args.status_root)['version'])
            return 0
        host = runtime.host_config(args.status_root)
        if args.action == 'host-service':
            print(host['service'])
            return 0
        if os.geteuid() != 0:
            raise ValueError('维护命令需要 root')
        os.umask(0o022)
        change = partial(change_version, args.status_root, host, args.action, args.version,
                         confirm=partial(approve, apply=args.apply), recover=args.recover, timeout=args.timeout)
        if args.apply or args.recover:
            runtime.root_directory(Path(host['backups']), 0o700)
            with deploy.deployment_lock(Path(host['backups'])), runtime.interruption_handlers():
                result = change()
        else:
            result = change()
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, EOFError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ValueError) else type(error).__name__},
                         ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
