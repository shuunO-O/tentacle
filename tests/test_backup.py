"""All backups use tiny synthetic files; every service/Neo4j command is mocked."""

import hashlib
import json
import pwd
import tarfile
from types import SimpleNamespace

import pytest

from scripts import backup


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    app, output = tmp_path / 'app', tmp_path / 'backups'
    app.mkdir()
    output.mkdir()
    (app / '.env').write_text('SYNTHETIC=example\n')
    (app / '.env').chmod(0o600)
    data = app / 'data/files/logs'
    data.mkdir(parents=True)
    (data / 'example.txt').write_text('synthetic attachment')
    for name in ('logs', '.venv', 'tmp'):
        directory = app / name
        directory.mkdir()
        (directory / 'excluded').write_text('synthetic')
    states = {backup.APP_SERVICE: True, backup.DB_SERVICE: True}
    calls = []

    def command(argv, **kwargs):
        assert argv[0] == 'systemctl'
        _, action, service = argv
        calls.append((action, service))
        states[service] = action == 'start'

    def dump(database, destination, state_path):
        assert not any(states.values())
        assert state_path.exists()
        calls.append(('dump', database))
        destination.write_bytes(b'synthetic dump ' + database.encode())

    monkeypatch.setattr(backup, 'active', lambda service: states[service])
    monkeypatch.setattr(backup, 'command', command)
    monkeypatch.setattr(backup, 'dump', dump)
    monkeypatch.setattr(backup, 'wait_for_database', lambda: None)
    monkeypatch.setattr(backup, 'check_space', lambda *_: None)
    monkeypatch.setattr(backup.os, 'chown', lambda *_: None)
    return app, output, states, calls


def run_backup(host):
    app, output, _, _ = host
    return backup.make_backup(app, output, pwd.getpwuid(backup.os.getuid()).pw_name, extras={})


def test_plain_backup_and_checksums(fake_host):
    app, output, states, calls = fake_host
    result = run_backup(fake_host)
    folder = output / result['id']
    assert calls == [('stop', backup.APP_SERVICE), ('stop', backup.DB_SERVICE),
                     ('dump', 'neo4j'), ('dump', 'system'),
                     ('start', backup.DB_SERVICE), ('start', backup.APP_SERVICE)]
    assert all(states.values())
    assert result['encrypted'] is False
    assert json.loads((output / 'latest.json').read_text()) == result
    assert json.loads((folder / 'manifest.json').read_text()) == result
    for entry in result['files']:
        path = folder / entry['name']
        assert path.stat().st_mode & 0o777 == 0o600
        assert entry['size_bytes'] == path.stat().st_size
        assert entry['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    with tarfile.open(folder / 'tentacle.tar') as archive:
        assert archive.extractfile('app/.env').read() == b'SYNTHETIC=example\n'
        assert archive.getmember('app/.env').mode == 0o600
        assert archive.extractfile('app/data/files/logs/example.txt').read() == b'synthetic attachment'
        assert 'app/logs' not in archive.getnames()
        assert 'app/.venv' not in archive.getnames()
    assert not list(output.glob('.partial-*'))
    assert not (output / '.resume.json').exists()


@pytest.mark.parametrize('failure', ['dump', 'archive'])
def test_failure_resumes_services_and_preserves_previous_backup(fake_host, monkeypatch, failure):
    _, output, states, _ = fake_host
    previous = run_backup(fake_host)

    def fail(*_):
        raise RuntimeError('synthetic failure')

    monkeypatch.setattr(backup, 'dump' if failure == 'dump' else 'archive_app', fail)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        run_backup(fake_host)
    assert all(states.values())
    assert json.loads((output / 'latest.json').read_text()) == previous
    assert (output / previous['id'] / 'neo4j.dump').exists()
    assert not list(output.glob('.partial-*'))


def test_recovery_failure_retains_record_and_does_not_publish(fake_host, monkeypatch):
    _, output, states, _ = fake_host

    def fail():
        raise RuntimeError('synthetic database startup failure')

    monkeypatch.setattr(backup, 'wait_for_database', fail)
    with pytest.raises(RuntimeError, match='startup failure'):
        run_backup(fake_host)
    assert (output / '.resume.json').exists()
    assert not states[backup.APP_SERVICE]
    assert not (output / 'latest.json').exists()
    with pytest.raises(RuntimeError, match='--recover'):
        run_backup(fake_host)
    monkeypatch.setattr(backup, 'wait_for_database', lambda: None)
    backup.recover(output)
    assert all(states.values())
    assert not (output / '.resume.json').exists()


def test_previously_stopped_app_stays_stopped(fake_host):
    _, _, states, _ = fake_host
    states[backup.APP_SERVICE] = False
    run_backup(fake_host)
    assert states == {backup.APP_SERVICE: False, backup.DB_SERVICE: True}


def test_lock_prevents_concurrent_backups(tmp_path):
    with backup.locked(tmp_path):
        with pytest.raises(RuntimeError, match='another backup'):
            with backup.locked(tmp_path):
                pytest.fail('overlapping lock acquired')


def test_preview_does_not_touch_files_or_services(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['backup.py', '--app-root', str(tmp_path / 'missing-app'),
                                   '--output', str(tmp_path / 'missing-output')])
    monkeypatch.setattr(backup, 'command', lambda *_: pytest.fail('executed host command'))
    assert backup.main() == 0
    assert json.loads(capsys.readouterr().out)['preview'] is True
    assert not list(tmp_path.iterdir())


def test_dump_is_streamed_and_memory_limited(tmp_path, monkeypatch):
    state = tmp_path / 'resume.json'
    state.write_text(json.dumps({'restart': []}))
    calls = []

    def command(argv, timeout, stdout):
        calls.append(argv)
        stdout.write(b'synthetic dump')

    monkeypatch.setattr(backup, 'command', command)
    backup.dump('neo4j', tmp_path / 'neo4j.dump', state)
    assert '--property=MemoryMax=512M' in calls[0]
    assert '--property=MemorySwapMax=0' in calls[0]
    assert calls[0][-4:] == ['database', 'dump', 'neo4j', '--to-stdout']
    assert json.loads(state.read_text())['dump_unit'].startswith('tentacle-backup-dump-')


def test_disk_failure_does_not_stop_services(fake_host, monkeypatch):
    def fail(*_):
        raise RuntimeError('not enough disk space')

    monkeypatch.setattr(backup, 'check_space', fail)
    with pytest.raises(RuntimeError, match='disk space'):
        run_backup(fake_host)
    assert fake_host[3] == []
    assert all(fake_host[2].values())


def test_recovery_handles_collected_dump_unit(fake_host, monkeypatch):
    _, output, states, calls = fake_host
    states.update({backup.APP_SERVICE: False, backup.DB_SERVICE: False})
    state = {'restart': [backup.DB_SERVICE, backup.APP_SERVICE],
             'dump_unit': 'tentacle-backup-dump-123456abcdef.service'}
    (output / '.resume.json').write_text(json.dumps(state))
    unit_calls = []

    def run(argv, **_):
        unit_calls.append(argv)
        return SimpleNamespace(returncode=1, stdout=b'')  # Already collected by systemd.

    monkeypatch.setattr(backup.subprocess, 'run', run)
    backup.recover(output)
    assert unit_calls[0] == ['systemctl', 'stop', state['dump_unit']]
    assert all(states.values())
    assert calls == [('start', backup.DB_SERVICE), ('start', backup.APP_SERVICE)]


def test_ctrl_c_resumes_services_without_publishing(fake_host, monkeypatch):
    def interrupted(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr(backup, 'dump', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_backup(fake_host)
    assert all(fake_host[2].values())
    assert not (fake_host[1] / 'latest.json').exists()
