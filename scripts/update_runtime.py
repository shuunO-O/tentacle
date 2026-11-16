"""Root-owned host configuration, systemd checks and atomic state writes."""

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import signal
import ssl
import subprocess
import time

if __package__:
    from . import deploy
else:
    import deploy


STATE_FILE = 'current.json'
PENDING_FILE = '.update-pending.json'


def host_config(directory):
    path = deploy.checked_path(directory, 'host.json')
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('主机参数须由 root 所有且禁止其他用户写入')
    value = json.loads(path.read_bytes())
    validate_host(value)
    return value


def validate_host(value):
    if not isinstance(value, dict) or set(value) != {
        'schema', 'production', 'backups', 'config', 'service', 'health_port', 'home', 'user', 'python'
    } or value['schema'] != 2:
        raise ValueError('主机参数格式不符；旧部署须先按迁移清单切换')
    for key in ('production', 'backups', 'config', 'home', 'python'):
        path = value[key]
        if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', path) or '..' in Path(path).parts:
            raise ValueError('主机路径须为无空格的规范绝对路径')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', value['user']) or value['user'] == 'root':
        raise ValueError('应用用户必须为普通 Linux 用户')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service', value['service']):
        raise ValueError('服务名须为 systemd .service 单元')
    if type(value['health_port']) is not int or not 1 <= value['health_port'] <= 65535:
        raise ValueError('健康检查端口须在 1 到 65535 之间')


def read_json(root, name):
    path = deploy.checked_path(root, name)
    return json.loads(path.read_bytes()) if path.exists() else None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(root, name, value, mode=0o600):
    path = deploy.checked_path(root, name)
    deploy.write_file(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode(), mode)
    sync_directory(path.parent)


def root_directory(path, mode=0o755):
    if path.resolve() != path:
        raise ValueError('维护目录必须为真实绝对路径')
    for directory in reversed((path, *path.parents)):
        if not directory.exists():
            directory.mkdir(mode=mode if directory == path else 0o755)
        info = directory.stat()
        if not directory.is_dir() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f'维护路径 {directory} 必须由 root 控制且禁止其他用户写入')
    return path


def systemctl(host, action):
    subprocess.run(['systemctl', action, host['service']], check=True, timeout=150,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def service_info(host):
    properties = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'WorkingDirectory', 'User')
    result = subprocess.run(['systemctl', 'show', host['service'], *[f'--property={key}' for key in properties]],
                            check=True, capture_output=True, text=True, timeout=10)
    info = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if (info.get('LoadState') != 'loaded' or info.get('WorkingDirectory') != host['home']
            or info.get('User') != host['user']):
        raise ValueError('systemd 服务身份或工作目录与部署记录不符')
    return info


def app_processes(host):
    uid = pwd.getpwnam(host['user']).pw_uid
    if not uid:
        raise ValueError('应用不能使用 root 账号')
    found = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != uid:
                continue
            arguments = (directory / 'cmdline').read_bytes().split(b'\0')[1:]
            entry = next((Path(os.fsdecode(arg)) for arg in arguments
                          if Path(os.fsdecode(arg)).name == 'run_web.py'), None)
            if entry is None:
                continue
            if not entry.is_absolute():
                entry = (directory / 'cwd').resolve(strict=True) / entry
            found[int(directory.name)] = entry.resolve()
        except (OSError, ValueError):
            continue
    return found


def require_running(host, release):
    info = service_info(host)
    pid = int(info.get('MainPID', '0'))
    if (info.get('ActiveState') != 'active' or info.get('SubState') != 'running' or pid <= 0
            or app_processes(host) != {pid: release / 'run_web.py'}):
        raise ValueError('运行进程与唯一目标版本的 systemd 主进程不符')


def require_stopped(host):
    info = service_info(host)
    if info.get('ActiveState') not in {'inactive', 'failed'} or info.get('MainPID') != '0' or app_processes(host):
        raise ValueError('应用仍有活动进程，请先停止唯一生产实例')


def http_status(host):
    for secure in (False, True):
        connection = (http.client.HTTPSConnection('127.0.0.1', host['health_port'], timeout=2,
                                                   context=ssl._create_unverified_context())
                      if secure else http.client.HTTPConnection('127.0.0.1', host['health_port'], timeout=2))
        try:
            connection.request('GET', '/api/auth/state')
            status = connection.getresponse().status
        except (ConnectionResetError, http.client.RemoteDisconnected):
            if not secure:
                continue
            raise
        finally:
            connection.close()
        if status in (200, 403):
            return status
        break
    raise ValueError('鉴权状态端点未返回预期的 200 或 403')


def wait_healthy(host, release, expected=None, timeout=90):
    deadline = time.monotonic() + timeout
    successes = 0
    while time.monotonic() < deadline:
        try:
            require_running(host, release)
            if http_status(host) not in ((200, 403) if expected is None else (expected,)):
                raise ValueError('更新前后的鉴权状态发生变化')
            successes += 1
            if successes >= 2:
                return
        except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException):
            successes = 0
        time.sleep(1)
    raise ValueError('服务启动验证超时，请检查 systemd 和应用日志')


@contextmanager
def interruption_handlers():
    def interrupted(signum, _):
        raise InterruptedError(f'维护操作收到信号 {signum}')

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def clear_pending(backups):
    deploy.checked_path(backups, PENDING_FILE).unlink()
    sync_directory(backups)
