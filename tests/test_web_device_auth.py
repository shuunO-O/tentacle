"""
WebSettings 设备令牌层测试（纯逻辑层，不起服务、不碰生产 data/）

覆盖：绑定往返、错码/过期码/一码多用、吊销即失效、
freeze 带外总闸（冻结前设备失效、冻结后新设备不受影响、解冻恢复）、
pending 文件用后焚毁、last_seen 落盘节流。
"""

import hashlib
import json
import time

import pytest

from web.settings import SettingsWriteError, WebSettings


def make_settings(tmp_path) -> WebSettings:
    return WebSettings(str(tmp_path / "webui_settings.json"))


def write_pending(tmp_path, code: str, name: str = "测试设备", ttl: float = 900):
    """模拟主机脚本 gen-code 写入的一次性绑定码文件"""
    path = tmp_path / "device_enroll_pending.json"
    path.write_text(
        json.dumps(
            {
                "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                "expires": time.time() + ttl,
                "name": name,
            }
        ),
        encoding="utf-8",
    )
    return path


def write_freeze(tmp_path, created: float, reason: str = ""):
    """模拟主机脚本 lockout 写入的冻结文件"""
    path = tmp_path / "device_auth_freeze.json"
    path.write_text(
        json.dumps({"created": created, "reason": reason}), encoding="utf-8"
    )
    return path


class TestEnroll:
    def test_enroll_roundtrip(self, tmp_path):
        """绑定码换 token，token 通过 valid_device 校验"""
        s = make_settings(tmp_path)
        write_pending(tmp_path, "abcd1234abcd1234", name="手机")
        token = s.enroll_device("abcd1234abcd1234")
        assert token is not None
        dev = s.valid_device(token)
        assert dev is not None and dev["name"] == "手机"

    def test_enroll_wrong_code_keeps_pending(self, tmp_path):
        """错码拒绝且不焚毁 pending 文件（正确码随后仍可用）"""
        s = make_settings(tmp_path)
        write_pending(tmp_path, "right-code")
        assert s.enroll_device("wrong-code") is None
        assert s.pending_code_info() is not None  # 文件还在
        assert s.enroll_device("right-code") is not None  # 正确码仍有效

    def test_enroll_expired_code_burned(self, tmp_path):
        """过期码拒绝且焚毁文件（避免残留歧义）"""
        s = make_settings(tmp_path)
        write_pending(tmp_path, "old-code", ttl=-1)
        assert s.enroll_device("old-code") is None
        assert s.pending_code_info() is None

    def test_enroll_code_single_use(self, tmp_path):
        """一码一设备：第二次使用失败"""
        s = make_settings(tmp_path)
        write_pending(tmp_path, "once-only")
        assert s.enroll_device("once-only") is not None
        assert s.enroll_device("once-only") is None  # 已焚毁

    def test_enroll_no_pending_file(self, tmp_path):
        s = make_settings(tmp_path)
        assert s.enroll_device("whatever") is None


class TestDeviceLifecycle:
    def test_revoke_invalidates(self, tmp_path):
        s = make_settings(tmp_path)
        token = s.create_device("笔记本")
        assert s.valid_device(token) is not None
        dev_id = hashlib.sha256(token.encode()).hexdigest()
        assert s.revoke_device(dev_id) is True
        assert s.valid_device(token) is None
        assert s.revoke_device(dev_id) is False  # 再吊销报不存在

    def test_valid_device_none_or_garbage(self, tmp_path):
        s = make_settings(tmp_path)
        assert s.valid_device(None) is None
        assert s.valid_device("") is None
        assert s.valid_device("garbage-token") is None

    def test_list_and_set_device_auth(self, tmp_path):
        s = make_settings(tmp_path)
        s.create_device("A")
        s.create_device("B")
        names = [d["name"] for d in s.list_devices()]
        assert names == ["B", "A"]  # 绑定时间倒序
        assert s.device_auth is False
        s.set_device_auth(True)
        assert s.device_auth is True
        # 持久化：新实例读到同样状态
        s2 = make_settings(tmp_path)
        assert s2.device_auth is True
        assert len(s2.list_devices()) == 2

    def test_last_seen_throttled(self, tmp_path):
        """节流窗口内重复校验不更新 last_seen（避免高频写盘）"""
        s = make_settings(tmp_path)
        token = s.create_device("节流")
        first = s.valid_device(token)["last_seen"]
        again = s.valid_device(token)["last_seen"]
        assert again == first


class TestFreeze:
    def test_freeze_kills_old_devices(self, tmp_path):
        """冻结时刻之前绑定的设备全部失效"""
        s = make_settings(tmp_path)
        old_token = s.create_device("旧设备")
        freeze_at = time.time()
        write_freeze(tmp_path, freeze_at)
        assert s.valid_device(old_token) is None

    def test_freeze_spares_new_devices(self, tmp_path):
        """冻结后凭新绑定码登记的设备不受影响（主人不被锁死）"""
        s = make_settings(tmp_path)
        write_freeze(tmp_path, time.time())
        write_pending(tmp_path, "fresh-code")
        token = s.enroll_device("fresh-code")
        assert token is not None
        assert s.valid_device(token) is not None

    def test_unfreeze_restores(self, tmp_path):
        """解除冻结后原设备 cookie 恢复有效"""
        s = make_settings(tmp_path)
        token = s.create_device("临时下线")
        path = write_freeze(tmp_path, time.time())
        assert s.valid_device(token) is None
        path.unlink()
        assert s.valid_device(token) is not None

    def test_freeze_info(self, tmp_path):
        s = make_settings(tmp_path)
        assert s.freeze_info() is None
        write_freeze(tmp_path, 123.0, reason="测试")
        assert s.freeze_info() == {"created": 123.0, "reason": "测试"}

    @pytest.mark.parametrize("value", ["{", "[]", "{}", '{"created": null}',
                                     '{"created": "NaN"}', '{"created": NaN}', '{"created": -1}'])
    def test_malformed_freeze_rejects_devices(self, tmp_path, value):
        s = make_settings(tmp_path)
        token = s.create_device("旧设备")
        (tmp_path / "device_auth_freeze.json").write_text(value)
        assert s.freeze_info() is not None
        assert s.requires_device
        assert s.valid_device(token) is None

    def test_unreadable_freeze_rejects_devices(self, tmp_path, monkeypatch):
        s = make_settings(tmp_path)
        token = s.create_device("旧设备")
        original = type(s.path).read_text

        def read(path, *args, **kwargs):
            if path.name == "device_auth_freeze.json":
                raise PermissionError("synthetic permission failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(s.path), "read_text", read)
        assert s.requires_device
        assert s.valid_device(token) is None

    @pytest.mark.parametrize("enabled", [False, True])
    def test_freeze_blocks_gate_changes(self, tmp_path, enabled):
        s = make_settings(tmp_path)
        write_freeze(tmp_path, time.time())
        with pytest.raises(ValueError, match="冻结"):
            s.set_device_auth(enabled)
        assert not s.device_auth


class TestAuthPersistence:
    @pytest.mark.parametrize("value", ["{", "[]", '{"device_auth": null}',
                                     '{"device_auth": "false"}', '{"device_tokens": []}',
                                     '{"password_hash": "", "password_salt": "salt"}',
                                     '{"password_hash": "bad", "password_salt": "salt"}',
                                     '{"device_tokens": {"bad-id": {}}}'])
    def test_existing_corrupt_settings_never_disable_auth(self, tmp_path, value):
        path = tmp_path / "webui_settings.json"
        path.write_text(value)
        with pytest.raises(RuntimeError, match="拒绝"):
            make_settings(tmp_path)
        assert path.read_text() == value

    def test_unreadable_settings_never_disable_auth(self, tmp_path, monkeypatch):
        def unreadable(*args, **kwargs):
            raise PermissionError("synthetic permission failure")

        monkeypatch.setattr(type(tmp_path), "read_text", unreadable)
        with pytest.raises(RuntimeError, match="拒绝"):
            make_settings(tmp_path)

    def test_default_device_tables_are_independent(self, tmp_path):
        first = WebSettings(str(tmp_path / "one.json"))
        second = WebSettings(str(tmp_path / "two.json"))
        first.create_device("仅第一实例")
        assert len(first.list_devices()) == 1
        assert not second.list_devices()

    @pytest.mark.parametrize("operation", ["revoke", "password", "gate", "enroll"])
    def test_failed_write_preserves_disk_and_memory(self, tmp_path, monkeypatch, operation):
        s = make_settings(tmp_path)
        token = s.create_device("现有设备")
        s.set_password("old-password")
        password_token = s.issue_token()
        before = s.path.read_bytes()
        devices = s.list_devices()
        revision = s.auth_revision

        def failed_replace(*args):
            raise OSError("synthetic full disk")

        monkeypatch.setattr("web.auth_files.os.replace", failed_replace)
        with pytest.raises(SettingsWriteError):
            if operation == "revoke":
                s.revoke_device(hashlib.sha256(token.encode()).hexdigest())
            elif operation == "password":
                s.set_password("new-password")
            elif operation == "gate":
                s.set_device_auth(True)
            else:
                write_pending(tmp_path, "fresh-code")
                s.enroll_device("fresh-code")
        assert s.path.read_bytes() == before
        assert s.list_devices() == devices
        assert s.verify("old-password")
        assert s.valid_token(password_token)
        assert s.auth_revision == revision
        assert not s.device_auth
        assert not list(tmp_path.glob(".webui_settings.json.*"))

    def test_failed_last_seen_write_rejects_device(self, tmp_path, monkeypatch):
        s = make_settings(tmp_path)
        token = s.create_device("旧设备")
        first_seen = s.list_devices()[0]["last_seen"]
        monkeypatch.setattr("web.settings.time.time", lambda: first_seen + 100)

        def failed_replace(*args):
            raise OSError("synthetic full disk")

        monkeypatch.setattr("web.auth_files.os.replace", failed_replace)
        assert s.valid_device(token) is None
        assert s.list_devices()[0]["last_seen"] == first_seen


class TestNewDevicesPolicy:
    def test_close_survives_restart_and_host_reopen_is_live(self, tmp_path):
        s = make_settings(tmp_path)
        assert s.allow_new_devices
        token = s.create_device("旧设备")
        s.deny_new_devices()
        assert not s.allow_new_devices
        assert s.requires_device
        assert s.valid_device(token) is not None
        assert not make_settings(tmp_path).allow_new_devices
        (tmp_path / "device_access_policy.json").write_text(json.dumps({"allow_new_devices": True}))
        assert s.allow_new_devices

    def test_closed_policy_cannot_consume_binding_code(self, tmp_path):
        s = make_settings(tmp_path)
        write_pending(tmp_path, "synthetic-code")
        s.deny_new_devices()
        assert s.enroll_device("synthetic-code") is None
        assert s.pending_code_info() is not None
        assert not s.list_devices()

    @pytest.mark.parametrize("value", ["{", "[]", "{}", '{"allow_new_devices": null}',
                                     '{"allow_new_devices": "true"}'])
    def test_corrupt_policy_closes_entrance_but_keeps_old_device(self, tmp_path, value):
        s = make_settings(tmp_path)
        token = s.create_device("旧设备")
        (tmp_path / "device_access_policy.json").write_text(value)
        assert not s.allow_new_devices
        assert s.requires_device
        assert s.valid_device(token) is not None

    def test_policy_write_failure_preserves_open_state(self, tmp_path, monkeypatch):
        s = make_settings(tmp_path)

        def failed_replace(*args):
            raise OSError("synthetic full disk")

        monkeypatch.setattr("web.auth_files.os.replace", failed_replace)
        with pytest.raises(SettingsWriteError):
            s.deny_new_devices()
        assert s.allow_new_devices
        assert not list(tmp_path.glob(".device_access_policy.json.*"))
