#!/usr/bin/env python3
"""Install the versioned Linux layout on an Ubuntu/systemd host."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile

if __package__:
    from . import check_updates, deploy, github_releases, install_update, maintenance, update, update_runtime as runtime
else:
    import check_updates
    import deploy
    import github_releases
    import install_update
    import maintenance
    import update
    import update_runtime as runtime


PYTHON_VERSION = '3.13.15'
MARKER = '# Managed by Tentacle Linux installer\n'
INSTALL_RECORD = 'linux-install.json'


def specification(args):
    host = {'schema': 2, 'production': str(args.production), 'backups': str(args.backups),
            'config': str(args.config_root / 'update.json'), 'service': args.service,
            'health_port': args.port, 'home': str(args.home), 'user': args.user,
            'python': str(args.python or args.production / 'python/bin/python3.13')}
    runtime.validate_host(host)
    roots = (args.production, args.backups, args.config_root, args.home,
             Path('/usr/local/libexec/tentacle-update'), update.DEFAULT_STATUS)
    if any(path.resolve() != path or len(path.parts) < 3 for path in roots):
        raise ValueError('安装目录须为真实绝对路径，且不能使用系统顶层目录')
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ValueError('程序、Home、备份和维护目录必须分离')
    if args.version:
        github_releases.version_key(args.version)
    github_releases.Client(args.repository, args.token_file)
    return {'host': host, 'repository': args.repository, 'version': args.version,
            'bundle': str(args.bundle.resolve()) if args.bundle else None,
            'token_file': str(args.token_file.absolute()) if args.token_file else None}


def application_unit(host):
    root, home = host['production'], host['home']
    return MARKER + f'''[Unit]
Description=Tentacle agent service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User={host['user']}
WorkingDirectory={home}
Environment=HOME={home}
Environment=TENTACLE_HOME={home}
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=MALLOC_ARENA_MAX=2
ExecStart={root}/current/.venv/bin/python {root}/current/run_web.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
KillMode=control-group
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
'''


def prepare_python(args):
    if args.python:
        python = args.python.resolve(strict=True)
    else:
        base = runtime.root_directory(args.production / 'python')
        with tempfile.TemporaryDirectory(prefix='tentacle-uv-') as temporary:
            bootstrap = Path(temporary) / 'uv'
            update.run_build(['/usr/bin/python3', '-I', '-m', 'venv', bootstrap])
            update.run_build([bootstrap / 'bin/python', '-I', '-m', 'pip', '--isolated', 'install',
                              '--only-binary=:all:', '--disable-pip-version-check', 'uv>=0.9,<1'])
            uv = bootstrap / 'bin/uv'
            env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'UV_PYTHON_INSTALL_DIR': str(base),
                   'UV_NO_CONFIG': '1', 'UV_NO_CACHE': '1'}
            update.run_build([uv, 'python', 'install', '--no-bin', PYTHON_VERSION], env=env)
            found = subprocess.run([uv, 'python', 'find', '--managed-python', PYTHON_VERSION],
                                   cwd='/', env=env, text=True, check=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=30)
            python = Path(found.stdout.strip()).resolve(strict=True)
            if not python.is_relative_to(base):
                raise ValueError('管理的解释器不在 root 管理的安装目录')
    update.check_python(python)
    return str(python)


def prepare_home(host):
    home = Path(host['home'])
    runtime.root_directory(home.parent)
    try:
        account = pwd.getpwnam(host['user'])
    except KeyError:
        update.run_build(['/usr/sbin/useradd', '--system', '--no-log-init', '--create-home',
                          '--home-dir', home, '--shell', '/usr/sbin/nologin', host['user']], timeout=90)
        account = pwd.getpwnam(host['user'])
    if not home.exists():
        home.mkdir(mode=0o700)
        os.chown(home, account.pw_uid, account.pw_gid)
    if account.pw_uid == 0 or account.pw_dir != str(home) or home.is_symlink() or home.stat().st_uid != account.pw_uid:
        raise ValueError('现有应用账号或 Home 的归属与安装参数不符')
    overrides = {'webui': {'host': '127.0.0.1', 'port': host['health_port']}, 'heartbeat': {'enabled': False},
                 'subagents': {'enabled': False}}
    initial = {'config/config.yaml': json.dumps(overrides, indent=2) + '\n', 'config/.env': '',
               'prompt/soul.md': '你是 {{agent_name}}，正在与 {{user_name}} 交流。\n',
               'prompt/master.md': '', 'prompt/memory.md': '', 'prompt/situation.md': ''}
    # 在用户权限下创建文件，避免 root 跟随应用可写目录中的链接。
    seed = """
import json, os, stat, sys
from pathlib import Path
os.umask(0o077)
home = Path(sys.argv[1])
for name in ('config', 'data', 'prompt', 'skills', 'workspace', 'logs', '.cache', 'tmp'):
    path = home / name
    path.mkdir(exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ValueError('数据目录必须为应用账号所有的真实目录')
for name, data in json.loads(sys.argv[2]).items():
    path = home / name
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(data)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('用户文件必须为应用账号所有的普通文件')
        if path.name == '.env' and info.st_mode & 0o077:
            raise ValueError('.env 权限必须为 0600')
"""
    update.run_build(['/usr/sbin/runuser', '-u', host['user'], '--', '/usr/bin/python3',
                      '-I', '-c', seed, home, json.dumps(initial, ensure_ascii=False)])


def first_release(args, host, state, config):
    root = args.production
    if state.get('prepared'):
        record = state['release']
        update.installed_release(root, record)
        return record
    version = state.get('release', {}).get('version') or args.version
    with tempfile.TemporaryDirectory(prefix='tentacle-install-') as temporary:
        if args.bundle:
            # 离线包由管理员从可信渠道取得；复制到 root 私有目录后校验和构建。
            directory = Path(temporary) / 'bundle'
            shutil.copytree(args.bundle, directory, symlinks=True)
            manifest = json.loads(deploy.checked_path(directory, 'manifest.json').read_bytes())
            snapshot = update.identity(manifest)
        else:
            directory = Path(temporary) / 'bundle'
            client = github_releases.Client(args.repository, args.token_file)
            snapshot = client.download(client.select(version), directory)
        if version and snapshot['version'] != version:
            raise ValueError('Release 与指定版本不符')
        record = update.bundle_record(directory, snapshot)
        if state.get('release', record) != record:
            raise ValueError('中断安装对应的 Release 已变化')
        state['release'] = record
        runtime.write_json(config, INSTALL_RECORD, state)
        update.build_release(host, directory, record)
        if not args.bundle:
            client.assert_unchanged(snapshot)
        state['prepared'] = True
        runtime.write_json(config, INSTALL_RECORD, state)
        return record


def install(args, source, spec, prefix=Path('/usr/local'), unit_root=Path('/etc/systemd/system')):
    config = runtime.root_directory(args.config_root, 0o700)
    backups = runtime.root_directory(args.backups, 0o700)
    root = runtime.root_directory(args.production)
    runtime.root_directory(prefix)
    status = prefix / 'share/tentacle'
    unit = deploy.checked_path(unit_root, args.service)
    commit = deploy.revision(source, 'HEAD')
    with deploy.deployment_lock(backups), runtime.interruption_handlers():
        state = runtime.read_json(config, INSTALL_RECORD)
        if state is None:
            if (any(root.iterdir()) or unit.exists() or (status / 'host.json').exists()
                    or runtime.read_json(backups, runtime.STATE_FILE) is not None):
                raise ValueError('发现已有部署；请先按迁移清单备份和移开旧的主机登记')
            loaded = subprocess.run(['systemctl', 'show', args.service, '--property=LoadState', '--value'],
                                    capture_output=True, text=True, check=True, timeout=15)
            if loaded.stdout.strip() != 'not-found':
                raise ValueError('同名 systemd 服务已加载，请先处理旧服务')
            state = {'schema': 2, 'spec': spec, 'source_commit': commit}
            runtime.write_json(config, INSTALL_RECORD, state)
        if state.get('schema') != 2 or state['spec'] != spec or state['source_commit'] != commit:
            raise ValueError('安装参数或源码已变化，请使用原参数和提交恢复')
        if state.get('complete'):
            return {'installed': True, 'already_installed': True, 'version': update.public_status(status)['version']}

        if 'python' not in state:
            if not args.skip_system_packages:
                env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'DEBIAN_FRONTEND': 'noninteractive'}
                update.run_build(['/usr/bin/apt-get', 'update'], env=env)
                update.run_build(['/usr/bin/apt-get', 'install', '-y', '--no-install-recommends',
                                  'python3-venv', 'ca-certificates', 'git'], env=env)
            state['python'] = prepare_python(args)
            runtime.write_json(config, INSTALL_RECORD, state)
        host = {**spec['host'], 'python': state['python']}
        prepare_home(host)
        if runtime.app_processes(host):
            # 上次安装中断可能留下由同一 unit 启动的候选服务。
            if not state.get('release') or not unit.exists() or unit.read_text() != application_unit(host):
                raise ValueError('发现另一个应用进程；请停止旧服务或手工实例')
            runtime.require_running(host, update.installed_release(root, state['release']))

        settings = {'repository': args.repository}
        token = config / 'github-token'
        if args.token_file is not None:
            credential = github_releases.read_token(args.token_file)
            if token.exists():
                if github_releases.read_token(token) != credential:
                    raise ValueError('已有更新令牌与指定令牌不一致')
            else:
                deploy.write_file(deploy.checked_path(config, token.name), (credential + '\n').encode(), 0o600)
            settings['token_file'] = str(token)
        if runtime.read_json(config, 'update.json') not in (None, settings):
            raise ValueError('已有更新配置与安装参数不一致')
        runtime.write_json(config, 'update.json', settings)

        record = first_release(args, host, state, config)
        metadata = {'protocol': update.UPDATER_PROTOCOL, 'source_commit': commit,
                    'installed_at': datetime.now(timezone.utc).isoformat()}
        install_update.install(source, prefix, metadata, host)
        if unit.exists() and unit.read_text() != application_unit(host):
            raise ValueError('已有 systemd 单元内容变化，保留原文件')
        deploy.write_file(unit, application_unit(host).encode(), 0o644)
        update.switch_release(root, record)
        subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=30)
        try:
            runtime.systemctl(host, 'start')
            runtime.wait_healthy(host, update.installed_release(root, record))
            update.publish_state(backups, status, {'schema': update.UPDATER_PROTOCOL, 'current': record})
            check_updates.install_timer(unit_root)
            maintenance.install_socket(host, prefix, unit_root)
            runtime.systemctl(host, 'enable')
            state['complete'] = True
            runtime.write_json(config, INSTALL_RECORD, state)
        except (Exception, KeyboardInterrupt):
            runtime.systemctl(host, 'stop')
            raise
        return {'installed': True, 'version': record['version'], 'service': args.service}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='实际安装；默认仅打印方案')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--repository', default='shuunO-O/tentacle')
    parser.add_argument('--token-file', type=Path, default=None,
                        help='可选 GitHub 只读令牌文件（公开仓库可省略；私有仓库或避免限流时使用）')
    parser.add_argument('--version')
    parser.add_argument('--bundle', type=Path, help='从可信渠道取得的完整正式附件目录，供离线首装')
    parser.add_argument('--production', type=Path, default=Path('/opt/tentacle'))
    parser.add_argument('--backups', type=Path, default=Path('/var/lib/tentacle-releases'))
    parser.add_argument('--config-root', type=Path, default=Path('/etc/tentacle'))
    parser.add_argument('--user', default='tentacle')
    parser.add_argument('--home', type=Path, default=Path('/home/tentacle'))
    parser.add_argument('--service', default='tentacle.service')
    parser.add_argument('--port', type=int, default=8420)
    parser.add_argument('--python', type=Path, help='root 管理的 Python 3.13；省略时由 uv 安装')
    parser.add_argument('--skip-system-packages', action='store_true')
    args = parser.parse_args(argv)
    try:
        spec = specification(args)
        if not args.apply:
            print(json.dumps({'preview': True, **spec}, ensure_ascii=False, indent=2))
            return 0
        if sys.platform != 'linux' or os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
            raise ValueError('--apply 需要 root 和正在运行的 Linux systemd')
        if not args.skip_system_packages and not Path('/etc/debian_version').exists():
            raise ValueError('自动安装系统依赖仅支持 Ubuntu/Debian')
        os.umask(0o022)
        source = args.source.resolve(strict=True)
        if deploy.git(source, 'status', '--porcelain', '--untracked-files=normal').strip():
            raise ValueError('安装源须为通过 CI 的干净 Git 检出目录')
        print(json.dumps(install(args, source, spec), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ValueError) else type(error).__name__},
                         ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
