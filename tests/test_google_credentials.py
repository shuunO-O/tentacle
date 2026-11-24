"""Google 服务账号 JSON 凭据加载与 httpx 令牌刷新，不访问真实凭据或网络。"""
import os
from unittest.mock import Mock

import httpx
import pytest

from config.google_auth import SCOPES, load_credentials, make_refresh_request
from dialects.gemini import GeminiClient


def test_explicit_identity_uses_service_account_json_without_modifying_default_env(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_VERTEX_KEY", "/synthetic/new.json")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/synthetic/old.json")
    credentials = Mock()
    loader = Mock(return_value=credentials)
    monkeypatch.setattr("google.oauth2.service_account.Credentials.from_service_account_file", loader)
    monkeypatch.setattr("google.auth.default", Mock(side_effect=AssertionError("implicit ADC must not be used")))
    assert load_credentials("SYNTHETIC_VERTEX_KEY", "new-project") is credentials
    loader.assert_called_once_with("/synthetic/new.json", scopes=SCOPES, quota_project_id="new-project")
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/synthetic/old.json"


def test_default_identity_uses_google_application_credentials_json_never_implicit_adc(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/synthetic/default.json")
    credentials = Mock()
    loader = Mock(return_value=credentials)
    monkeypatch.setattr("google.oauth2.service_account.Credentials.from_service_account_file", loader)
    monkeypatch.setattr("google.auth.default", Mock(side_effect=AssertionError("implicit ADC must not be used")))
    assert load_credentials(project="default-project") is credentials
    loader.assert_called_once_with("/synthetic/default.json", scopes=SCOPES, quota_project_id="default-project")


def test_missing_or_invalid_identity_never_falls_back_or_echoes_secret(monkeypatch):
    monkeypatch.delenv("SYNTHETIC_VERTEX_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr("google.auth.default", Mock(side_effect=AssertionError("implicit ADC must not be used")))
    with pytest.raises(ValueError, match="未设置"):
        load_credentials()
    with pytest.raises(ValueError, match="未设置"):
        load_credentials("SYNTHETIC_VERTEX_KEY")
    monkeypatch.setenv("SYNTHETIC_VERTEX_KEY", "/synthetic/new.json")
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        Mock(side_effect=ValueError("secret-private-key")),
    )
    with pytest.raises(ValueError) as exc:
        load_credentials("SYNTHETIC_VERTEX_KEY")
    assert "secret-private-key" not in str(exc.value)


def test_httpx_refresh_request_maps_response_and_wraps_transport_errors():
    from google.auth.exceptions import TransportError

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url), req.content, dict(req.headers)))
        if req.url.path == "/fail":
            raise httpx.ConnectError("network down", request=req)
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    with make_refresh_request(proxy="", timeout=15.0, transport=httpx.MockTransport(handler)) as refresh_req:
        resp = refresh_req("https://oauth2.example/token", method="POST", body=b"grant_type=urn", headers={"x-test": "1"})
        assert resp.status == 200
        assert resp.headers["content-type"] == "application/json"
        assert b"access_token" in resp.data
        assert seen[0][:3] == ("POST", "https://oauth2.example/token", b"grant_type=urn")
        assert seen[0][3]["x-test"] == "1"

        with pytest.raises(TransportError):
            refresh_req("https://oauth2.example/fail", method="POST", body=b"x")


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
