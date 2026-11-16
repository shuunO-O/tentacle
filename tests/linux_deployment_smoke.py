"""Run a synthetic deployment against real systemd on a disposable GitHub runner."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from runtime import maintenance_protocol
from scripts import check_updates, deploy, install_linux, maintenance, release_package, update, update_runtime


def bundle(repo, base, port, version, dependency, healthy=True, include_host_tools=False):
    app = f"""
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
from runtime.paths import get_paths

paths = get_paths()
assert os.getuid() != 0 and paths.state_root == Path.cwd()
assert paths.config_file.is_file() and paths.defaults_file.is_file()
assert version('idna') == {dependency!r}
assert {healthy!r}, 'synthetic startup failure'
(paths.data_dir / 'running-version').write_text({version!r})
assert {version == 'v1.0'!r} or (paths.state_root / 'skills/synthetic/SKILL.md').is_file()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/api/auth/state' else 404)
        self.end_headers()

    def do_POST(self):
        operation_id = self.path.removeprefix('/fixture/restart/')
        assert len(operation_id) == 32
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10)
            connection.connect({str(maintenance_protocol.SOCKET)!r})
            request = dict(id=operation_id, action='restart', version=None)
            connection.sendall((json.dumps(request) + '\\n').encode())
            with connection.makefile('rb') as stream:
                assert json.loads(stream.readline())['accepted'] is True
            self.send_response(202)
            self.end_headers()
            self.wfile.flush()
            connection.sendall(b'1')

HTTPServer(('127.0.0.1', {port}), Handler).serve_forever()
"""
    files = {'run_web.py': app, 'agent/protocol.py': '# synthetic\n',
             'config.defaults.yaml': 'synthetic: true\n', 'requirements.txt': f'idna=={dependency}\n',
             release_package.DATA_EPOCH_FILE: '1\n'}
    for path in release_package.PUBLIC_PROMPTS:
        files[path] = 'public template\n'
    if include_host_tools:
        for name in deploy.UPDATER_FILES:
            content = (SOURCE / 'scripts' / name).read_text(encoding='utf-8')
            if name == 'update.py':
                content += '\n# synthetic host refresh\n'
            files['scripts/' + name] = content
        files['scripts/tentacle.sh'] = (SOURCE / 'scripts/tentacle.sh').read_text(encoding='utf-8') + '\n# synthetic host refresh\n'
        files['runtime/maintenance_protocol.py'] = (SOURCE / 'runtime/maintenance_protocol.py').read_text(encoding='utf-8')
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding='utf-8')
    if include_host_tools:
        (repo / 'scripts/tentacle.sh').chmod(0o755)
    shutil.copyfile(SOURCE / 'runtime/paths.py', repo / 'runtime/paths.py')
    deploy.git(repo, 'add', '.')
    deploy.git(repo, 'commit', '-qm', version)
    deploy.git(repo, 'tag', '-a', version, '-m', version)
    directory = base / version
    release_package.build(repo, version, directory)
    return directory


def check_as_user(*arguments, expected=0):
    result = subprocess.run(['/usr/sbin/runuser', '-u', 'tentacle', '--', *map(str, arguments)], timeout=15)
    assert result.returncode == expected


def smoke(base):
    source = base / 'fixture'
    source.mkdir()
    deploy.git(source, 'init', '-q')
    deploy.git(source, 'config', 'user.name', 'Synthetic')
    deploy.git(source, 'config', 'user.email', 'ci@example.invalid')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    bundles = {
        'v1.0': bundle(source, base, port, 'v1.0', '3.10'),
        'v1.1': bundle(source, base, port, 'v1.1', '3.11'),
        'v1.2': bundle(source, base, port, 'v1.2', '3.11', healthy=False),
        'v1.3': bundle(source, base, port, 'v1.3', '3.11', include_host_tools=True),
    }
    root, home, config, backups = (base / name for name in ('app', 'home', 'etc', 'state'))
    status = update.DEFAULT_STATUS
    args = ['--apply', '--bundle', str(bundles['v1.0']), '--version', 'v1.0',
            '--source', str(SOURCE),
            '--production', str(root), '--home', str(home), '--config-root', str(config),
            '--backups', str(backups), '--port', str(port)]
    assert install_linux.main(args) == 0
    check_as_user('/usr/bin/mkdir', '-p', home / 'skills/synthetic')
    fixtures = {'config/config.yaml': 'synthetic: preserved\n', 'config/.env': 'SYNTHETIC=retained\n',
                'data/attachment': 'synthetic binary marker\n', 'skills/synthetic/SKILL.md': '# Skill\n',
                'workspace/work.txt': 'workspace marker\n'}
    for name, value in fixtures.items():
        check_as_user('/usr/bin/python3', '-c',
                      'from pathlib import Path; import os,sys; os.umask(0o077); Path(sys.argv[1]).write_text(sys.argv[2])',
                      home / name, value)
    assert install_linux.main(args) == 0
    host = update_runtime.host_config(status)
    old = update.read_state(host, status)
    check_as_user('/usr/bin/test', '-r', root / 'current/run_web.py')
    for path in (root, root / 'releases', root / 'current', root / 'current/run_web.py',
                 root / 'current/.venv', backups):
        check_as_user('/usr/bin/test', '-w', path, expected=1)
    assert not (config / 'github-token').exists()
    assert json.loads((config / 'update.json').read_text(encoding='utf-8')) == {'repository': 'shuunO-O/tentacle'}
    assert (home / 'config/.env').stat().st_mode & 0o777 == 0o600
    subprocess.run(['systemctl', 'is-enabled', '--quiet', maintenance.SOCKET_UNIT], check=True, timeout=15)
    check_as_user('/usr/bin/python3', '-c', """
import json, socket, sys
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(10)
    connection.connect(sys.argv[1])
    connection.sendall((json.dumps(dict(id='a' * 32, action='restart', version=None)) + '\\n').encode())
    with connection.makefile('rb') as stream:
        reply = json.loads(stream.readline())
    assert reply['accepted'] is False and '主进程' in reply['error']
""", maintenance_protocol.SOCKET)
    old_pid = update_runtime.service_info(host)['MainPID']
    with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/fixture/restart/' + 'b' * 32,
                                                       method='POST'), timeout=15) as response:
        assert response.status == 202
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        operations = update_runtime.read_json(status, maintenance_protocol.OPERATIONS) or []
        if operations and operations[0]['state'] != 'running':
            break
        time.sleep(0.2)
    assert operations and operations[0]['id'] == 'b' * 32 and operations[0]['state'] == 'succeeded', operations
    assert update_runtime.service_info(host)['MainPID'] != old_pid

    class LocalRelease:
        def __init__(self, *args):
            pass

        def select(self, version):
            return version

        def download(self, version, directory):
            shutil.copytree(bundles[version], directory)
            return update.identity(json.loads((directory / 'manifest.json').read_bytes()))

        def assert_unchanged(self, snapshot):
            pass

    with patch.object(update.github_releases, 'Client', LocalRelease):
        bundle_dir = bundles['v1.1']
        selected = update.bundle_record(bundle_dir, json.loads((bundle_dir / 'manifest.json').read_bytes()))
        target = {**update.identity(selected), 'bundle_sha256': selected['bundle_sha256']}
        check_updates.save_result(status, {'ok': True, 'current': 'v1.0', 'latest': 'v1.1',
                                           'available': True, 'data_compatible': True, 'target': target})
        request = {'action': 'update', 'version': 'v1.1'}
        checked = maintenance.checked_target(status, 'v1.0', 'v1.1')
        with deploy.deployment_lock(backups):
            try:
                maintenance.run_operation(status, host, request, old, {**checked, 'bundle_sha256': 'a' * 64})
            except ValueError as error:
                assert '包已变化' in str(error)
            else:
                raise AssertionError('changed bundle was allowed')
            assert update.read_state(host, status) == old
            maintenance.run_operation(status, host, request, old, checked)
        assert (home / 'data/running-version').read_text() == 'v1.1'
        with patch('builtins.input', return_value='v1.2'):
            assert update.main(['update', 'v1.2', '--apply', '--timeout', '8']) == 1
    active = update.read_state(host, status)
    assert active['current']['version'] == 'v1.1'
    assert (home / 'data/running-version').read_text() == 'v1.1'

    command = '/usr/local/bin/tentacle'
    subprocess.run([command, 'revert', '--apply', '--timeout', '20'],
                   input='v1.0\n', text=True, check=True, timeout=90)
    assert (home / 'data/running-version').read_text() == 'v1.0'

    # Simulate a host interrupted after the link changes and before state is published.
    before = update.read_state(host, status)
    update_runtime.write_json(backups, update_runtime.PENDING_FILE,
                              {'schema': update.UPDATER_PROTOCOL, 'before': before,
                               'target': active['current'], 'http_status': 200})
    update_runtime.systemctl(host, 'stop')
    update.switch_release(root, active['current'])
    subprocess.run([command, 'update', '--recover', '--timeout', '20'], check=True, timeout=90)
    assert update.read_state(host, status) == before
    update_runtime.systemctl(host, 'restart')
    subprocess.run(['systemctl', 'is-enabled', '--quiet', host['service']], check=True, timeout=15)
    update_runtime.wait_healthy(host, update.installed_release(root, old['current']), 200, timeout=20)
    for name, value in fixtures.items():
        assert (home / name).read_text() == value
    assert subprocess.check_output([command, 'version'], text=True).strip() == 'v1.0'

    with patch.object(update.github_releases, 'Client', LocalRelease), patch('builtins.input', return_value='v1.3'):
        assert update.main(['update', 'v1.3', '--apply', '--timeout', '20']) == 0
    assert (home / 'data/running-version').read_text() == 'v1.3'
    assert subprocess.check_output([command, 'version'], text=True).strip() == 'v1.3'
    assert '# synthetic host refresh' in Path(command).read_text(encoding='utf-8')
    assert '# synthetic host refresh' in Path('/usr/local/libexec/tentacle-update/update.py').read_text(encoding='utf-8')
    assert update_runtime.read_json(Path('/usr/local/libexec/tentacle-update'), 'install.json')['version'] == 'v1.3'
    assert any(Path('/usr/local/libexec/tentacle-update-backups').iterdir())


if __name__ == '__main__':
    if os.geteuid() != 0 or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('Only run on a disposable GitHub-hosted runner as root')
    if any(Path(path).exists() for path in ('/usr/local/bin/tentacle', '/etc/systemd/system/tentacle.service')):
        raise SystemExit('Expected a disposable host without Tentacle')
    with tempfile.TemporaryDirectory(prefix='tentacle-ci-', dir='/var/lib') as temporary:
        base = Path(temporary)
        base.chmod(0o755)
        try:
            smoke(base)
        except Exception:
            subprocess.run(['journalctl', '-u', 'tentacle.service', '-u', 'tentacle-maintenance@*',
                            '-n', '55', '--no-pager'],
                           check=False, timeout=15)
            raise
        finally:
            if Path('/etc/systemd/system/tentacle.service').exists():
                subprocess.run(['systemctl', 'disable', '--now', 'tentacle.service', 'tentacle-update-check.timer',
                                maintenance.SOCKET_UNIT],
                               check=False, timeout=40)
