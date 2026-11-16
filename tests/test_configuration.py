"""配置事务、鉴权与回合边界验收；仅使用临时文件和假客户端。"""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from config.store import ConfigurationConflict, ConfigurationError, ConfigStore, parse_config, patch_text
from config.service import ConfigurationService
from runtime.heartbeat import Heartbeat
from config.manager import ConfigManager
from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


SOURCE = """# synthetic configuration
config_version: 2
providers:
  primary:
    type: openai_compatible
    base_url: https://example.test/v1
    api_key_env: SYNTHETIC_KEY
  secondary:
    type: openai_compatible
    base_url: https://example.test/v1
    api_key_env: SYNTHETIC_KEY
models:
  sample:
    provider: primary
    name: sample
    defaults:
      temperature: 0.3  # keep comment
  new_model:
    provider: primary
    name: new-model
    defaults:
      temperature: 0.3
  worker_model:
    provider: secondary
    name: worker
roles:
  chat:
    model: sample
    tools:
      enabled:
        - get_time
  worker:
    model: worker_model
    tools: {enabled: []}
# heartbeat comment
heartbeat:
  interval_minutes: 60
webui:
  port: 8420
# custom extension retained verbatim
custom_extension: {untouched: true}
"""


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(SOURCE, encoding="utf-8")
    return ConfigStore(path)


def test_preview_is_readonly_and_write_preserves_comments_and_extra_fields(store):
    revision, _, _ = store.read()
    changes = {"roles.chat.overrides.temperature": 0.8, "heartbeat.interval_minutes": 20,
               "roles.chat.tools.enabled": ["get_time", "browser"], "providers.primary.proxy": ""}
    assert len(store.preview(changes, revision)["changes"]) == 4
    assert store.path.read_text() == SOURCE
    assert not store.backup_dir.exists()
    result = store.save(changes, revision)
    text = store.path.read_text()
    assert "# keep comment" in text and "# heartbeat comment" in text
    assert "custom_extension: {untouched: true}" in text
    assert (store.backup_dir / result["backup"]).read_text() == SOURCE
    assert yaml.safe_load(text)["roles"]["chat"]["tools"]["enabled"] == ["get_time", "browser"]
    assert yaml.safe_load(text)["providers"]["primary"]["proxy"] == ""
    with pytest.raises(ConfigurationConflict):
        store.save(changes, revision)


@pytest.mark.parametrize("changes", [
    {"heartbeat.interval_minutes": 0}, {"heartbeat.interval_minutes": float("nan")},
    {"roles.chat.overrides.stream": "false"}, {"roles.chat.max_iterations": True},
    {"roles.chat.tools.enabled": ["get_time", "get_time"]},
    {"providers.primary.api_key_env": "actual-secret-not-a-variable"},
    {"providers.primary.base_url": "https://secret@example.test"},
    {"providers.primary.base_url": "https://example.test?token=secret"},
    {"providers.primary.auth": "adc", "models.sample.api_style": "chat"},
    {"models.sample.api_style": "nonsense"}, {"roles.chat.selectable": False},
    {"../.env": "secret"}, {"webui.port": 65536},
])
def test_invalid_patch_never_writes(store, changes):
    with pytest.raises(ConfigurationError):
        store.save(changes, store.read()[0])
    assert store.path.read_text() == SOURCE
    assert not store.backup_dir.exists()


@pytest.mark.parametrize("text", ["models: {}\nmodels: {}", "models: &m {}\nother: *m", "models: [", "[]"])
def test_ambiguous_or_broken_yaml_is_rejected(text):
    with pytest.raises(ConfigurationError):
        parse_config(text)


@pytest.mark.parametrize("text,path,value", [
    ("models: {}\n# tail\n", "models.chat.temperature", 0.5),
    ("models:\n  chat:\n    tools:\n      enabled:\n        - a\n# tail\n", "models.chat.temperature", 0.5),
    ("models:\n  chat:\n    tools: {enabled: []}\n# tail\n", "models.chat.tools.enabled", ["b"]),
    ("models:\n  chat:\n    tools:\n      enabled:\n        - a\n    name: x\n", "models.chat.tools.enabled", []),
    ("models:\n  chat:\n    name: x", "heartbeat.interval_minutes", 30),
])
def test_yaml_node_patch_roundtrip(text, path, value):
    from config.store import set_value
    expected = yaml.safe_load(text)
    set_value(expected, path, value)
    assert yaml.safe_load(patch_text(text, path, value)) == expected


def test_failed_atomic_save_keeps_original(store, monkeypatch):
    import config.store as configuration
    original = configuration.atomic_write

    def fail_target(path, data, mode=0o600):
        if path == store.path:
            raise OSError("synthetic disk full")
        original(path, data, mode)

    monkeypatch.setattr(configuration, "atomic_write", fail_target)
    with pytest.raises(OSError):
        store.save({"heartbeat.interval_minutes": 20}, store.read()[0])
    assert store.path.read_text() == SOURCE
    assert len(store.history()) == 1


def test_public_values_never_include_url_credentials_or_environment_secrets(store, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_KEY", "DO_NOT_DISCLOSE")
    store.path.write_text(SOURCE.replace("https://example.test/v1", "https://hidden-password@example.test/v1"))
    public = json.dumps(store.public())
    assert "hidden-password" not in public and "DO_NOT_DISCLOSE" not in public
    assert store.public()["credentials"]["chat"]["configured"] is True


def test_handover_message_is_exposed_as_multiline(store):
    fields = {field["path"]: field for field in store.public()["fields"]}
    assert fields["heartbeat.session_switch.message"]["multiline"] is True
    assert fields["heartbeat.message"]["multiline"] is False


def test_restore_preview_is_revision_guarded_and_only_changes_managed_fields(store):
    result = store.save({"heartbeat.interval_minutes": 20}, store.read()[0])
    store.path.write_text(store.path.read_text().replace("untouched: true", "untouched: false"))
    revision = store.read()[0]
    preview = store.restore(result["backup"], revision)
    assert [item["path"] for item in preview["changes"]] == ["heartbeat.interval_minutes"]
    assert preview["changes"][0]["after"] == 60
    assert yaml.safe_load(store.path.read_text())["heartbeat"]["interval_minutes"] == 20
    store.restore(result["backup"], revision, apply=True)
    assert yaml.safe_load(store.path.read_text())["heartbeat"]["interval_minutes"] == 60
    assert yaml.safe_load(store.path.read_text())["custom_extension"]["untouched"] is False
    with pytest.raises(ConfigurationError):
        store.restore("../config.yaml", store.read()[0])


class FakeLoop:
    def __init__(self, model_config, callback=None):
        self.model_config = model_config
        self.callback = callback
        self.closed = False
        self.client = self

    async def close(self):
        self.closed = True


@pytest.fixture
def service(store, tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic")
    monkeypatch.setattr('config.service.AgentLoop', FakeLoop)
    manager = object.__new__(ConfigManager)
    manager._config_path = str(store.path)
    manager._raw_config = store.read()[2]
    agent = SimpleNamespace(role="chat", manager=manager, _lock=asyncio.Lock(), callback=None, _on_event=AsyncMock(),
                            session_manager=SimpleNamespace(set_model_role=lambda role: None))
    agent._loop = FakeLoop(manager.get_model("chat"))
    heartbeat = Heartbeat(agent, interval_minutes=60, state_file=tmp_path / "synthetic-heartbeat.json")
    return ConfigurationService(manager, agent, heartbeat)


async def test_current_round_finishes_before_new_model_tools_and_interval_apply(service):
    original = service.session._loop
    generation = service.heartbeat._timer_generation
    await service.session._lock.acquire()
    try:
        result = await service.save({"roles.chat.overrides.temperature": 0.8, "roles.chat.tools.enabled": [],
                                     "heartbeat.interval_minutes": 15, "webui.port": 8421,
                                     "models.worker_model.name": "new-worker"}, service.store.read()[0])
        assert result["status"]["applying"]
        assert service.session._loop is original and not original.closed
        assert service.heartbeat.interval_seconds == 3600
        assert service.manager.get_model("worker").name == "worker"
    finally:
        service.session._lock.release()
    await service._task
    assert original.closed
    assert service.session._loop.model_config.temperature == 0.8
    assert service.session._loop.callback is service.session._on_event
    assert service.session._loop.model_config.tool_names == []
    assert service.heartbeat.interval_seconds == 900
    assert service.heartbeat._timer_generation == generation + 1
    snapshot = await service.snapshot()
    assert snapshot["restart_required"] and not snapshot["applying"]
    assert service.manager.snapshot()["webui"]["port"] == 8420
    assert service.manager.get_model("worker").name == "worker"


async def test_configuration_available_before_chat_model_is_initialized(service):
    service.session._loop = None

    snapshot = await service.snapshot()
    assert snapshot["active_model"] is None and "主对话尚未就绪" in snapshot["error"]

    await service.save({"roles.chat.overrides.temperature": 0.8}, service.store.read()[0])
    await service._task

    snapshot = await service.snapshot()
    assert snapshot["active_model"]["name"] == "sample"
    assert snapshot["error"] is None


async def test_environment_endpoint_writes_values_only_to_dotenv(service, monkeypatch):
    from dotenv import dotenv_values
    from fastapi import Depends, FastAPI, HTTPException, Request
    from config.environment import environment_name
    from web.configuration import configuration_router

    path = "providers.primary.api_key_env"
    name = environment_name(path)
    monkeypatch.setenv(name, "synthetic-previous")
    monkeypatch.setattr("config.service.updated_env_keys", set())
    env = service.store.path.with_name(".env")
    env.write_text("# keep comment\nOTHER='unchanged'\n", encoding="utf-8")

    async def authenticated(request: Request):
        if request.headers.get("Authorization") != "Bearer synthetic-admin":
            raise HTTPException(401)

    app = FastAPI()
    app.include_router(configuration_router(service, Depends(authenticated)))
    url = "/api/configuration/environment"
    secret = "synthetic-key'\\with # spaces"
    body = {"path": path, "value": secret, "revision": service.store.read()[0]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.post(url, json=body)).status_code == 401
        client.headers["Authorization"] = "Bearer synthetic-admin"
        invalid = await client.post(url, json={**body, "value": [secret]})
        assert invalid.status_code == 422 and "synthetic-key" not in invalid.text
        invalid = await client.post(url, json={**body, "path": "models.sample.name"})
        assert invalid.status_code == 422 and "synthetic-key" not in invalid.text
        response = await client.post(url, json=body)
        assert response.status_code == 200 and "synthetic-key" not in response.text
        status = response.json()["status"]
        assert status["restart_required"]
        assert next(field for field in status["fields"] if field["path"] == path)["configured"]
        assert service.store.read()[2]["providers"]["primary"]["api_key_env"] == name
        assert dotenv_values(env) == {"OTHER": "unchanged", name: secret}
        assert env.read_text(encoding="utf-8").startswith("# keep comment\n")
        assert service.session._loop.model_config.api_key == "synthetic"
        assert (await client.post(url, json=body)).status_code == 409
        rotated = await client.post(url, json={**body, "revision": status["revision"], "value": "rotated"})
        assert rotated.status_code == 200 and "rotated" not in rotated.text
        assert dotenv_values(env)[name] == "rotated"
        assert env.read_text(encoding="utf-8").count(name + "=") == 1
        for backup in service.store.backup_dir.glob("*.yaml"):
            assert "synthetic-key" not in backup.read_text(encoding="utf-8")


async def test_saves_coalesce_while_round_is_running(service):
    await service.session._lock.acquire()
    await service.save({"roles.chat.overrides.temperature": 0.5}, service.store.read()[0])
    await service.save({"roles.chat.overrides.temperature": 0.9}, service.store.read()[0])
    service.session._lock.release()
    await service._task
    assert service.session._loop.model_config.temperature == 0.9
    assert not (await service.snapshot())["restart_required"]


async def test_dynamic_failure_keeps_current_loop_and_can_retry(service, monkeypatch):
    old = service.session._loop

    def broken(**kw):
        raise ValueError("DO_NOT_DISCLOSE")

    monkeypatch.setattr('config.service.AgentLoop', broken)
    await service.save({"roles.chat.model": "new_model"}, service.store.read()[0])
    await service._task
    state = await service.snapshot()
    assert state["error"] and "DO_NOT_DISCLOSE" not in json.dumps(state)
    assert service.session._loop is old and not old.closed
    assert service.manager.get_model("chat").name == "sample"
    monkeypatch.setattr('config.service.AgentLoop', FakeLoop)
    service.request_apply()
    await service._task
    assert service.session._loop.model_config.name == "new-model"
    assert (await service.snapshot())["error"] is None


async def test_shutdown_does_not_apply_waiting_changes(service):
    await service.session._lock.acquire()
    await service.save({"roles.chat.overrides.temperature": 0.7}, service.store.read()[0])
    await service.close()
    service.session._lock.release()
    assert service.manager.get_model("chat").temperature == 0.3


async def test_adding_dynamic_defaults_does_not_require_restart(service):
    raw = service.store.read()[2]
    del raw["heartbeat"]
    service.store.path.write_text(yaml.safe_dump(raw))
    service.manager.replace_config(raw)
    service.boot = deepcopy(raw)
    service.heartbeat.interval_seconds = 1800
    await service.save({"heartbeat.interval_minutes": 45}, service.store.read()[0])
    await service._task
    assert not (await service.snapshot())["restart_required"]
    assert service.heartbeat.interval_seconds == 2700


async def test_heartbeat_messages_and_window_apply_at_round_boundary(service):
    await service.session._lock.acquire()
    try:
        await service.save({
            "heartbeat.message": "合成心跳",
            "heartbeat.include_in_progress_tasks": True,
            "heartbeat.session_switch.window_start": "23:00",
            "heartbeat.session_switch.window_end": "04:30",
            "heartbeat.session_switch.message": "合成交接提醒",
        }, service.store.read()[0])
        assert service.heartbeat.settings["message"] == "心跳"
    finally:
        service.session._lock.release()
    await service._task
    assert service.error is None
    assert service.heartbeat.settings["message"] == "合成心跳"
    assert service.heartbeat.settings["include_in_progress_tasks"] is True
    assert service.heartbeat.settings["session_switch"]["window_start"] == "23:00"
    assert service.heartbeat.settings["session_switch"]["message"] == "合成交接提醒"
    assert not (await service.snapshot())["restart_required"]


async def test_program_timezone_is_saved_but_requires_restart(service):
    from runtime.timezone import timezone_name
    original = timezone_name()
    await service.save({"app.timezone": "UTC"}, service.store.read()[0])
    await service._task
    state = await service.snapshot()
    field = next(item for item in state["fields"] if item["path"] == "app.timezone")
    assert field["value"] == "UTC" and field["active"] == original
    assert field["pending"] and field["effect"] == "restart"
    assert state["restart_required"]
    assert timezone_name() == original


@pytest.mark.parametrize("value", ["Not/A_Zone", "", "../Asia/Shanghai", 8, None, ["UTC"]])
def test_invalid_program_timezone_is_rejected(store, value):
    original = store.path.read_text()
    with pytest.raises(ConfigurationError):
        store.save({"app.timezone": value}, store.read()[0])
    assert store.path.read_text() == original


@pytest.mark.parametrize("changes", [
    {"heartbeat.session_switch.window_start": "25:00"},
    {"heartbeat.session_switch.window_start": "7:00"},
    {"heartbeat.session_switch.window_end": "01:00"},
    {"heartbeat.include_in_progress_tasks": "true"},
    {"heartbeat.message": ""},
])
def test_invalid_heartbeat_policy_does_not_write_config(store, changes):
    original = store.path.read_text()
    with pytest.raises(ConfigurationError):
        store.save(changes, store.read()[0])
    assert store.path.read_text() == original


async def test_publish_failure_rolls_back_config_and_keeps_old_client(service):
    original = service.session._loop
    calls = []

    def fail_once(role):
        calls.append(role)
        if len(calls) == 1:
            raise ValueError("synthetic update failure")

    service.session.session_manager.set_model_role = fail_once
    await service.save({"roles.chat.overrides.temperature": 0.8}, service.store.read()[0])
    await service._task
    assert service.error
    assert service.manager.get_model("chat").temperature == 0.3
    assert service.session._loop is original and not original.closed


async def test_history_changes_only_when_model_change_takes_effect(service, tmp_path, monkeypatch):
    from agent.compressor import Compressor
    from agent.handoff import model_identity
    from agent.session_manager import SessionManager

    monkeypatch.setattr(Compressor, '_load_system_prompt', lambda self: 'Synthetic')
    monkeypatch.setattr(SessionManager, '_assemble_summary_message', lambda self: 'Synthetic')
    sm = SessionManager(service.manager, db_path=str(tmp_path / 'history.db'),
                        cache_path=str(tmp_path / 'active.json'),
                        compression_state_path=str(tmp_path / 'compression.json'))
    active = sm.get_or_create_active_session()
    active.history = [{'role': 'user', 'content': 'Question'},
                      {'role': 'assistant', 'content': 'Answer', 'reasoning_content': 'Old reasoning'}]
    sm._history_model = model_identity(service.session._loop.model_config)
    sm.save_session()
    service.session.session_manager = sm
    await service.session._lock.acquire()
    await service.save({'roles.chat.model': 'new_model'}, service.store.read()[0])
    assert 'reasoning_content' in active.history[-1]
    service.session._lock.release()
    await service._task
    assert service.error is None
    assert active.history[-1] == {'role': 'assistant', 'content': 'Answer'}
    assert json.loads(sm.cache_path.read_text())['history_model']['model'] == 'new-model'


async def test_history_write_failure_rolls_back_live_model_and_interval(service, monkeypatch):
    original = service.session._loop
    def fail(cfg):
        raise OSError('synthetic write failure')
    service.session.session_manager.apply_model_config = fail
    await service.save({'roles.chat.model': 'new_model', 'heartbeat.interval_minutes': 10}, service.store.read()[0])
    await service._task
    assert service.error
    assert service.session._loop is original and not original.closed
    assert service.manager.get_model('chat').name == 'sample'
    assert service.heartbeat.interval_seconds == 3600


def test_cli_previews_without_changing_files_and_requires_revision_to_write(store, monkeypatch, capsys):
    from scripts.configure import main
    args = ["configure", "--config", str(store.path), "set", "--set", "heartbeat.interval_minutes=15"]
    monkeypatch.setattr("sys.argv", args)
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert store.path.read_text() == SOURCE
    monkeypatch.setattr("sys.argv", args + ["--apply"])
    assert main() == 1
    assert store.path.read_text() == SOURCE
    monkeypatch.setattr("sys.argv", args + ["--apply", "--revision", result["revision"]])
    assert main() == 0
    assert store.read()[2]["heartbeat"]["interval_minutes"] == 15


async def test_configuration_routes_require_both_device_and_password(service, tmp_path):
    settings = WebSettings(str(tmp_path / "web-settings.json"))
    cookie = settings.create_device("synthetic")
    settings.set_device_auth(True)
    settings.set_password("synthetic password")
    app = create_app(ConnectionManager(), None, service.session, service.heartbeat, None,
                     SimpleNamespace(db_path=tmp_path / "unused.db"), settings, None,
                     configuration_service=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        revision = service.store.read()[0]
        body = {"revision": revision, "changes": {"heartbeat.interval_minutes": 15}}
        for method, path, payload in [("GET", "", None), ("POST", "/preview", body),
                                      ("PUT", "", body), ("POST", "/apply", None),
                                      ("POST", "/discover-models", {"revision": revision, "provider": "demo"}),
                                      ("POST", "/restart", {"revision": revision, "reembed": True}),
                                      ("POST", "/structure", {"revision": revision, "operations": [{}]}),
                                      ("POST", "/restore", {"revision": revision, "backup": "invalid"})]:
            response = await client.request(method, "/api/configuration" + path, json=payload)
            assert response.status_code == 401
        client.cookies.set(DEVICE_COOKIE, cookie)
        assert (await client.get("/api/configuration")).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.issue_token()
        assert (await client.get("/api/configuration")).status_code == 200
        response = await client.post("/api/configuration/preview", json=body)
        assert response.status_code == 200 and service.store.read()[0] == revision
        response = await client.put("/api/configuration", json=body)
        assert response.status_code == 200
        await service._task
        assert (await client.put("/api/configuration", json=body)).status_code == 409
        cookie_id = next(iter(settings.list_devices()))["id"]
        settings.revoke_device(cookie_id)
        assert (await client.get("/api/configuration")).status_code == 401


def test_config_snapshot_cannot_mutate_manager(service):
    raw = service.manager.snapshot()
    raw["models"]["sample"]["name"] = "mutated"
    assert service.manager.get_model("chat").name == "sample"
    candidate = deepcopy(raw)
    assert service.manager.get_model("chat", raw_config=candidate).name == "mutated"
    assert service.manager.get_model("chat").name == "sample"


@pytest.mark.parametrize("reembed", [False, True])
async def test_restart_endpoint_waits_and_blocks_configuration_writes(service, reembed):
    from fastapi import Depends, FastAPI
    from runtime.control import RestartController
    from web.configuration import configuration_router

    service.restart = RestartController(service.session)
    if reembed:
        text = SOURCE.replace(
            "  worker:\n    model: worker_model\n    tools: {enabled: []}",
            "  worker:\n    model: worker_model\n    tools: {enabled: []}\n  embedding:\n    model: sample",
        )
        service.store.path.write_text(text, encoding="utf-8")
    app = FastAPI()

    async def authenticated():
        pass

    app.include_router(configuration_router(service, Depends(authenticated)))
    revision = service.store.read()[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        stale = await client.post("/api/configuration/restart", json={"revision": "0" * 64, "reembed": reembed})
        assert stale.status_code == 409 and service.restart.state == "idle"
        await service.session._lock.acquire()
        response = await client.post("/api/configuration/restart", json={"revision": revision, "reembed": reembed})
        assert response.status_code == 202
        assert response.json()["reembedding"] is reembed
        assert service.restart.reembed_revision == (revision if reembed else None)
        assert service.restart.state == "waiting" and not service.restart.ready.is_set()
        blocked = await client.put("/api/configuration", json={"revision": revision, "changes": {"heartbeat.interval_minutes": 15}})
        assert blocked.status_code == 422 and service.store.read()[0] == revision
        service.session._lock.release()
        await service.restart._task
        assert service.restart.ready.is_set()


async def test_restart_rejects_invalid_saved_configuration(service):
    from runtime.control import RestartController

    service.restart = RestartController(service.session)
    service.store.path.write_text(service.store.path.read_text().replace("port: 8420", "port: 0"))
    with pytest.raises(ConfigurationError):
        await service.prepare_restart(service.store.read()[0])
    assert service.restart.state == "idle"


async def test_restart_keeps_optional_role_credentials_lazy(service, monkeypatch):
    from runtime.control import RestartController

    monkeypatch.delenv("UNCONFIGURED_OPTIONAL_ROLE", raising=False)
    raw = SOURCE.replace(
        "  secondary:\n    type: openai_compatible\n    base_url: https://example.test/v1\n    api_key_env: SYNTHETIC_KEY",
        "  secondary:\n    type: openai_compatible\n    base_url: https://example.test/v1\n    api_key_env: UNCONFIGURED_OPTIONAL_ROLE",
    ).replace(
        "  worker:\n    model: worker_model\n    tools: {enabled: []}",
        "  worker:\n    model: worker_model\n    tools: {enabled: []}\n  embedding:\n    model: worker_model",
    )
    service.store.path.write_text(raw, encoding="utf-8")
    service.restart = RestartController(service.session)
    await service.prepare_restart(service.store.read()[0])
    assert service.restart.state == "idle"
    with pytest.raises(ConfigurationError, match="向量嵌入角色"):
        await service.prepare_restart(service.store.read()[0], reembed=True)
    assert service.restart.reembed_revision is None


def test_backup_history_uses_write_time_instead_of_random_suffix(store):
    from os import utime

    store.backup_dir.mkdir(parents=True, exist_ok=True)
    older = store.backup_dir / ("20261017T000000Z-" + "f" * 32 + ".yaml")
    newer = store.backup_dir / ("20261017T000000Z-" + "0" * 32 + ".yaml")
    for path in (older, newer):
        path.write_bytes(store.path.read_bytes())
    utime(older, ns=(1000000000, 1000000000))
    utime(newer, ns=(2000000000, 2000000000))
    assert [item["id"] for item in store.history()][:2] == [newer.name, older.name]
