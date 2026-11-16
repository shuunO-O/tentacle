#!/usr/bin/env python3
"""Plain offline backup of Tentacle + Neo4j. Defaults to preview; --run stops services."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import uuid


APP_SERVICE = 'tentacle.service'
DB_SERVICE = 'neo4j.service'
EXCLUDES = {'.git', '.venv', '.dev-python', 'logs', 'tmp', '.cache', '.pytest_cache', '.ruff_cache', 'node_modules'}
EXTRAS = {
    'host/google_adc.json': Path('/home/tentacle/.config/gcloud/application_default_credentials.json'),
    'host/neo4j.conf': Path('/etc/neo4j/neo4j.conf'),
    'host/tentacle.service': Path('/etc/systemd/system/tentacle.service'),
    'host/neo4j_plugins': Path('/var/lib/neo4j/plugins'),
}


def atomic_json(path, value):
    fd, temp = tempfile.mkstemp(prefix='.json-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def command(argv, timeout=120, stdout=None):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout or subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f'{Path(argv[0]).name} failed (exit {process.returncode}); backup not published')
    return output


def active(service):
    value = command(['systemctl', 'show', service, '-p', 'ActiveState', '--value'], timeout=15).decode().strip()
    if value not in {'active', 'inactive', 'failed'}:
        raise RuntimeError('service is missing or changing state')
    return value == 'active'


def wait_for_database():
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', 7687), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError('Neo4j did not become ready; recovery record retained')


@contextmanager
def locked(output):
    with (output / '.backup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another backup/recovery is running') from None
        yield


def recover(output):
    path = output / '.resume.json'
    if not path.exists():
        return
    state = json.loads(path.read_text())
    services = state['restart']
    if services != [s for s in (DB_SERVICE, APP_SERVICE) if s in services]:
        raise ValueError('invalid recovery service list')
    unit = state.get('dump_unit')
    if unit:
        if not re.fullmatch(r'tentacle-backup-dump-[0-9a-f]{12}\.service', unit):
            raise ValueError('invalid recovery dump unit')
        # A transient dump must be stopped before allowing Neo4j to start.
        subprocess.run(['systemctl', 'stop', unit], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, check=False)
        result = subprocess.run(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.stdout.strip() in (
                b'active', b'activating', b'deactivating'):
            raise RuntimeError('dump still running; recovery record retained')
    for service in services:
        if not active(service):
            command(['systemctl', 'start', service], timeout=180)
        if service == DB_SERVICE:
            wait_for_database()
    path.unlink()
    sync_directory(output)


def archive_filter(info):
    parts = Path(info.name).parts
    if parts and parts[0] == 'app':
        relative = parts[1:]
        if relative and relative[0] in EXCLUDES:
            return None
        if relative and relative[0] != 'data' and '__pycache__' in relative:
            return None
    return info


def archive_app(app, destination, extras):
    # Plain tar avoids compression CPU and preserves Linux ownership/mode and symlinks.
    with tarfile.open(destination, 'w', stream=True) as archive:
        archive.add(app, arcname='app', filter=archive_filter)
        for name, source in extras.items():
            if source.exists():
                archive.add(source, arcname=name)
    with destination.open('rb') as stream:
        os.fsync(stream.fileno())


def dump(database, destination, state_path):
    unit = f'tentacle-backup-dump-{uuid.uuid4().hex[:12]}.service'
    state = json.loads(state_path.read_text())
    state['dump_unit'] = unit
    atomic_json(state_path, state)
    # No concurrent production JVM: callers have already stopped Neo4j.
    # The transient unit gives the CLI a hard limit without installing any service.
    with destination.open('xb') as stream:
        command(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', f'--unit={unit}',
                 '--property=MemoryMax=512M', '--property=MemorySwapMax=0',
                 '--property=OOMScoreAdjust=1000', '--property=RuntimeMaxSec=600',
                 '--uid=neo4j', '--setenv=HEAP_SIZE=128m', '--setenv=NEO4J_CONF=/etc/neo4j',
                 '--', '/usr/bin/neo4j-admin', 'database', 'dump', database, '--to-stdout'],
                timeout=660, stdout=stream)
        stream.flush()
        os.fsync(stream.fileno())
    if destination.stat().st_size == 0:
        raise RuntimeError('Neo4j dump is empty')


def check_space(app, output):
    size = 0
    for source in (app, Path('/var/lib/neo4j/data')):
        for directory, dirs, files in os.walk(source, followlinks=False):
            if source == app and Path(directory) == app:
                dirs[:] = [d for d in dirs if d not in EXCLUDES]
            size += sum((Path(directory) / name).lstat().st_size for name in files)
    if shutil.disk_usage(output).free < size * 2 + 1024**3:
        raise RuntimeError('not enough disk space; services have not been stopped')


def make_backup(app, output, download_user, extras=None):
    if (output / '.resume.json').exists():
        raise RuntimeError('unfinished maintenance: run --recover before making another backup')
    check_space(app, output)
    owner = pwd.getpwnam(download_user)
    restart = [s for s in (DB_SERVICE, APP_SERVICE) if active(s)]
    if APP_SERVICE in restart and DB_SERVICE not in restart:
        raise RuntimeError('Tentacle is running while Neo4j is down; refusing maintenance')
    backup_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    state_path = output / '.resume.json'
    with tempfile.TemporaryDirectory(prefix='.partial-', dir=output) as temporary:
        work = Path(temporary)
        atomic_json(state_path, {'restart': restart, 'backup_id': backup_id})
        try:
            for service in (APP_SERVICE, DB_SERVICE):
                if service in restart:
                    command(['systemctl', 'stop', service], timeout=180)
                if active(service):
                    raise RuntimeError('service did not stop')
            dump('neo4j', work / 'neo4j.dump', state_path)
            dump('system', work / 'system.dump', state_path)
            archive_app(app, work / 'tentacle.tar', EXTRAS if extras is None else extras)
        finally:
            # Also runs on command failure, Ctrl+C and SIGTERM; SIGKILL needs --recover.
            recover(output)
        records = []
        # Hash after services are restored; failed backups never replace latest.json.
        for name in ('neo4j.dump', 'system.dump', 'tentacle.tar'):
            path = work / name
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            records.append({'name': name, 'size_bytes': path.stat().st_size, 'sha256': digest})
        manifest = {'schema_version': 1, 'id': backup_id,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'encrypted': False, 'files': records}
        atomic_json(work / 'manifest.json', manifest)
        for path in work.iterdir():
            path.chmod(0o600)
            os.chown(path, owner.pw_uid, owner.pw_gid)
        work.chmod(0o700)
        os.chown(work, owner.pw_uid, owner.pw_gid)
        final = output / backup_id
        os.rename(work, final)
        sync_directory(output)
        atomic_json(output / 'latest.json', manifest)
        os.chown(output / 'latest.json', owner.pw_uid, owner.pw_gid)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=Path('/home/tentacle/app'))
    parser.add_argument('--output', type=Path, default=Path('/home/ubuntu/tentacle-backups'))
    parser.add_argument('--download-user', default='ubuntu')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--run', action='store_true', help='Stop production services and make a plain backup')
    actions.add_argument('--recover', action='store_true', help='Recover services after interrupted maintenance')
    args = parser.parse_args()
    app, output = args.app_root.resolve(), args.output.resolve()
    if app == output or app in output.parents or output in app.parents:
        parser.error('application and backup directories must be separate')
    if not (args.run or args.recover):
        print(json.dumps({'preview': True, 'app_root': str(app), 'output': str(output),
                          'download_user': args.download_user, 'encrypted': False,
                          'files': ['neo4j.dump', 'system.dump', 'tentacle.tar', 'manifest.json'],
                          'maintenance': 'stop Tentacle, stop Neo4j, dump + tar, restore original services'}))
        return 0
    if os.geteuid() != 0:
        parser.error('--run/--recover require sudo')
    os.umask(0o077)
    if args.run:
        if not app.is_dir():
            parser.error('application directory does not exist')
        owner = pwd.getpwnam(args.download_user)
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(output, owner.pw_uid, owner.pw_gid)
        output.chmod(0o700)
    elif not output.is_dir():
        parser.error('backup output directory does not exist')

    def interrupted(signum, _):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        with locked(output):
            if args.recover:
                recover(output)
                result = {'recovered': True}
            else:
                result = make_backup(app, output, args.download_user)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error_type': type(error).__name__,
                          'message': str(error) if type(error) in (ValueError, RuntimeError) else 'backup failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
