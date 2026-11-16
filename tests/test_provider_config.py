"""供应商、模型与角色配置：纯解析、文件事务与假客户端，不请求外部服务。"""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from config.store import ConfigurationConflict, ConfigurationError, ConfigStore
from config.service import ConfigurationService
from runtime.heartbeat import Heartbeat
from agent.protocol import ModelConfig
from config.manager import ConfigManager
from config.providers import automatic_identifier, balance_connection, file_connections, resolved_models, validate_structure


@pytest.fixture
def layered():
    return {
        "config_version": 2,
        "providers": {
            "primary": {"type": "deepseek", "api_key_env": "PROVIDER_TEST_KEY", "proxy": ""},
            "second": {"type": "deepseek", "api_key_env": "PROVIDER_TEST_SECOND"},
        },
        "models": {
            "fast": {"provider": "primary", "name": "synthetic-fast", "vision": True, "context_window": 10000,
                     "defaults": {"temperature": 0.3}},
            "other": {"provider": "second", "name": "synthetic-other", "defaults": {"temperature": 0.1}},
        },
        "roles": {
            "chat": {"model": "fast", "selectable": True, "max_iterations": 50,
                     "overrides": {"stream": False}, "tools": {"enabled": ["get_time"]}},
            "compress": {"model": "fast", "max_iterations": 3, "overrides": {"temperature": 0.7}},
        },
        "heartbeat": {"interval_minutes": 60},
        "files": {"uploader": "gcs", "base_url": "gs://synthetic-bucket/images"},
    }


def manager_for(raw, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    manager = object.__new__(ConfigManager)
    manager._raw_config = deepcopy(raw)
    manager._config_path = str(path)
    return manager


async def test_fresh_defaults_allow_incremental_model_setup(tmp_path, monkeypatch):
    from config.environment import environment_name
    from memory.concept_aligner import AlignmentConfig

    defaults = Path(__file__).resolve().parents[1] / "config.defaults.yaml"
    (tmp_path / defaults.name).write_bytes(defaults.read_bytes())
    store = ConfigStore(tmp_path / "config.yaml")
    _, _, raw = store.read()
    assert raw["providers"] == raw["models"] == {}
    assert set(raw["roles"]) == {
        "chat", "compress", "memory_extractor", "embedding", "concept_alignment", "concept_dedup",
    }
    store.validate(raw, [])
    assert not AlignmentConfig.from_app_config(raw).model
    manager = object.__new__(ConfigManager)
    manager._raw_config = raw
    manager._config_path = str(store.path)
    session = SimpleNamespace(role="chat", _loop=None)
    restart = SimpleNamespace(state="idle", snapshot=lambda: {"available": True})
    service = ConfigurationService(manager, session, None, restart=restart)
    path = "providers.test.api_key_env"
    monkeypatch.setenv(environment_name(path), "synthetic-previous")
    monkeypatch.setattr("config.service.updated_env_keys", set())
    snapshot = await service.snapshot()
    assert snapshot["active_model"] is None
    assert await service.prepare_restart(snapshot["revision"]) == {"available": True}

    for kind, key, value in [
        ("providers", "test", {"type": "deepseek", "api_key_env": "PROVIDER_TEST_KEY"}),
        ("models", "chat_model", {"provider": "test", "name": "synthetic-chat"}),
        ("providers", "scoring", {"type": "typesafe", "api_key_env": "SCORING_TEST_KEY"}),
        ("models", "scoring_model", {"provider": "scoring", "name": "synthetic-scoring", "api_style": "systemone"}),
    ]:
        store.structure([{"op": "create", "kind": kind, "id": key, "value": value}], store.read()[0], apply=True)
    saved_secret = await service.save_environment_value(path, "synthetic-key", store.read()[0])
    snapshot = saved_secret["status"]
    chat_model = next(field for field in snapshot["fields"] if field["path"] == "roles.chat.model")
    assert chat_model["value"] is None and chat_model["choices"] == ("chat_model",)
    scoring_model = next(field for field in snapshot["fields"] if field["path"] == "roles.concept_alignment.model")
    assert scoring_model["value"] is None and scoring_model["choices"] == ("scoring_model",)
    store.save({"roles.chat.model": "chat_model", "roles.concept_alignment.model": "scoring_model"},
               snapshot["revision"])
    revision, _, bound = store.read()
    assert manager.get_model("chat", raw_config=bound).name == "synthetic-chat"
    assert manager.get_model("chat", raw_config=bound).api_key == "synthetic-key"
    assert AlignmentConfig.from_app_config(bound).model == "synthetic-scoring"
    assert await service.prepare_restart(revision) == {"available": True}
    store.save({"roles.chat.model": None}, revision)
    assert store.read()[2]["roles"]["chat"]["model"] is None


@pytest.mark.parametrize("kind,value,existing,expected", [
    ("providers", {"type": "openai_compatible"}, set(), "provider"),
    ("providers", {"type": "deepseek", "label": "任意备注"}, {"deepseek", "deepseek-2"}, "deepseek-3"),
    ("providers", {"type": "google_vertex"}, set(), "google_vertex"),
    ("models", {"provider": "primary", "name": "vendor/model.v1"}, set(), "primary/vendor-model-v1"),
    ("models", {"provider": "primary", "name": "sample", "label": "备注"}, {"primary/sample"}, "primary/sample-2"),
])
def test_automatic_identifiers_use_connection_type_and_model_name(kind, value, existing, expected):
    assert automatic_identifier(kind, value, existing) == expected


def test_auto_identifiers_in_structure_preview_match_saved_keys(layered, tmp_path):
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    operations = [
        {"op": "create", "kind": "providers", "value": {"type": "openai_compatible", "base_url": "https://example.test/v1", "api_key_env": "TEST_KEY", "label": "备用连接"}},
        {"op": "create", "kind": "models", "value": {"provider": "provider", "name": "sample.model"}},
        {"op": "create", "kind": "models", "value": {"provider": "provider", "name": "sample/model"}},
    ]
    revision = store.read()[0]
    preview = store.structure(operations, revision)
    assert [d["path"] for d in preview["changes"]] == ["providers.provider", "models.provider/sample-model", "models.provider/sample-model-2"]
    saved = store.structure(operations, revision, apply=True)
    assert saved["changes"] == preview["changes"]
    public = store.public()
    assert next(p for p in public["entities"]["providers"] if p["id"] == "provider")["label"] == "备用连接"
    assert next(m for m in public["entities"]["models"] if m["id"] == "provider/sample-model")["label"] == "provider/sample-model"
    changed = store.save({"models.provider/sample-model.defaults.temperature": 0.4,
                          "roles.chat.model": "provider/sample-model"}, saved["revision"])
    assert changed["revision"] != saved["revision"]
    assert store.read()[2]["models"]["provider/sample-model"]["defaults"]["temperature"] == 0.4


def test_auto_identifier_truncation_still_preserves_uniqueness():
    first = automatic_identifier("models", {"provider": "primary", "name": "x" * 100}, set())
    second = automatic_identifier("models", {"provider": "primary", "name": "x" * 100}, {first})
    assert len(first) == len(second) == len("primary/") + 64 and second.endswith("-2")


def test_roles_share_connection_and_model_but_keep_policy_and_parameter_overrides(layered, tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_TEST_KEY", "synthetic-key")
    manager = manager_for(layered, tmp_path)
    chat, compress = manager.get_model("chat"), manager.get_model("compress")
    assert (chat.provider_id, chat.model_id, chat.role) == ("primary", "fast", "chat")
    assert (compress.provider_id, compress.model_id, compress.role) == ("primary", "fast", "compress")
    assert chat.name == compress.name == "synthetic-fast"
    assert chat.api_key == compress.api_key == "synthetic-key"
    assert chat.temperature == 0.3 and compress.temperature == 0.7
    assert chat.max_iterations == 50 and compress.max_iterations == 3
    assert chat.proxy == "" and chat.tool_names == ["get_time"]
    assert manager.list_model_roles() == ["chat"]
    assert layered["models"]["fast"]["defaults"] == {"temperature": 0.3}


def test_generic_default_does_not_enable_deepseek_quirks():
    assert ModelConfig("sample", "https://example.test").provider == "openai_compatible"


@pytest.mark.parametrize("change", [
    lambda x: x["models"]["fast"].update(provider="missing"),
    lambda x: x["roles"]["chat"].update(model="missing"),
    lambda x: x["providers"]["primary"].update(type="unsupported"),
    lambda x: x["providers"]["primary"].update(auth="adc"),
    lambda x: x["models"]["fast"].update(api_style="interactions"),
    lambda x: x["roles"]["chat"].update(overrides={"api_key_env": "SECRET"}),
    lambda x: x["providers"]["primary"].update(api_key="DO_NOT_STORE_SECRETS"),
    lambda x: x["roles"]["chat"].update(tools="invalid"),
])
def test_invalid_references_and_cross_layer_fields_are_rejected(layered, change):
    change(layered)
    with pytest.raises(ConfigurationError):
        validate_structure(layered)


def test_standalone_model_is_checked_even_before_role_assignment(layered, tmp_path):
    layered["models"]["other"]["context_window"] = True
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    with pytest.raises(ConfigurationError):
        store.validate(layered, [])


def test_flat_legacy_config_and_config_version_1_are_rejected():
    for bad in (
        {"models": {"chat": {"provider": "deepseek", "name": "same", "api_key_env": "TEST"}}},
        {"config_version": 1, "providers": {}, "models": {}, "roles": {}},
    ):
        with pytest.raises(ConfigurationError):
            validate_structure(bad)


def test_entity_create_and_delete_are_previewed_backed_up_and_reference_guarded(layered, tmp_path):
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    revision = store.read()[0]
    operation = [{"op": "create", "kind": "providers", "id": "third", "value": {
        "type": "openai_compatible", "base_url": "https://gateway.test/v1", "api_key_env": "THIRD_KEY"}}]
    assert store.structure(operation, revision)["changes"]
    assert "third" not in store.read()[2]["providers"]
    result = store.structure(operation, revision, apply=True)
    assert result["backup"] and "third" in store.read()[2]["providers"]
    store.structure([{"op": "delete", "kind": "providers", "id": "third"}], store.read()[0], apply=True)
    for kind, key in [("providers", "primary"), ("models", "fast"), ("roles", "chat")]:
        before = store.read()[0]
        with pytest.raises(ConfigurationError):
            store.structure([{"op": "delete", "kind": kind, "id": key}], before, apply=True)
        assert store.read()[0] == before


def test_yaml_tool_list_can_be_edited_without_losing_comments(layered, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(layered, sort_keys=False) + "# preserve following section\nwebui:\n  port: 8420\n")
    store = ConfigStore(path)
    store.save({"roles.chat.tools.enabled": ["get_time", "browser"], "roles.chat.overrides.temperature": 0.7}, store.read()[0])
    assert "# preserve following section" in path.read_text()
    assert resolved_models(store.read()[2])["chat"]["tools"]["enabled"] == ["get_time", "browser"]


def test_balance_is_explicit_and_file_storage_remains_independent(layered):
    assert balance_connection(layered) == {}
    layered["usage"] = {"balance_provider": "second"}
    assert balance_connection(layered)["api_key_env"] == "PROVIDER_TEST_SECOND"
    layered["files"]["uploaders"] = {"deepseek_files": {"provider_ref": "second"}}
    files = file_connections(layered)
    assert files["base_url"] == "gs://synthetic-bucket/images"
    assert files["uploaders"]["deepseek_files"]["api_key_env"] == "PROVIDER_TEST_SECOND"


def test_provider_used_only_for_upload_or_balance_cannot_be_deleted(layered, tmp_path):
    layered["models"].pop("other")
    layered["usage"] = {"balance_provider": "second"}
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    with pytest.raises(ConfigurationError):
        store.structure([{"op": "delete", "kind": "providers", "id": "second"}], store.read()[0], apply=True)
    layered.pop("usage")
    layered["files"]["uploaders"] = {"deepseek_files": {"provider_ref": "second"}}
    manager_for(layered, tmp_path)
    with pytest.raises(ConfigurationError):
        store.structure([{"op": "delete", "kind": "providers", "id": "second"}], store.read()[0], apply=True)


def test_role_can_remove_override_and_follow_future_model_defaults(layered, tmp_path):
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    store.save({"roles.compress.overrides.temperature": {"$inherit": True}}, store.read()[0])
    assert "temperature" not in store.read()[2]["roles"]["compress"]["overrides"]
    assert resolved_models(store.read()[2])["compress"]["temperature"] == 0.3
    store.save({"models.fast.defaults.temperature": 0.5}, store.read()[0])
    assert resolved_models(store.read()[2])["compress"]["temperature"] == 0.5


def test_pending_model_default_displays_actual_inherited_runtime_value(layered, tmp_path):
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    active = deepcopy(layered)
    store.save({"models.fast.defaults.temperature": 0.8}, store.read()[0])
    field = next(f for f in store.public(active)["fields"] if f["path"] == "roles.chat.overrides.temperature")
    assert field["value"] == 0.8 and field["active"] == 0.3 and field["pending"]


def test_provider_and_protocol_can_change_in_one_reviewed_transaction(layered, tmp_path):
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    changes = {"providers.primary.type": "google_developer", "providers.primary.base_url": "https://google.example.test/v1beta",
               "models.fast.api_style": "interactions"}
    preview = store.preview(changes, store.read()[0])
    assert preview["changes"][0]["impact"]["roles"] == ["chat", "compress"]
    store.save(changes, store.read()[0])
    assert resolved_models(store.read()[2])["chat"]["api_style"] == "interactions"


class FakeLoop:
    def __init__(self, model_config, callback=None):
        self.model_config, self.closed = model_config, False
        self.callback = callback
        self.client = self

    async def close(self):
        self.closed = True


async def test_model_discovery_is_readonly_and_rejects_stale_revisions(layered, tmp_path, monkeypatch):
    manager = manager_for(layered, tmp_path)
    service = ConfigurationService(manager, SimpleNamespace(), SimpleNamespace())
    original = service.store.path.read_bytes()
    calls = []

    async def fake_fetch(provider):
        calls.append(provider)
        return {"models": [{"name": "catalog-only", "label": "Catalog only"}], "truncated": False}

    monkeypatch.setattr('config.catalog.fetch_catalog', fake_fetch)
    result = await service.discover_models("primary", service.store.read()[0])
    assert result["models"][0]["name"] == "catalog-only"
    assert calls[0]["type"] == "deepseek"
    assert service.store.path.read_bytes() == original and not service.store.backup_dir.exists()
    with pytest.raises(ConfigurationConflict):
        await service.discover_models("primary", "0" * 64)
    assert len(calls) == 1

    async def change_during_fetch(provider):
        service.store.save({"providers.primary.label": "changed"}, service.store.read()[0])
        return {"models": [], "truncated": False}

    monkeypatch.setattr('config.catalog.fetch_catalog', change_during_fetch)
    with pytest.raises(ConfigurationConflict, match="读取期间"):
        await service.discover_models("primary", service.store.read()[0])


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:pass@example.test/models",
                                 "https://example.test/models?api_key=secret", "https://example.test/models#token"])
def test_custom_catalog_url_rejects_credentials_and_non_http_urls(layered, tmp_path, url):
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    with pytest.raises(ConfigurationError):
        store.save({"providers.primary.models_url": url}, store.read()[0])


async def test_catalog_url_edit_is_immediate_without_rebuilding_model_or_requiring_restart(layered, tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_TEST_KEY", "synthetic")
    manager = manager_for(layered, tmp_path)
    agent = SimpleNamespace(role="chat", _lock=asyncio.Lock(), _loop=FakeLoop(manager.get_model("chat")))
    service = ConfigurationService(manager, agent, SimpleNamespace())
    previous_loop = agent._loop
    url = "https://catalog.example.test/custom/models?api-version=2026-01-01"
    await service.save({"providers.primary.models_url": url}, service.store.read()[0])
    await service._task
    snapshot = await service.snapshot()
    field = next(f for f in snapshot["fields"] if f["path"] == "providers.primary.models_url")
    assert field["value"] == field["active"] == url and not field["pending"]
    assert field["effect"] == "immediate" and not snapshot["restart_required"]
    assert agent._loop is previous_loop
    assert service._role_ready(service.store.read()[2], "chat")


async def test_remarks_do_not_change_ids_or_require_rebuilding_clients(layered, tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_TEST_KEY", "synthetic")
    manager = manager_for(layered, tmp_path)
    agent = SimpleNamespace(role="chat", _lock=asyncio.Lock(), _loop=FakeLoop(manager.get_model("chat")))
    service = ConfigurationService(manager, agent, SimpleNamespace())
    previous = agent._loop
    changes = {"providers.primary.label": "主账号", "models.fast.label": "日常模型", "roles.chat.label": "主对话"}
    await service.save(changes, service.store.read()[0])
    await service._task
    state = await service.snapshot()
    assert not state["restart_required"] and service._role_ready(service.store.read()[2], "chat")
    assert agent._loop is previous and state["entities"]["providers"][0]["id"] == "primary"
    assert state["entities"]["providers"][0]["label"] == "主账号"
    assert all(f["effect"] == "immediate" and not f["pending"] for f in state["fields"] if f["path"] in changes)
    await service.save({key: None for key in changes}, service.store.read()[0])
    await service._task
    assert (await service.snapshot())["entities"]["providers"][0]["label"] == "primary"


async def test_role_switch_is_dynamic_but_shared_provider_changes_wait_for_restart(layered, tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_TEST_KEY", "first")
    monkeypatch.setenv("PROVIDER_TEST_SECOND", "second")
    monkeypatch.setattr('config.service.AgentLoop', FakeLoop)
    manager = manager_for(layered, tmp_path)
    agent = SimpleNamespace(role="chat", _lock=asyncio.Lock(), callback=None, _on_event=AsyncMock(),
                            session_manager=SimpleNamespace(set_model_role=lambda role: None),
                            _loop=FakeLoop(manager.get_model("chat")))
    heartbeat = Heartbeat(agent, 60, tmp_path / "synthetic-heartbeat.json")
    service = ConfigurationService(manager, agent, heartbeat)
    revision = service.store.read()[0]
    await agent._lock.acquire()
    await service.save({"roles.chat.model": "other"}, revision)
    assert agent._loop.model_config.name == "synthetic-fast"
    agent._lock.release()
    await service._task
    assert agent._loop.model_config.name == "synthetic-other"
    assert agent._loop.callback is agent._on_event
    assert manager.get_model("compress").name == "synthetic-fast"
    await service.save({"providers.second.proxy": "http://proxy.test:8080"}, service.store.read()[0])
    await service._task
    state = await service.snapshot()
    assert state["restart_required"]
    assert agent._loop.model_config.proxy is None
    preview = await service.preview({"roles.chat.overrides.temperature": 0.9}, service.store.read()[0])
    assert preview["changes"][0]["effect"] == "restart"
    await service.save({"roles.chat.overrides.temperature": 0.9}, service.store.read()[0])
    await service._task
    assert agent._loop.model_config.temperature == 0.1


@pytest.mark.parametrize("url", ["https://user:secret@example.test/balance", "file:///balance",
                                 "https://example.test/balance?token=secret"])
def test_balance_url_validated_like_other_metadata(layered, tmp_path, url):
    store = ConfigStore(manager_for(layered, tmp_path).config_path)
    with pytest.raises(ConfigurationError):
        store.save({"providers.primary.balance_url": url}, store.read()[0])


async def test_balance_settings_are_immediate_without_model_restart(layered, tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_TEST_KEY", "synthetic")
    manager = manager_for(layered, tmp_path)
    agent = SimpleNamespace(role="chat", _lock=asyncio.Lock(), _loop=FakeLoop(manager.get_model("chat")))
    service = ConfigurationService(manager, agent, SimpleNamespace())
    original = agent._loop
    changes = {"providers.primary.balance_mode": "deepseek",
               "providers.primary.balance_url": "https://billing.example.test/credits?version=1"}
    await service.save(changes, service.store.read()[0])
    await service._task
    snapshot = await service.snapshot()
    for field in snapshot["fields"]:
        if field["path"] in changes:
            assert field["value"] == field["active"] == changes[field["path"]]
            assert field["effect"] == "immediate" and not field["pending"]
    assert not snapshot["restart_required"] and agent._loop is original
    assert service._role_ready(service.store.read()[2], "chat")


def test_snapshot_restore_adds_removes_and_modifies_linked_entities(layered, tmp_path):
    manager = manager_for(layered, tmp_path)
    store = ConfigStore(manager.config_path)
    original = store.read()[2]
    created = store.structure([
        {"op": "delete", "kind": "models", "id": "other"},
        {"op": "delete", "kind": "providers", "id": "second"},
        {"op": "create", "kind": "providers", "id": "extra", "value": {
            "type": "openai_compatible", "base_url": "https://example.test/v1", "api_key_env": "TEST"}},
    ], store.read()[0], apply=True)
    store.save({"models.fast.name": "changed"}, store.read()[0])
    current = store.read()[0]
    preview = store.restore(created["backup"], current)
    assert store.read()[0] == current
    assert {item["path"] for item in preview["changes"]} >= {"models.other", "providers.second", "providers.extra", "models.fast.name"}
    applied = store.restore(created["backup"], current, apply=True)
    assert applied["backup"] and store.read()[2] == original
    assert store.restore(created["backup"], store.read()[0])["changes"] == []
    with pytest.raises(ConfigurationConflict):
        store.restore(created["backup"], current, apply=True)


def test_vertex_scoped_fields_resolve_and_enable_lazy_uploads(layered, tmp_path):
    layered['providers']['primary'] = dict(type='google_vertex', auth='adc', project='synthetic',
        credentials_env='SYNTHETIC_VERTEX_KEY', gcs_base_url='gs://synthetic-bucket/images')
    layered['models']['fast']['api_style'] = 'interactions'
    validate_structure(layered)
    cfg = manager_for(layered, tmp_path).get_model('chat')
    assert cfg.credentials_env == 'SYNTHETIC_VERTEX_KEY'
    assert cfg.gcs_base_url == 'gs://synthetic-bucket/images'
    assert file_connections(layered)['defer_uploads'] is True
    for field, value in [('credentials_env', '/secret.json'), ('gcs_base_url', 'https://wrong.test')]:
        bad = deepcopy(layered)
        bad['providers']['primary'][field] = value
        with pytest.raises(ConfigurationError):
            validate_structure(bad)
