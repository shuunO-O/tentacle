#!/usr/bin/env python3
"""Run one delegated operation from the systemd app process on a separate root unit."""

from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import subprocess
import sys
import tarfile

if not __package__ and not Path(__file__).with_name('runtime').is_dir():
    # Source checkout entry points are executed from scripts/; the installed worker has runtime/ beside it.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import maintenance_protocol as protocol

if __package__:
    from . import check_updates, deploy, update, update_runtime as runtime
else:
    import check_updates
    import deploy
    import update
    import update_runtime as runtime


SOCKET_UNIT = 'tentacle-maintenance.socket'
SERVICE_UNIT = 'tentacle-maintenance@.service'
ERRORS = (ValueError, OSError, KeyError, TypeError, tarfile.TarError,
          subprocess.SubprocessError, http.client.HTTPException)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def checked_target(status, current, version):
    result = runtime.read_json(status, check_updates.RESULT_FILE) or {}
    target = result.get('target')
    if (not result.get('ok') or not result.get('available') or result.get('data_compatible') is not True
            or result.get('updater_compatible') is False
            or result.get('current') != current or result.get('latest') != version
            or not target or target.get('version') != version):
        raise ValueError('请重新检查更新并选择数据兼容的正式版本')
    return target


def run_operation(status, host, request, before, target):
    action = request['action']
    if action == 'check':
        result = check_updates.check(status)
        if not result['ok']:
            raise ValueError(result['error'])
        return
    if action == 'restart':
        release = update.installed_release(Path(host['production']), before['current'])
        runtime.require_running(host, release)
        expected = runtime.http_status(host)
        runtime.systemctl(host, 'restart')
        runtime.wait_healthy(host, release, expected)
        return

    def approve_checked(preview):
        if preview['target'] != update.identity(target):
            raise ValueError('Release 标识已变化，请重新检查更新')
        return True

    result = update.change_version(status, host, 'update', request['version'],
                                   confirm=approve_checked, expected_bundle=target['bundle_sha256'])
    if result is None:
        raise ValueError('目标版本已变化，请重新检查更新')


def reply(connection, value):
    connection.sendall((json.dumps(value, ensure_ascii=False) + '\n').encode())


def receive(connection, host):
    pid, uid, _ = struct.unpack('3i', connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    if not uid or uid != pwd.getpwnam(host['user']).pw_uid:
        raise ValueError('仅允许生产服务发起维护')
    with connection.makefile('rb') as incoming:
        raw = incoming.readline(protocol.REQUEST_LIMIT + 1)
    if len(raw) > protocol.REQUEST_LIMIT or not raw.endswith(b'\n'):
        raise ValueError('维护请求格式错误')
    try:
        request = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('维护请求格式错误') from None
    if (not isinstance(request, dict) or set(request) != {'id', 'action', 'version'}
            or not protocol.valid_id(request['id'])):
        raise ValueError('维护请求格式错误')
    protocol.validate_action(request['action'], request['version'])
    if pid != int(runtime.service_info(host)['MainPID']):
        raise ValueError('仅允许生产主进程发起维护')
    return request


def handle(connection, status=protocol.STATUS_ROOT):
    connection.settimeout(12)
    try:
        host = runtime.host_config(status)
        request = receive(connection, host)
        backups = runtime.root_directory(Path(host['backups']), 0o700)
        with deploy.deployment_lock(backups), runtime.interruption_handlers():
            before = update.read_state(host, status)
            target = (checked_target(status, before['current']['version'], request['version'])
                      if request['action'] == 'update' else None)
            operations = runtime.read_json(status, protocol.OPERATIONS) or []
            if any(record['id'] == request['id'] for record in operations):
                raise ValueError('维护操作已经登记')
            record = {**request, 'state': 'running', 'started_at': stamp(),
                      'worker_pid': os.getpid(), 'worker_identity': protocol.worker_identity(os.getpid())}
            recent = [record, *operations[:11]]
            runtime.write_json(status, protocol.OPERATIONS, recent, 0o644)
            try:
                reply(connection, {'accepted': True})
                # The root job starts only after the caller receives the acknowledgement.
                if connection.recv(1) != b'1':
                    raise ValueError('维护请求未完成交接')
                connection.close()
                run_operation(status, host, request, before, target)
                record['state'] = 'succeeded'
            except update.ReleaseRolledBack as error:
                record.update(state='rolled_back', error=str(error))
            except ERRORS as error:
                record.update(state='failed', error=str(error) if isinstance(error, ValueError)
                              else type(error).__name__)
            finally:
                record.pop('worker_pid')
                record.pop('worker_identity')
                record['finished_at'] = stamp()
                runtime.write_json(status, protocol.OPERATIONS, recent, 0o644)
    except ERRORS as error:
        try:
            reply(connection, {'accepted': False, 'error': '已有主机维护操作正在运行'
                  if isinstance(error, BlockingIOError) else str(error) if isinstance(error, ValueError)
                  else '主机维护服务不可用'})
        except OSError:
            pass


def units(host, prefix):
    group = pwd.getpwnam(host['user']).pw_gid
    return {
        SOCKET_UNIT: f"""[Unit]
Description=Tentacle production maintenance requests

[Socket]
ListenStream={protocol.SOCKET}
Accept=yes
SocketMode=0660
SocketGroup={group}
MaxConnections=4
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
""",
        SERVICE_UNIT: f"""[Unit]
Description=Run a Tentacle production maintenance request

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -E -s {prefix}/libexec/tentacle-update/maintenance.py
StandardInput=socket
StandardOutput=journal
StandardError=journal
TimeoutStartSec=60min
PrivateTmp=true
""",
    }


def install_socket(host, prefix=Path('/usr/local'), unit_root=Path('/etc/systemd/system')):
    marker = '# Managed by Tentacle maintenance\n'
    payloads = units(host, prefix)
    for name in payloads:
        path = deploy.checked_path(unit_root, name)
        if path.exists() and not path.read_text().startswith(marker):
            raise ValueError('维护单元已存在且不由 Tentacle 管理：' + name)
    for name, content in payloads.items():
        deploy.write_file(deploy.checked_path(unit_root, name), (marker + content).encode(), 0o644, owner=(0, 0))
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=30)
    subprocess.run(['systemctl', 'enable', '--now', SOCKET_UNIT], check=True, timeout=30)


if __name__ == '__main__':
    os.umask(0o022)
    with socket.socket(fileno=0) as connection:
        handle(connection)
