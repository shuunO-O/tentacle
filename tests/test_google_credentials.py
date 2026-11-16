"""独立 Google 连接的身份加载，不访问真实凭据或网络。"""
from unittest.mock import Mock

import pytest

from config.google_auth import load_credentials
from dialects.gemini import GeminiClient


def test_explicit_identity_does_not_use_or_modify_adc(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_VERTEX_KEY", "/synthetic/new.json")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/synthetic/old.json")
    credentials = Mock()
    loader = Mock(return_value=(credentials, "ignored"))
    monkeypatch.setattr("google.auth.load_credentials_from_file", loader)
    monkeypatch.setattr("google.auth.default", Mock(side_effect=AssertionError("ADC used")))
    assert load_credentials("SYNTHETIC_VERTEX_KEY", "new-project") is credentials
    assert loader.call_args.args == ("/synthetic/new.json",)
    assert loader.call_args.kwargs["quota_project_id"] == "new-project"
    import os
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/synthetic/old.json"


def test_explicit_missing_or_invalid_identity_never_falls_back(monkeypatch):
    monkeypatch.delenv("SYNTHETIC_VERTEX_KEY", raising=False)
    with pytest.raises(ValueError, match="未设置"):
        load_credentials("SYNTHETIC_VERTEX_KEY")
    monkeypatch.setenv("SYNTHETIC_VERTEX_KEY", "/synthetic/new.json")
    monkeypatch.setattr("google.auth.load_credentials_from_file", Mock(side_effect=ValueError("secret")))
    with pytest.raises(ValueError) as exc:
        load_credentials("SYNTHETIC_VERTEX_KEY")
    assert "secret" not in str(exc.value)


async def test_vertex_client_uses_connection_identity(monkeypatch):
    credentials = Mock(valid=True)
    loader = Mock(return_value=credentials)
    monkeypatch.setattr("config.google_auth.load_credentials", loader)
    async with GeminiClient(auth_mode="adc", project="synthetic", credentials_env="SYNTHETIC_KEY") as client:
        assert str(client.base_url).endswith("/projects/synthetic/locations/global/")
    loader.assert_called_once_with("SYNTHETIC_KEY", "synthetic")


def test_provider_credential_paths_are_protected_before_loading(monkeypatch, tmp_path):
    from attachments.outgoing import open_outgoing_file
    from config.google_auth import protect_credentials
    path = tmp_path / 'arbitrary.json'
    path.write_text('synthetic')
    monkeypatch.setenv('SYNTHETIC_PROTECTED_KEY', str(path))
    protect_credentials({'providers': {'test': {'credentials_env': 'SYNTHETIC_PROTECTED_KEY'}}})
    with pytest.raises(PermissionError):
        open_outgoing_file(str(path))


def test_identity_changes_do_not_reuse_interactions_history():
    from dataclasses import replace
    from agent.handoff import compatible_history, model_identity
    from agent.protocol import ModelConfig
    cfg = ModelConfig('gemini-synthetic', 'https://test', provider='google', api_style='interactions',
                      credentials_env='SYNTHETIC_A')
    assert not compatible_history(model_identity(cfg), model_identity(replace(cfg, credentials_env='SYNTHETIC_B')))


def test_model_catalog_uses_provider_identity_and_quota_project(monkeypatch):
    from config.catalog import _adc_headers
    credentials = Mock()
    credentials.apply.side_effect = lambda h: h.update(authorization='Bearer synthetic')
    loader = Mock(return_value=credentials)
    monkeypatch.setattr('config.google_auth.load_credentials', loader)
    assert _adc_headers({'project': 'synthetic-project', 'credentials_env': 'SYNTHETIC_KEY'}) == {
        'x-goog-user-project': 'synthetic-project', 'authorization': 'Bearer synthetic'}
    loader.assert_called_once_with('SYNTHETIC_KEY', 'synthetic-project')
    credentials.refresh.assert_called_once()
