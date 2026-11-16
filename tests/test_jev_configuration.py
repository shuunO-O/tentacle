"""JEV 配置接入回归；仅使用合成配置、临时文件与假 HTTP。"""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from agent.session import AgentSession
from config.store import ConfigStore, ConfigurationError
from memory.concept_aligner import AlignmentConfig, JevClient
from memory.concept_dedup import ConceptDedupManager
from config.providers import concept_connection, validate_structure
from web.settings import WebSettings


@pytest.fixture
def layered():
    return {
        "config_version": 2,
        "providers": {
            "chat_conn": {"type": "openai_compatible", "base_url": "https://example.test/v1", "api_key_env": "CHAT_TEST_KEY"},
            "align_conn": {"type": "openai_compatible", "base_url": "https://example.test/v1", "api_key_env": "JEV_TEST_KEY"},
            "dedup_conn": {"type": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "HISTORY_TEST_KEY"},
        },
        "models": {
            "chat_m": {"provider": "chat_conn", "name": "synthetic-chat"},
            "align_m": {"provider": "align_conn", "name": "synthetic-jev", "api_style": "systemone"},
            "dedup_m": {"provider": "dedup_conn", "name": "typesafe/synthetic-jev", "api_style": "systemone"},
        },
        "roles": {
            "chat": {"model": "chat_m"},
            "concept_alignment": {"model": "align_m", "selectable": False},
            "concept_dedup": {"model": "dedup_m", "selectable": False},
        },
        "memory": {
            "concept_alignment": {"enabled": False, "model_role": "concept_alignment", "same_probability": 0.97},
            "concept_dedup": {"model_role": "concept_dedup"},
        },
    }


def test_legacy_direct_concept_keys_are_rejected(layered):
    raw = deepcopy(layered)
    raw["memory"]["concept_alignment"]["base_url"] = "https://legacy.test/v1"
    with pytest.raises(ConfigurationError, match="model_role"):
        validate_structure(raw)
    with pytest.raises(ConfigurationError, match="model_role"):
        concept_connection(raw, "concept_alignment")


def test_jev_fields_save_and_runtime_uses_catalog_assignments(layered, tmp_path):
    raw = deepcopy(layered)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    store = ConfigStore(path)
    fields = {f["path"]: f for f in store.public()["fields"]}
    assert fields["memory.concept_alignment.enabled"]["value"] is False
    assert set(fields["memory.concept_alignment.model_role"]["choices"]) == {"concept_alignment", "concept_dedup"}
    assert "roles.concept_alignment.tools.enabled" not in fields
    changes = {"memory.concept_alignment.enabled": True,
               "memory.concept_alignment.model_role": "concept_dedup",
               "memory.concept_alignment.same_probability": 0.99}
    store.save(changes, store.read()[0])
    config = AlignmentConfig.from_app_config(store.read()[2])
    assert config.enabled and config.api_key_env == "HISTORY_TEST_KEY"
    assert config.model == "typesafe/synthetic-jev" and config.same_probability == 0.99
    assert all(d["effect"] == "restart" for d in store.preview(
        {"memory.concept_alignment.enabled": False}, store.read()[0])["changes"])
    for section in ("concept_alignment", "concept_dedup"):
        with pytest.raises(ConfigurationError):
            store.structure([{"op": "delete", "kind": "roles", "id": section}], store.read()[0])
    with pytest.raises(ConfigurationError):
        store.preview({"memory.concept_alignment.same_probability": 0.5}, store.read()[0])


def test_role_model_and_connection_edits_reach_jev_runtime(layered, tmp_path):
    raw = deepcopy(layered)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    store = ConfigStore(path)
    model_id = raw["roles"]["concept_alignment"]["model"]
    provider_id = raw["models"][model_id]["provider"]
    store.save({f"models.{model_id}.name": "replacement-jev",
                f"providers.{provider_id}.base_url": "https://replacement.test/v1",
                f"providers.{provider_id}.api_key_env": "REPLACEMENT_TEST_KEY",
                f"providers.{provider_id}.proxy": "http://proxy.test:8080"}, store.read()[0])
    config = AlignmentConfig.from_app_config(store.read()[2])
    assert (config.model, config.base_url, config.api_key_env, config.proxy) == (
        "replacement-jev", "https://replacement.test/v1", "REPLACEMENT_TEST_KEY", "http://proxy.test:8080")


def test_jev_roles_are_not_offered_for_chat_and_cannot_be_assigned_to_chat(layered):
    raw = deepcopy(layered)
    session = object.__new__(AgentSession)
    session.manager = SimpleNamespace(snapshot=lambda: raw)
    jev_model = raw["roles"]["concept_alignment"]["model"]
    assert jev_model not in session.list_models()
    raw["roles"]["chat"]["model"] = jev_model
    with pytest.raises(ConfigurationError, match="不能使用 JEV"):
        validate_structure(raw)


def test_defaults_define_jev_roles_safely(tmp_path):
    defaults = Path(__file__).resolve().parents[1] / "config.defaults.yaml"
    default_text = defaults.read_text(encoding="utf-8")
    (tmp_path / defaults.name).write_text(default_text, encoding="utf-8")
    store = ConfigStore(tmp_path / "config.yaml")
    assert not AlignmentConfig.from_app_config(store.read()[2]).model
    assert {"concept_alignment", "concept_dedup"} <= store.read()[2]["roles"].keys()
    assert (tmp_path / defaults.name).read_text(encoding="utf-8") == default_text


def test_history_manager_resolves_its_independent_model_role(layered, tmp_path, monkeypatch):
    raw = deepcopy(layered)
    monkeypatch.setattr("memory.concept_dedup.ConfigManager", lambda: SimpleNamespace(snapshot=lambda: raw))
    manager = ConceptDedupManager(tmp_path / "unused.db", graph=object())
    manager.runtime()
    assert manager.config.api_key_env == "HISTORY_TEST_KEY"
    assert manager.config.model == "typesafe/synthetic-jev"
    assert not (tmp_path / "unused.db").exists()


async def test_catalog_connection_drives_systemone_request(layered, monkeypatch):
    raw = deepcopy(layered)
    monkeypatch.setenv("JEV_TEST_KEY", "synthetic-secret")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"model": "synthetic-jev", "answers": {"pair_0": {
            "type": "score", "score": 2, "confidence": 1, "probabilities": {"0": 0, "1": 0, "2": 1}}}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("memory.concept_aligner.httpx.AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    await JevClient(AlignmentConfig.from_app_config(raw), usage_tracker=SimpleNamespace(
        record_usage=AsyncMock())).evaluate({"name": "synthetic"}, [{"name": "candidate"}])
    assert str(requests[0].url) == "https://example.test/v1/systemone"
    assert requests[0].headers["authorization"] == "Bearer synthetic-secret"
    assert json.loads(requests[0].content)["model"] == "synthetic-jev"


def test_configured_name_is_the_only_display_name_even_with_old_setting(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"agent_name": "stale-name"}), encoding="utf-8")
    monkeypatch.setattr("web.settings.configured_agent_name", lambda: "configured-name")
    settings = WebSettings(path)
    assert settings.get_public()["agent_name"] == "configured-name"
    settings.update(show_reasoning=True)
    assert "agent_name" not in json.loads(path.read_text(encoding="utf-8"))
    monkeypatch.setattr("web.settings.configured_agent_name", lambda: "")
    assert settings.agent_name == "Tentacle"
