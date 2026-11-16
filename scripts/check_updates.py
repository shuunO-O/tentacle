#!/usr/bin/env python3
"""定时检查正式 Release 并保存公开摘要；绝不安装、停服或调用模型。"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

if __package__:
    from . import deploy, github_releases, install_update, update, update_runtime as runtime
else:
    import deploy
    import github_releases
    import install_update
    import update
    import update_runtime as runtime


RESULT_FILE = 'update-check.json'
CHECK_SERVICE = 'tentacle-update-check.service'
CHECK_TIMER = 'tentacle-update-check.timer'


def save_result(status, result):
    deploy.write_file(deploy.checked_path(status, RESULT_FILE),
                      (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode(), 0o644)


def check(status=update.DEFAULT_STATUS):
    """复用完整发布验证和人工审阅清单，检查不会取得发布锁或更改生产文件。"""
    previous = runtime.read_json(status, RESULT_FILE) or {}
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        host = runtime.host_config(status)
        before = update.read_state(host, status)['current']
        config = update.load_config(Path(host['config']))
        client = github_releases.Client(config['repository'], config.get('token_file'))
        selected = client.select()
        remote = client.snapshot(selected)
        current_key = github_releases.version_key(before['version'])
        latest_key = github_releases.version_key(remote['version'])
        if current_key == latest_key and update.identity(before) != update.identity(remote):
            raise ValueError('相同版本的远端标签标识发生变化，须人工核对')
        available = latest_key > current_key
        compatible = updater_compatible = target = None
        if available:
            with tempfile.TemporaryDirectory(prefix='tentacle-check-') as temporary:
                directory = Path(temporary) / 'bundle'
                snapshot = client.download(selected, directory)
                record = update.bundle_record(directory, snapshot)
                compatible = record['data_epoch'] == before['data_epoch']
                target_protocol = install_update.archive_updater_protocol(directory)
                updater_compatible = target_protocol in (None, update.UPDATER_PROTOCOL)
                target = {**update.identity(record), 'bundle_sha256': record['bundle_sha256']}
        if update.read_state(host, status)['current'] != before:
            raise ValueError('检查期间当前版本发生变化，请重新检查')
        result = {'ok': True, 'checked_at': stamp, 'last_success_at': stamp,
                  'current': before['version'], 'latest': remote['version'], 'available': available,
                  'data_compatible': compatible, 'updater_compatible': updater_compatible, 'target': target,
                  'release_url': f'https://github.com/{config["repository"]}/releases/tag/{remote["version"]}'}
        save_result(status, result)
        return result
    except (ValueError, OSError, KeyError, TypeError, SyntaxError, tarfile.TarError, subprocess.SubprocessError) as error:
        result = {key: value for key, value in previous.items() if key not in {'ok', 'error', 'checked_at'}}
        # 库异常、网络响应和凭据内容不得进入公开文件。
        result.update(ok=False, checked_at=stamp,
                      error=str(error) if isinstance(error, ValueError) else type(error).__name__)
        save_result(status, result)
        return result


def describe(result):
    if not result:
        return '尚无检查记录；运行 tentacle check-updates 或等待定时器。'
    if not result.get('ok'):
        return '最近检查失败：' + result.get('error', '未知错误') + '；上次成功：' + result.get('last_success_at', '无')
    message = f"当前 {result['current']}，最新正式版 {result['latest']}；检查时间 {result['checked_at']}"
    if result['available']:
        message += '\n发现新版本；运行 tentacle update 预览，再用 tentacle update --apply 手动安装。'
        if result.get('data_compatible') is False:
            message += '\n数据兼容代号已变化；请按 Release 的迁移说明处理。'
        if result.get('updater_compatible') is False:
            message += '\n宿主维护协议已变化；请先按发布说明处理维护工具再升级。'
    else:
        message += '\n没有更新版本。'
    return message


def units():
    return {
        CHECK_SERVICE: '''[Unit]
Description=Check Tentacle stable releases (never install)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -E -s /usr/local/libexec/tentacle-update/check_updates.py
TimeoutStartSec=15min
UMask=0077
Nice=10
CPUQuota=50%
MemoryMax=512M
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/usr/local/share/tentacle
PrivateTmp=true
''',
        CHECK_TIMER: '''[Unit]
Description=Check Tentacle stable releases every six hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
RandomizedDelaySec=15min
Persistent=true
Unit=tentacle-update-check.service

[Install]
WantedBy=timers.target
''',
    }


def install_timer(unit_root=Path('/etc/systemd/system')):
    """只管理专用检查单元；拒绝接管同名的第三方文件。"""
    marker = '# Managed by Tentacle update checker\n'
    for name in units():
        path = deploy.checked_path(unit_root, name)
        if path.exists() and not path.read_text().startswith(marker):
            raise ValueError('检查单元已存在且不由 Tentacle 管理：' + name)
    for name, content in units().items():
        deploy.write_file(deploy.checked_path(unit_root, name), (marker + content).encode(), 0o644, owner=(0, 0))
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=30)
    subprocess.run(['systemctl', 'enable', '--now', CHECK_TIMER], check=True, timeout=30)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status-root', type=Path, default=update.DEFAULT_STATUS)
    parser.add_argument('--status', action='store_true', help='离线读取最近检查结果，无需 sudo')
    parser.add_argument('--install-timer', action='store_true', help='安装并启用六小时检查定时器，不安装更新')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.status and args.install_timer:
        parser.error('--status 和 --install-timer 不能同时使用')
    try:
        if args.status:
            result = runtime.read_json(args.status_root, RESULT_FILE)
        else:
            if os.geteuid() != 0:
                raise ValueError('在线检查和安装定时器需要 root；使用 tentacle check-updates')
            if args.install_timer:
                if not runtime.host_config(args.status_root):
                    raise ValueError('须先安装主机维护工具并登记版本')
                update.public_status(args.status_root)
                if args.status_root != update.DEFAULT_STATUS:
                    raise ValueError('定时器只支持全局 /usr/local 安装')
                install_timer()
                print('已启用定时检查；不会自动安装或重启应用。')
                return 0
            result = check(args.status_root)
        print(json.dumps(result, ensure_ascii=False) if args.json else describe(result))
        return 1 if result and not result.get('ok') else 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ValueError) else type(error).__name__},
                         ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
