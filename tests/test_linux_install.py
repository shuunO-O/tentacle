"""Linux installer unit tests use synthetic files and mocked systemd."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip('pwd')

from scripts import check_updates, install_linux


def args_for(tmp_path, **changes):
    values = dict(user='tentacle', production=tmp_path / 'opt/tentacle', backups=tmp_path / 'var/releases',
                  config_root=tmp_path / 'etc/tentacle', home=tmp_path / 'home/tentacle', service='companion.service',
                  port=9842, version=None, repository='example/repo', token_file=None,
                  python=None, bundle=None, skip_system_packages=True)
    return SimpleNamespace(**(values | changes))


def test_preview_needs_no_token_network_or_writes(tmp_path, capsys):
    with patch.object(install_linux, 'install', side_effect=AssertionError('preview cannot install')), \
            patch.object(install_linux.github_releases, 'read_token', side_effect=AssertionError('no secrets')):
        assert install_linux.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['preview']
    assert result['token_file'] is None
    assert result['host']['user'] == 'tentacle' and result['host']['home'] == '/home/tentacle'
    assert result['host']['production'] == '/opt/tentacle'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('change', [
    {'production': Path('/etc')}, {'user': 'root'}, {'user': 'bad\nExecStart=bad'},
    {'service': 'bad.service\nExecStart=bad'}, {'production': Path('/srv/bad path/app')},
    {'production': Path('relative/app')}, {'port': 0},
])
def test_install_rejects_unsafe_host_parameters_before_mutation(tmp_path, change):
    with pytest.raises(ValueError):
        install_linux.specification(args_for(tmp_path, **change))
    assert not list(tmp_path.iterdir())


def test_nested_and_symlink_paths_cannot_alias_maintenance_or_user_data(tmp_path):
    for change in ({'backups': tmp_path / 'opt/tentacle/releases'}, {'backups': tmp_path / 'home/tentacle/releases'},
                   {'home': tmp_path / 'opt/tentacle/user'}, {'home': tmp_path / 'opt/tentacle'}):
        with pytest.raises(ValueError, match='必须分离'):
            install_linux.specification(args_for(tmp_path, **change))
    target = tmp_path / 'real'
    target.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        install_linux.specification(args_for(tmp_path, production=alias / 'app'))


def test_unit_separates_working_directory_and_read_only_program(tmp_path):
    host = install_linux.specification(args_for(tmp_path))['host']
    unit = install_linux.application_unit(host)
    assert 'User=tentacle\n' in unit
    assert f'WorkingDirectory={host["home"]}\n' in unit
    assert f'Environment=TENTACLE_HOME={host["home"]}\n' in unit
    assert f'{host["production"]}/current/.venv/bin/python' in unit
    assert f'{host["production"]}/current/run_web.py' in unit
    assert 'EnvironmentFile=' not in unit


def test_existing_application_requires_manual_migration(tmp_path, monkeypatch):
    args = args_for(tmp_path)
    args.production.mkdir(parents=True)
    (args.production / 'run_web.py').write_text('# synthetic existing app\n')

    def root_directory(path, mode=0o755):
        path.mkdir(mode=mode, exist_ok=True, parents=True)
        return path

    monkeypatch.setattr(install_linux.runtime, 'root_directory', root_directory)
    monkeypatch.setattr(install_linux.deploy, 'revision', lambda *_: 'a' * 40)
    with pytest.raises(ValueError, match='迁移清单'):
        install_linux.install(args, tmp_path, install_linux.specification(args), prefix=tmp_path / 'usr/local',
                              unit_root=tmp_path / 'units')
    assert (args.production / 'run_web.py').read_text() == '# synthetic existing app\n'
    assert not (args.config_root / 'linux-install.json').exists()


def test_checker_timer_conflict_is_detected_before_any_unit_write(tmp_path):
    conflict = tmp_path / check_updates.CHECK_TIMER
    conflict.write_text('# someone else owns this\n')
    with patch.object(check_updates.subprocess, 'run', side_effect=AssertionError('no systemctl')):
        with pytest.raises(ValueError, match='不由 Tentacle 管理'):
            check_updates.install_timer(tmp_path)
    assert not (tmp_path / check_updates.CHECK_SERVICE).exists()


def test_systemd_accepts_generated_service_and_timer_definitions(tmp_path):
    if not shutil.which('systemd-analyze'):
        pytest.skip('systemd unit validation requires Linux systemd tools')
    host = install_linux.specification(args_for(tmp_path))['host']
    python = Path(host['production']) / 'current/.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    checker = tmp_path / 'check_updates.py'
    checker.touch()
    units = check_updates.units()
    units[check_updates.CHECK_SERVICE] = units[check_updates.CHECK_SERVICE].replace(
        '/usr/local/libexec/tentacle-update/check_updates.py', str(checker))
    for name, content in {host['service']: install_linux.application_unit(host), **units}.items():
        (tmp_path / name).write_text(content)
    result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(tmp_path / host['service']),
                             str(tmp_path / check_updates.CHECK_SERVICE), str(tmp_path / check_updates.CHECK_TIMER)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
