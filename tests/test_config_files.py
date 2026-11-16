"""只使用临时合成配置验证默认文件、用户覆盖与配置事务。"""

from copy import deepcopy

import pytest
import yaml

from config.files import ConfigFiles, merge_config, parse_config
from config.store import ConfigurationConflict, ConfigurationError, ConfigStore
from config.manager import ConfigManager


DEFAULTS = """config_version: 2
providers:
  local:
    type: deepseek
    api_key_env: SYNTHETIC_KEY
models:
  fast:
    provider: local
    name: synthetic-chat
    defaults:
      temperature: 0.3
  unused:
    provider: local
    name: unused
roles:
  chat:
    model: fast
    tools: {enabled: [get_time]}
heartbeat:
  interval_minutes: 60
  session_switch: {enabled: true, window_start: '01:00', window_end: '07:00'}
webui: {host: '127.0.0.1', port: 8420}
"""


@pytest.fixture
def layered_files(tmp_path):
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text(DEFAULTS, encoding="utf-8")
    return defaults, ConfigStore(tmp_path / "config.yaml")


def test_deep_overrides_and_replacements_do_not_mutate_inputs():
    defaults = {"a": {"nested": {"x": 1, "y": 2}, "list": [1, 2], "optional": "value"}}
    user = {"a": {"nested": {"x": 3}, "list": [], "optional": None}}
    original = deepcopy(defaults)
    result = merge_config(defaults, user)
    assert result == {"a": {"nested": {"x": 3, "y": 2}, "list": [], "optional": None}}
    result["a"]["nested"]["x"] = 9
    assert defaults == original and user["a"]["nested"]["x"] == 3


@pytest.mark.parametrize("text", ["", "# only a comment\n", "{}\n"])
def test_missing_or_empty_user_file_uses_defaults(layered_files, text):
    defaults, store = layered_files
    assert store.read()[2] == parse_config(DEFAULTS)
    store.path.write_text(text, encoding="utf-8")
    assert store.read()[2] == parse_config(DEFAULTS)
    assert defaults.read_text(encoding="utf-8") == DEFAULTS


def test_first_save_creates_only_user_override_and_restore_inherits_defaults(layered_files):
    defaults, store = layered_files
    revision = store.read()[0]
    assert store.preview({"heartbeat.interval_minutes": 20}, revision)["changes"]
    assert not store.path.exists() and not store.backup_dir.exists()
    result = store.save({"heartbeat.interval_minutes": 20}, revision)
    assert parse_config(store.path.read_text()) == {"heartbeat": {"interval_minutes": 20}}
    assert store.read()[2]["heartbeat"]["session_switch"]["enabled"] is True
    assert defaults.read_text() == DEFAULTS
    assert (store.backup_dir / result["backup"]).read_bytes() == b""
    store.restore(result["backup"], result["revision"], apply=True)
    assert store.read()[2] == parse_config(DEFAULTS)


def test_comments_and_unknown_user_settings_survive(layered_files):
    _, store = layered_files
    store.path.write_text("# local settings\ncustom: {keep: true}\nheartbeat: {interval_minutes: 40}\n")
    store.save({"webui.port": 8500}, store.read()[0])
    assert "# local settings" in store.path.read_text()
    assert store.read()[2]["custom"] == {"keep": True}
    assert store.read()[2]["webui"] == {"host": "127.0.0.1", "port": 8500}


def test_default_update_invalidates_preview_and_preserves_user_values(layered_files):
    defaults, store = layered_files
    store.path.write_text("heartbeat: {interval_minutes: 20}\n")
    revision = store.read()[0]
    defaults.write_text(DEFAULTS.replace("port: 8420", "port: 8500"))
    with pytest.raises(ConfigurationConflict):
        store.save({"heartbeat.interval_minutes": 30}, revision)
    raw = store.read()[2]
    assert raw["heartbeat"]["interval_minutes"] == 20 and raw["webui"]["port"] == 8500
    assert store.path.read_text() == "heartbeat: {interval_minutes: 20}\n"


def test_config_manager_and_store_use_same_effective_config(layered_files):
    _, store = layered_files
    store.path.write_text("webui: {port: 8500}\n")
    manager = object.__new__(ConfigManager)
    manager._config_path, manager._raw_config = str(store.path), None
    assert manager.snapshot() == store.read()[2]


def test_first_model_edit_pins_catalog_without_pinning_runtime_defaults(layered_files):
    _, store = layered_files
    store.save({"models.fast.name": "another-synthetic-chat"}, store.read()[0])
    user = parse_config(store.path.read_text())
    assert user["config_version"] == 2
    assert user["models"]["fast"]["name"] == "another-synthetic-chat"
    assert "unused" in user["models"] and "heartbeat" not in user
    assert store.read()[2]["heartbeat"]["interval_minutes"] == 60


def test_user_catalog_replaces_default_model_bundle(layered_files):
    _, store = layered_files
    user = {"config_version": 2, "providers": {"local": {"type": "deepseek", "api_key_env": "SYNTHETIC_KEY"}},
            "models": {"fast": {"provider": "local", "name": "synthetic"}},
            "roles": {"chat": {"model": "fast"}}}
    store.path.write_text(yaml.safe_dump(user))
    assert store.read()[2]["models"] == user["models"]
    assert store.read()[2]["roles"] == user["roles"]
    assert store.read()[2]["webui"]["port"] == 8420
    store.validate(store.read()[2], [])


def test_public_field_source_identifies_user_overrides(layered_files):
    _, store = layered_files
    store.path.write_text("webui: {port: 8500}\n")
    fields = {field["path"]: field for field in store.public()["fields"]}
    assert fields["webui.port"]["source"] == "file"
    assert fields["webui.host"]["source"] == "default"


@pytest.mark.parametrize("text", ["null", "[]", "models: {}\nmodels: {}", "models: &m {}\nother: *m", "models: ["])
def test_invalid_defaults_never_fall_back_to_user_only(layered_files, text):
    defaults, store = layered_files
    store.path.write_text("models: {}\n")
    defaults.write_text(text)
    with pytest.raises(ConfigurationError):
        store.read()


def test_default_file_cannot_be_selected_as_writable_user_config(layered_files):
    defaults, _ = layered_files
    with pytest.raises(ConfigurationError, match="只读"):
        ConfigFiles(defaults)


def test_legacy_flat_model_config_is_rejected(layered_files):
    defaults, store = layered_files
    defaults.write_text("models:\n  chat:\n    name: old-flat-model\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        store.public()


def test_inherited_catalog_deletion_restore_and_role_inheritance(layered_files):
    defaults, store = layered_files
    raw = parse_config(DEFAULTS)
    raw["roles"]["chat"]["overrides"] = {"temperature": 0.8}
    defaults.write_text(yaml.safe_dump(raw))
    fields = {field["path"]: field for field in store.public()["fields"]}
    # source=default 在角色覆盖字段上表示继承模型，不能把默认文件中的显式覆盖误判为继承。
    assert fields["roles.chat.overrides.temperature"]["source"] == "file"
    saved = store.structure([{"op": "delete", "kind": "models", "id": "unused"}], store.read()[0], apply=True)
    assert "unused" not in store.read()[2]["models"]
    store.restore(saved["backup"], store.read()[0], apply=True)
    assert "unused" in store.read()[2]["models"]
    store.save({"roles.chat.overrides.temperature": {"$inherit": True}}, store.read()[0])
    assert "temperature" not in store.read()[2]["roles"]["chat"]["overrides"]


def test_restore_with_user_flow_mapping(layered_files):
    defaults, store = layered_files
    raw = {"config_version": 2, "providers": {"local": {"type": "deepseek", "api_key_env": "SYNTHETIC_KEY"}},
           "models": {"fast": {"provider": "local", "name": "synthetic"}}, "roles": {"chat": {"model": "fast"}}}
    import json
    store.path.write_text(json.dumps(raw))
    result = store.save({"models.fast.name": "changed"}, store.read()[0])
    store.restore(result["backup"], store.read()[0], apply=True)
    assert store.read()[2]["models"]["fast"]["name"] == "synthetic"
