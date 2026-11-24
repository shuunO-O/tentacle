"""主机鉴权脚本只操作 tmp_path；不运行生产脚本或读取真实设备列表。"""

import json
import time
from types import SimpleNamespace

import pytest

from scripts import device_token
from web.settings import WebSettings


@pytest.fixture
def paths(tmp_path):
    data = tmp_path / "data"
    WebSettings(str(data / "webui_settings.json")).save()
    return data


@pytest.mark.parametrize("command", ["cmd_lockout", "cmd_gen_code"])
def test_atomic_cli_failure_preserves_previous_file(paths, monkeypatch, command):
    path = (paths / "device_auth_freeze.json") if command == "cmd_lockout" else (paths / "device_enroll_pending.json")
    value = {"created": time.time(), "reason": "synthetic previous state"}
    path.write_text(json.dumps(value))

    def failed_replace(*args):
        raise OSError("synthetic full disk")

    monkeypatch.setattr("web.auth_files.os.replace", failed_replace)
    with pytest.raises(OSError):
        getattr(device_token, command)(SimpleNamespace(reason="synthetic", name="synthetic"))
    assert json.loads(path.read_text()) == value
    assert not list(paths.glob(f".{path.name}.*"))


def test_malformed_freeze_is_reported_as_blocked(paths, capsys):
    (paths / "device_auth_freeze.json").write_text("{")
    assert device_token.cmd_status(SimpleNamespace()) == 0
    assert "访问被拒绝" in capsys.readouterr().out


def test_corrupt_settings_status_does_not_report_auth_disabled(paths, capsys):
    (paths / "webui_settings.json").write_text("{")
    assert device_token.cmd_status(SimpleNamespace()) == 2
    assert "已损坏" in capsys.readouterr().err


def test_lockout_preserves_existing_gate_and_blocks_old_devices(paths, capsys):
    settings = WebSettings(str((paths / "webui_settings.json")))
    token = settings.create_device("synthetic old device")
    assert device_token.cmd_lockout(SimpleNamespace(reason="synthetic")) == 0
    assert not settings.device_auth
    assert settings.requires_device
    assert settings.valid_device(token) is None
    assert "已写入紧急冻结" in capsys.readouterr().out
    assert device_token.cmd_unlock(SimpleNamespace()) == 0
    assert settings.valid_device(token) is not None


def test_host_open_and_close_are_seen_by_running_settings(paths, capsys):
    settings = WebSettings(str((paths / "webui_settings.json")))
    token = settings.create_device("synthetic old device")
    assert device_token.cmd_device_policy(SimpleNamespace(command="deny-new")) == 0
    assert not settings.allow_new_devices
    assert settings.valid_device(token) is not None
    assert device_token.cmd_status(SimpleNamespace()) == 0
    assert "新设备入口: 关闭" in capsys.readouterr().out
    assert device_token.cmd_device_policy(SimpleNamespace(command="allow-new")) == 0
    assert settings.allow_new_devices


def test_host_open_does_not_release_freeze(paths, capsys):
    settings = WebSettings(str((paths / "webui_settings.json")))
    old_token = settings.create_device("synthetic old device")
    device_token.cmd_lockout(SimpleNamespace(reason="synthetic"))
    device_token.cmd_device_policy(SimpleNamespace(command="deny-new"))
    device_token.cmd_device_policy(SimpleNamespace(command="allow-new"))
    assert settings.allow_new_devices
    assert settings.requires_device
    assert settings.valid_device(old_token) is None


def test_failed_host_reopen_keeps_policy_closed(paths, monkeypatch):
    device_token.cmd_device_policy(SimpleNamespace(command="deny-new"))

    def failed_replace(*args):
        raise OSError("synthetic full disk")

    monkeypatch.setattr("web.auth_files.os.replace", failed_replace)
    with pytest.raises(OSError):
        device_token.cmd_device_policy(SimpleNamespace(command="allow-new"))
    assert not WebSettings(str((paths / "webui_settings.json"))).allow_new_devices


def test_tentacle_sh_exposes_device_cli_and_deploy_whitelist():
    from pathlib import Path
    from scripts import deploy, release_package

    for rel in ("scripts/device_token.py", "scripts/tentacle.sh"):
        assert deploy.managed(rel), f"not deploy.managed: {rel}"
        assert release_package.runtime_name(rel), f"not release_package.runtime_name: {rel}"
        assert release_package.package_name(rel), f"not release_package.package_name: {rel}"

    script_text = (Path(__file__).resolve().parent.parent / "scripts" / "tentacle.sh").read_text(encoding="utf-8")
    assert "tentacle device <gen-code|status|lockout|unlock|deny-new|allow-new>" in script_text
    assert "    device)\n" in script_text
    assert 'sudo -u "$app_user" env "TENTACLE_HOME=$app_home"' in script_text
