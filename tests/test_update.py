"""Host tool installation and release locking use only temporary directories."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip('pwd')

from scripts import deploy, install_update, update, update_runtime


class HostEntryTests(unittest.TestCase):
    def test_installer_backs_up_host_command_and_installs_static_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / 'bin').mkdir()
            command = prefix / 'bin/tentacle'
            command.write_bytes(b'old host command\n')
            result = install_update.install(Path(__file__).resolve().parents[1], prefix)
            backup = Path(result['backup'])
            self.assertEqual((backup / 'bin/tentacle').read_bytes(), b'old host command\n')
            self.assertEqual(command.stat().st_mode & 0o777, 0o755)
            self.assertEqual((prefix / 'libexec/tentacle-update/update.py').stat().st_mode & 0o777, 0o644)
            self.assertFalse(result['service_changed'])
            parsed = subprocess.run(['sh', '-n', str(command)], capture_output=True)
            self.assertEqual(parsed.returncode, 0)
            help_result = subprocess.run(['sh', str(command), 'help'], capture_output=True, text=True)
            self.assertIn('tentacle update', help_result.stdout)

    def test_installer_failure_restores_previous_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / 'bin').mkdir()
            command = prefix / 'bin/tentacle'
            command.write_bytes(b'old host command\n')
            write_file = deploy.write_file

            def failed(path, *args, **kwargs):
                if path == command:
                    raise OSError('install failed')
                return write_file(path, *args, **kwargs)

            with patch.object(deploy, 'write_file', side_effect=failed):
                with self.assertRaises(OSError):
                    install_update.install(Path(__file__).resolve().parents[1], prefix)
            self.assertEqual(command.read_bytes(), b'old host command\n')
            self.assertFalse((prefix / 'libexec/tentacle-update/update.py').exists())


def test_release_lock_serializes_install_switch_and_recovery(tmp_path):
    with deploy.deployment_lock(tmp_path):
        with pytest.raises(BlockingIOError):
            with deploy.deployment_lock(tmp_path):
                pytest.fail('concurrent mutation entered the lock')


def test_maintenance_write_refuses_symlink_parents(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'alias').symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        deploy.checked_path(tmp_path, 'alias/current.json')


def test_install_from_release_syncs_changes_skips_unchanged_and_rolls_back(tmp_path, monkeypatch):
    source_root = Path(__file__).resolve().parents[1]
    prefix = tmp_path / 'usr-local'
    install_update.install(source_root, prefix)
    monkeypatch.setattr(update_runtime, 'root_directory', lambda path, mode=0o755: (path.mkdir(mode=mode, parents=True, exist_ok=True), path)[1])
    update_runtime.write_json(
        prefix / 'libexec/tentacle-update',
        'install.json',
        {'protocol': update.UPDATER_PROTOCOL, 'source_commit': 'a' * 40},
        0o644,
    )

    release_dir = tmp_path / 'release'
    for name in deploy.UPDATER_FILES:
        dest = release_dir / 'scripts' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / 'scripts' / name, dest)
    shutil.copy2(source_root / 'scripts/tentacle.sh', release_dir / 'scripts/tentacle.sh')
    (release_dir / 'runtime').mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_root / 'runtime/maintenance_protocol.py', release_dir / 'runtime/maintenance_protocol.py')

    meta = {'protocol': update.UPDATER_PROTOCOL, 'source_commit': 'b' * 40, 'version': 'v1.1'}
    backups_before = list((prefix / 'libexec/tentacle-update-backups').iterdir())
    unchanged = install_update.install_from_release(release_dir, prefix, meta)
    assert unchanged['changed'] is False and list((prefix / 'libexec/tentacle-update-backups').iterdir()) == backups_before

    (release_dir / 'scripts/tentacle.sh').write_text(
        (source_root / 'scripts/tentacle.sh').read_text(encoding='utf-8') + '\n# updated-sh\n',
        encoding='utf-8',
    )
    updated = install_update.install_from_release(release_dir, prefix, meta)
    assert updated['changed'] is True
    assert '# updated-sh' in (prefix / 'bin/tentacle').read_text(encoding='utf-8')
    assert json.loads((prefix / 'libexec/tentacle-update/install.json').read_text(encoding='utf-8'))['version'] == 'v1.1'

    with pytest.raises(ValueError, match='不符'):
        install_update.install_from_release(release_dir, prefix, {**meta, 'protocol': update.UPDATER_PROTOCOL - 1})

    update_runtime.write_json(
        prefix / 'libexec/tentacle-update',
        'install.json',
        {'protocol': update.UPDATER_PROTOCOL + 1, 'source_commit': 'a' * 40},
        0o644,
    )
    with pytest.raises(ValueError, match='降级'):
        install_update.install_from_release(release_dir, prefix, meta)
    update_runtime.write_json(
        prefix / 'libexec/tentacle-update',
        'install.json',
        {'protocol': update.UPDATER_PROTOCOL, 'source_commit': 'a' * 40},
        0o644,
    )

    (release_dir / 'scripts/update.py').write_text('def broken(:\n', encoding='utf-8')
    with pytest.raises(ValueError, match='语法无效'):
        install_update.install_from_release(release_dir, prefix, meta)

    shutil.copy2(source_root / 'scripts/update.py', release_dir / 'scripts/update.py')
    (release_dir / 'scripts/update.py').write_text(
        (source_root / 'scripts/update.py').read_text(encoding='utf-8') + '\n# second-update\n',
        encoding='utf-8',
    )
    write_file = deploy.write_file

    def fail_on_command(path, *args, **kwargs):
        if path == prefix / 'bin/tentacle':
            raise OSError('disk full')
        return write_file(path, *args, **kwargs)

    with patch.object(deploy, 'write_file', side_effect=fail_on_command):
        with pytest.raises(OSError, match='disk full'):
            install_update.install_from_release(release_dir, prefix, meta)
    assert '# second-update' not in (prefix / 'libexec/tentacle-update/update.py').read_text(encoding='utf-8')
