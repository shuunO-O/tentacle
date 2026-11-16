"""内置 harmony 插件：设备授权、闹钟队列与回执、HTTP 聊天保活、实况窗推送与凭据安全测试。"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from agent.commands import CommandRegistry
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from runtime.tool_registry import ToolRegistry
from web.server import create_app
from web.settings import WebSettings


def _load_harmony_submodule(name: str):
    root = Path(__file__).resolve().parents[1] / "plugins" / "bundled" / "harmony"
    spec = importlib.util.spec_from_file_location(f"test_harmony_{name}", root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


phone_service_mod = _load_harmony_submodule("phone_service")
push_mod = _load_harmony_submodule("push")

PhoneService = phone_service_mod.PhoneService
current_phone = phone_service_mod.current_phone
CREDENTIAL_FIELDS = push_mod.CREDENTIAL_FIELDS
DEFAULT_AUDIENCE = push_mod.DEFAULT_AUDIENCE
HarmonyPush = push_mod.HarmonyPush


async def _start_harmony_plugin(tmp_path: Path, *, cfg: dict | None = None):
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    bundled = Path(__file__).resolve().parents[1] / "plugins" / "bundled"
    pm = PluginManager(
        plugins_dir=tmp_path / "user-plugins",
        bundled_dir=bundled,
        data_dir=tmp_path / "plugin-data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    await pm.startup({
        "plugins": {
            "items": {
                "harmony": {
                    "enabled": True,
                    "trusted": True,
                    "config": cfg or {"action_wait_seconds": 12, "allow_alarms": True, "allow_live_view": True},
                }
            },
            "mcp": {},
        }
    })
    service = pm._active_runtimes["harmony"]["ctx"].harmony_service
    return pm, reg, service


def test_chat_defaults_configure_harmony_bundled_plugin():
    from config.files import parse_config

    defaults = parse_config((Path(__file__).resolve().parents[1] / "config.defaults.yaml").read_text(encoding="utf-8"))
    enabled = defaults["roles"]["chat"]["tools"]["enabled"]
    assert "phone_alarm" not in enabled
    assert "schedule_list" in enabled
    harmony_cfg = defaults["plugins"]["items"]["harmony"]
    assert harmony_cfg["enabled"] is False
    assert harmony_cfg["trusted"] is True


@pytest.fixture(autouse=True)
def isolated_push_environment(monkeypatch):
    for name in [*CREDENTIAL_FIELDS.values(), "HUAWEI_PUSH_CREDENTIALS_FILE", "HUAWEI_PUSH_TEST_MESSAGE"]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def service(tmp_path):
    push = SimpleNamespace(configured=True, finish=AsyncMock())
    svc = PhoneService(tmp_path / "phone.db", AsyncMock(), push)
    svc.register("synthetic-phone-01", "s" * 64, "owner", "synthetic-token", "Asia/Shanghai", True, True, lambda: True)
    return svc


def alarm_args():
    return {
        "operation": "create",
        "title": "synthetic reminder",
        "at": datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat(),
    }


async def test_queued_is_not_success_and_survives_restart(service):
    result = await service.alarm("synthetic-phone-01", alarm_args(), wait_seconds=0)
    assert result["status"] == "pending"
    restored = PhoneService(service.path)
    restored.register("synthetic-phone-01", "s" * 64, "owner", "", "Asia/Shanghai", True, False, lambda: True)
    action = restored.state("synthetic-phone-01")["actions"][0]
    assert action["id"] == result["action_id"]
    restored.acknowledge("synthetic-phone-01", action["id"], "executed", "17")
    with pytest.raises(ValueError):
        restored.acknowledge("synthetic-phone-01", action["id"], "failed", "changed")
    assert restored.state("synthetic-phone-01")["actions"] == []


@pytest.mark.parametrize("expired_count", [0, 1, 32])
async def test_alarm_queue_capacity_excludes_expired_actions(service, expired_count):
    now = time.time()
    with service.db() as db:
        db.executemany(
            "INSERT INTO phone_actions VALUES (?,?,?,'pending','',?)",
            [(f"synthetic-{i}", "synthetic-phone-01", "{}", now - 1 if i < expired_count else now + 600) for i in range(32)],
        )
    if not expired_count:
        with pytest.raises(ValueError, match="待执行闹钟过多"):
            await service.alarm("synthetic-phone-01", alarm_args(), wait_seconds=0)
        return

    result = await service.alarm("synthetic-phone-01", alarm_args(), wait_seconds=0)
    assert result["status"] == "pending"
    with service.db() as db:
        counts = dict(db.execute("SELECT status, COUNT(*) FROM phone_actions GROUP BY status").fetchall())
    assert counts == {"expired": expired_count, "pending": 33 - expired_count}


async def test_device_receipt_unblocks_tool_and_other_devices_cannot_ack(service):
    task = asyncio.create_task(service.alarm("synthetic-phone-01", alarm_args()))
    await asyncio.sleep(0)
    action = service.state("synthetic-phone-01")["actions"][0]
    service.register("synthetic-phone-02", "x" * 64, "other", "", "Asia/Shanghai", True, False, lambda: True)
    with pytest.raises(LookupError):
        service.acknowledge("synthetic-phone-02", action["id"], "executed", "1")
    service.acknowledge("synthetic-phone-01", action["id"], "executed", "42")
    assert await task == {"action_id": action["id"], "status": "executed", "result": "42"}


@pytest.mark.parametrize("at", ["2026-01-01T09:00:00", "invalid", "2020-01-01T09:00:00+08:00"])
async def test_invalid_alarm_time_never_enqueues(service, at):
    with pytest.raises(ValueError):
        await service.alarm("synthetic-phone-01", {**alarm_args(), "at": at}, wait_seconds=0)
    assert not service.state("synthetic-phone-01")["actions"]
    assert not service.state("synthetic-phone-01")["rounds"]


async def test_account_change_cancels_pending_actions_and_old_push(service):
    await service.alarm("synthetic-phone-01", alarm_args(), wait_seconds=0)
    service.begin_round("synthetic-phone-01", "synthetic-round", 7, "PROGRESS")
    service.finish_round("synthetic-round", "completed")
    service.register("synthetic-phone-01", "s" * 64, "new-owner", "new-token", "Asia/Shanghai", True, True, lambda: True)
    assert not service.state("synthetic-phone-01")["actions"]
    await service.push_round("synthetic-round")
    service.push.finish.assert_not_awaited()
    with pytest.raises(PermissionError):
        service.authorize("synthetic-phone-01", "s" * 64, "owner")
    with pytest.raises(PermissionError):
        await service.alarm("synthetic-phone-01", alarm_args(), expected_owner="owner", wait_seconds=0)


async def test_revoked_device_never_receives_push_or_tool_action(service):
    service.begin_round("synthetic-phone-01", "synthetic-round", 7, "PROGRESS")
    service.finish_round("synthetic-round", "completed")
    service.guards["synthetic-phone-01"] = lambda: False
    await service.push_round("synthetic-round")
    service.push.finish.assert_not_awaited()
    with pytest.raises(PermissionError):
        await service.alarm("synthetic-phone-01", alarm_args(), wait_seconds=0)


async def test_live_completion_is_independent_of_websocket_and_push_failure(service):
    service.begin_round("synthetic-phone-01", "synthetic-round", 8, "PROGRESS")
    service.finish_round("synthetic-round", "failed")
    service.push.finish.side_effect = RuntimeError("synthetic platform error")
    await service.push_round("synthetic-round")
    row = service.state("synthetic-phone-01")["rounds"][0]
    assert (row["status"], row["push_status"]) == ("failed", "failed")
    body = HarmonyPush().body(service.push.finish.call_args.args[0])
    assert body["payload"]["operation"] == 2
    assert body["payload"]["activityData"]["notificationData"]["contentTitle"] != "回复完成"
    assert body["target"]["token"] == ["synthetic-token"]


async def test_completed_push_only_contains_generic_status_and_is_not_resent(service):
    service.begin_round("synthetic-phone-01", "synthetic-round", 9, "PROGRESS")
    service.finish_round("synthetic-round", "completed")
    await service.push_round("synthetic-round")
    await service.push_round("synthetic-round")
    assert service.push.finish.await_count == 1
    body = HarmonyPush().body(service.push.finish.call_args.args[0])
    assert body["payload"]["activityData"]["notificationData"]["contentTitle"] == "回复完成"
    assert body["pushOptions"]["testMessage"] is True


def test_server_restart_marks_unfinished_round_unknown(service):
    service.begin_round("synthetic-phone-01", "synthetic-round", 10, "PROGRESS")
    restored = PhoneService(service.path)
    restored.register("synthetic-phone-01", "s" * 64, "owner", "", "Asia/Shanghai", True, False, lambda: True)
    assert restored.state("synthetic-phone-01")["rounds"][0]["status"] == "unknown"


async def test_plugin_tool_requires_request_scoped_device_and_pending_is_explicit(tmp_path, service):
    pm, reg, _ = await _start_harmony_plugin(tmp_path, cfg={"action_wait_seconds": 0.05, "allow_alarms": True})
    try:
        assert reg.has("p_harmony__phone_alarm")
        assert json.loads(await reg.execute("p_harmony__phone_alarm", alarm_args()))["status"] == "error"

        ctx = pm._active_runtimes["harmony"]["ctx"]
        # 通过 PluginContext 隔离模块中的 current_phone 注入会话设备
        plugin_phone_mod = next(
            m for name, m in __import__("sys").modules.items()
            if name.startswith("tentacle_plugins.harmony") and hasattr(m, "current_phone")
        )
        service.alarm = AsyncMock(return_value={"status": "pending", "action_id": "synthetic"})
        marker = plugin_phone_mod.current_phone.set((service, "synthetic-phone-01", "owner"))
        try:
            result = json.loads(await reg.execute("p_harmony__phone_alarm", alarm_args()))
            assert result["status"] == "pending" and "尚未设置成功" in result["message"]
        finally:
            plugin_phone_mod.current_phone.reset(marker)
        assert ctx.harmony_service is not None
    finally:
        await pm.shutdown()


async def test_http_device_secret_and_auth_are_both_required(tmp_path):
    pm, _, _ = await _start_harmony_plugin(tmp_path)
    try:
        settings = WebSettings(str(tmp_path / "settings.json"))
        settings.set_password("pw")
        token = settings.issue_token()
        app = create_app(
            SimpleNamespace(broadcast=AsyncMock()),
            None,
            None,
            None,
            None,
            SimpleNamespace(db_path=tmp_path / "chronicle.db"),
            settings,
            None,
            plugin_manager=pm,
        )
        reg = {"device_id": "synthetic-phone-http", "secret": "a" * 64, "timezone": "Asia/Shanghai", "alarms": True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://synthetic.invalid") as client:
            assert (await client.post("/api/plugins/ext/harmony/register", json=reg)).status_code == 401
            client.headers["Authorization"] = f"Bearer {token}"
            assert (await client.post("/api/plugins/ext/harmony/register", json=reg)).status_code == 200
            assert (await client.get("/api/plugins/ext/harmony/state")).status_code == 401
            client.headers.update({"X-Tentacle-Device": reg["device_id"], "X-Tentacle-Device-Secret": "b" * 64})
            assert (await client.get("/api/plugins/ext/harmony/state")).status_code == 403
            client.headers["X-Tentacle-Device-Secret"] = reg["secret"]
            assert (await client.get("/api/plugins/ext/harmony/state")).json() == {"actions": [], "rounds": []}
            assert (await client.delete("/api/plugins/ext/harmony/register")).status_code == 200
    finally:
        await pm.shutdown()


async def test_phone_round_continues_after_http_cancellation(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    pm, _, svc = await _start_harmony_plugin(tmp_path)
    try:
        plugin_phone_mod = next(
            m for name, m in __import__("sys").modules.items()
            if name.startswith("tentacle_plugins.harmony") and hasattr(m, "current_phone")
        )

        async def run_round(*args, **kwargs):
            seen.append(plugin_phone_mod.current_phone.get()[1])
            entered.set()
            await release.wait()
            return SimpleNamespace(round_status={"status": "completed"}, stop_reason="complete", total_usage={})

        settings = WebSettings(str(tmp_path / "settings.json"))
        app = create_app(
            SimpleNamespace(broadcast=AsyncMock()),
            None,
            SimpleNamespace(run_round=run_round),
            None,
            None,
            SimpleNamespace(db_path=tmp_path / "chronicle.db"),
            settings,
            None,
            plugin_manager=pm,
        )
        ctx_web = pm._active_runtimes["harmony"]["ctx"].web
        request = Request({"type": "http", "headers": []})
        svc.register(
            "synthetic-phone-01",
            "s" * 64,
            ctx_web.session_owner(request),
            "",
            "Asia/Shanghai",
            True,
            True,
            lambda: True,
        )
        headers = {"X-Tentacle-Device": "synthetic-phone-01", "X-Tentacle-Device-Secret": "s" * 64}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://synthetic.invalid") as client:
            task = asyncio.create_task(
                client.post(
                    "/api/chat/send",
                    headers=headers,
                    json={
                        "text": "synthetic input",
                        "platform": "harmony",
                        "request_id": "synthetic-request",
                        "client_meta": {"live_view": {"activity_id": 11, "event": "PROGRESS"}},
                    },
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            for _ in range(20):
                if not app.state.client_tasks:
                    break
                await asyncio.sleep(0.01)
        assert seen == ["synthetic-phone-01"]
        assert svc.state("synthetic-phone-01")["rounds"][0]["status"] == "completed"
    finally:
        await pm.shutdown()


def test_push_jwt_uses_ps256_with_synthetic_key_only(monkeypatch):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    monkeypatch.setenv("HUAWEI_PUSH_PRIVATE_KEY", pem)
    monkeypatch.setenv("HUAWEI_PUSH_PROJECT_ID", "synthetic-project")
    monkeypatch.setenv("HUAWEI_PUSH_KEY_ID", "synthetic-key")
    monkeypatch.setenv("HUAWEI_PUSH_SUB_ACCOUNT", "synthetic-account")
    monkeypatch.setenv("HUAWEI_PUSH_AUDIENCE", "https://synthetic.invalid")
    token = HarmonyPush().jwt()
    header, payload, signature = token.split(".")

    def decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    assert json.loads(decode(header))["alg"] == "PS256"
    data = json.loads(decode(payload))
    assert data["exp"] - data["iat"] == 3600
    key.public_key().verify(
        decode(signature),
        (header + "." + payload).encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )


@pytest.mark.parametrize("code", ["80000000", "synthetic-denied"])
async def test_push_transport_uses_v3_and_checks_platform_acceptance(monkeypatch, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"code": code})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handler))
    )
    monkeypatch.setenv("HUAWEI_PUSH_PROJECT_ID", "synthetic-project")
    monkeypatch.setattr(HarmonyPush, "jwt", lambda self: "synthetic-jwt")
    row = {"activity": 17, "status": "completed", "event": "PROGRESS", "token": "synthetic-token"}
    if code == "80000000":
        await HarmonyPush().finish(row)
    else:
        with pytest.raises(RuntimeError, match="未被平台接受"):
            await HarmonyPush().finish(row)
    assert str(requests[0].url) == "https://push-api.cloud.huawei.com/v3/synthetic-project/messages:send"
    assert requests[0].headers["push-type"] == "7"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-jwt"
    assert json.loads(requests[0].content)["target"]["token"] == ["synthetic-token"]


async def test_ordinary_test_push_is_separate_from_live_view(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"code": "80000000"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handler))
    )
    monkeypatch.setenv("HUAWEI_PUSH_PROJECT_ID", "synthetic-project")
    monkeypatch.setenv("HUAWEI_PUSH_TEST_MESSAGE", "false")
    monkeypatch.setattr(HarmonyPush, "jwt", lambda self: "synthetic-jwt")
    await HarmonyPush().test_notification("synthetic-token")
    request = requests[0]
    body = json.loads(request.content)
    assert request.headers["push-type"] == "0"
    assert body["pushOptions"] == {"testMessage": True, "ttl": 60}
    assert body["payload"]["notification"]["category"] == "MARKETING"
    assert body["payload"]["notification"]["title"] == "Tentacle 推送测试"
    assert body["payload"]["notification"]["clickAction"] == {"actionType": 0}
    assert "activityId" not in body["payload"]
    with pytest.raises(ValueError, match="Push Token"):
        await HarmonyPush().test_notification("")
    assert len(requests) == 1


async def test_push_error_excludes_remote_private_text(monkeypatch):
    def handler(request):
        return httpx.Response(403, json={"code": "80300001", "msg": "synthetic-secret-response"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handler))
    )
    monkeypatch.setenv("HUAWEI_PUSH_PROJECT_ID", "synthetic-project")
    monkeypatch.setattr(HarmonyPush, "jwt", lambda self: "synthetic-jwt")
    with pytest.raises(RuntimeError) as error:
        await HarmonyPush().test_notification("synthetic-token")
    assert "80300001" in str(error.value)
    assert "synthetic-secret-response" not in str(error.value)


@pytest.fixture(scope="module")
def synthetic_push_material():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key, pem


def synthetic_credentials(pem):
    return {
        "project_id": "synthetic-file-project",
        "key_id": "synthetic-file-key",
        "sub_account": "synthetic-file-account",
        "private_key": pem,
        "token_uri": DEFAULT_AUDIENCE,
    }


@pytest.mark.parametrize("audience", [None, "", "https://synthetic.invalid"])
def test_environment_credentials_default_audience(monkeypatch, synthetic_push_material, audience):
    _, pem = synthetic_push_material
    for field, name in CREDENTIAL_FIELDS.items():
        if field != "token_uri":
            monkeypatch.setenv(name, synthetic_credentials(pem)[field])
    if audience is not None:
        monkeypatch.setenv("HUAWEI_PUSH_AUDIENCE", audience)
    push = HarmonyPush()
    assert push.configured
    payload = push.jwt().split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["aud"] == (audience or DEFAULT_AUDIENCE)
    monkeypatch.delenv("HUAWEI_PUSH_PRIVATE_KEY")
    assert not HarmonyPush().configured


@pytest.mark.parametrize("kind", ["notification", "live_view"])
async def test_file_credentials_sign_and_send_without_mixing_environment(
    monkeypatch, tmp_path, synthetic_push_material, kind
):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key, pem = synthetic_push_material
    data = synthetic_credentials(pem.replace("\n", "\\n"))
    data["unused_metadata"] = "synthetic-unused"
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps(data), encoding="utf-8-sig")
    monkeypatch.setenv("HUAWEI_PUSH_CREDENTIALS_FILE", str(path))
    for name in CREDENTIAL_FIELDS.values():
        monkeypatch.setenv(name, "synthetic-environment-value-must-not-be-used")
    seen = []

    def decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def handler(request):
        seen.append(request)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        header, payload, signature = token.split(".")
        assert json.loads(decode(header))["kid"] == "synthetic-file-key"
        claims = json.loads(decode(payload))
        assert claims["iss"] == "synthetic-file-account"
        assert claims["aud"] == DEFAULT_AUDIENCE
        assert claims["exp"] - claims["iat"] == 3600
        key.public_key().verify(
            decode(signature),
            (header + "." + payload).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
        return httpx.Response(200, json={"code": "80000000"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handler))
    )
    push = HarmonyPush()
    assert push.configured
    if kind == "notification":
        await push.test_notification("synthetic-token")
    else:
        await push.finish({"activity": 17, "status": "completed", "event": "PROGRESS", "token": "synthetic-token"})
    assert str(seen[0].url) == "https://push-api.cloud.huawei.com/v3/synthetic-file-project/messages:send"
    assert seen[0].headers["push-type"] == ("0" if kind == "notification" else "7")
    assert os.environ["HUAWEI_PUSH_PRIVATE_KEY"] == "synthetic-environment-value-must-not-be-used"


@pytest.mark.parametrize("invalid", ["missing", "json", "object", "empty_fields", "private_key", "audience", "project"])
async def test_invalid_file_disables_push_without_falling_back_or_leaking_values(
    monkeypatch, tmp_path, synthetic_push_material, caplog, invalid
):
    _, pem = synthetic_push_material
    data = synthetic_credentials(pem)
    for field, name in CREDENTIAL_FIELDS.items():
        monkeypatch.setenv(name, data[field])
    path = tmp_path / "synthetic-sensitive-filename.json"
    monkeypatch.setenv("HUAWEI_PUSH_CREDENTIALS_FILE", str(path))
    if invalid == "json":
        path.write_text('{"private_key":"synthetic-sensitive-value"', encoding="utf-8")
    elif invalid == "object":
        path.write_text('["synthetic-sensitive-value"]', encoding="utf-8")
    elif invalid != "missing":
        if invalid == "empty_fields":
            data.update(key_id="", private_key="")
        elif invalid == "private_key":
            data["private_key"] = "synthetic-sensitive-value"
        elif invalid == "audience":
            data["token_uri"] = "https://synthetic-sensitive-value.invalid"
        elif invalid == "project":
            data["project_id"] = "synthetic-sensitive-value/invalid"
        path.write_text(json.dumps(data), encoding="utf-8")
    push = HarmonyPush()
    assert not push.configured
    with pytest.raises(ValueError) as error:
        await push.test_notification("synthetic-token")
    with pytest.raises(ValueError):
        push.jwt()
    for value in ["synthetic-sensitive-value", str(path), pem]:
        assert value not in str(error.value)
        assert value not in caplog.text


def test_file_credentials_are_loaded_once_until_backend_restart(monkeypatch, tmp_path, synthetic_push_material):
    _, pem = synthetic_push_material
    path = tmp_path / "service-account.json"
    data = synthetic_credentials(pem)
    del data["token_uri"]
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HUAWEI_PUSH_CREDENTIALS_FILE", str(path))
    push = HarmonyPush()
    assert push.configured
    path.write_text("not-json", encoding="utf-8")
    assert push.jwt()
    assert push.configured
    assert not HarmonyPush().configured


def test_relative_credentials_path_uses_tentacle_home(monkeypatch, tmp_path, synthetic_push_material):
    _, pem = synthetic_push_material
    root = tmp_path / "synthetic-project"
    path = root / "keys" / "push.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(synthetic_credentials(pem)), encoding="utf-8")
    monkeypatch.setenv("TENTACLE_HOME", str(root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HUAWEI_PUSH_CREDENTIALS_FILE", "keys/push.json")
    assert HarmonyPush().configured


async def test_backend_registration_reports_file_readiness_without_exposing_credentials(
    monkeypatch, tmp_path, synthetic_push_material
):
    _, pem = synthetic_push_material
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps(synthetic_credentials(pem)), encoding="utf-8")
    monkeypatch.setenv("HUAWEI_PUSH_CREDENTIALS_FILE", str(path))
    body = {"device_id": "synthetic-file-phone", "secret": "s" * 64, "alarms": True}
    for ready in [True, False]:
        if not ready:
            path.write_text("{}", encoding="utf-8")
        sub_tmp = tmp_path / f"run-{ready}"
        sub_tmp.mkdir()
        pm, _, _ = await _start_harmony_plugin(sub_tmp)
        try:
            settings = WebSettings(str(sub_tmp / "synthetic-settings.json"))
            app = create_app(
                None,
                None,
                None,
                None,
                None,
                SimpleNamespace(db_path=sub_tmp / "synthetic-chronicle.db"),
                settings,
                None,
                plugin_manager=pm,
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://synthetic.invalid") as client:
                response = await client.post("/api/plugins/ext/harmony/register", json=body)
            assert response.status_code == 200
            assert response.json() == {"registered": True, "push_configured": ready}
        finally:
            await pm.shutdown()


@pytest.mark.parametrize("relative", [True, False])
def test_push_credentials_cannot_be_read_as_an_outgoing_attachment(monkeypatch, tmp_path, relative):
    import attachments.outgoing as outgoing

    path = tmp_path / "ordinary-name.json"
    path.write_text('{"synthetic":"not-for-download"}', encoding="utf-8")
    monkeypatch.setenv("HUAWEI_PUSH_CREDENTIALS_FILE", path.name if relative else str(path))
    other = tmp_path / "unrelated-working-directory"
    other.mkdir()
    monkeypatch.chdir(other)
    for requested in [path.name, str(path)]:
        with pytest.raises(PermissionError, match="认证材料"):
            outgoing.open_outgoing_file(requested)
