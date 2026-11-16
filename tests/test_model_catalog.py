import asyncio
import json
import re

import httpx
import pytest

from config.catalog import fetch_catalog, import_candidates
from config.providers import ConfigurationError


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("CATALOG_TEST_KEY", "synthetic-secret")
    return {"type": "openai_compatible", "base_url": "https://example.test/v1", "api_key_env": "CATALOG_TEST_KEY", "proxy": ""}


@pytest.mark.parametrize("kind", ["openai_compatible", "openai", "deepseek"])
async def test_compatible_discovery_paginates_without_changing_origin(provider, kind):
    provider["type"] = kind
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer synthetic-secret"
        if "after" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "vendor/model.A"}], "has_more": True})
        return httpx.Response(200, json={"data": [{"id": "vendor/model.A"}, {"id": "second-model"}, {"id": "bad\nname"}]})

    result = await fetch_catalog(provider, transport=httpx.MockTransport(respond))
    assert [m["name"] for m in result["models"]] == ["second-model", "vendor/model.A"]
    assert calls[1].url.params["after"] == "vendor/model.A"
    assert not result["truncated"]
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.parametrize("kind,prefix,list_key", [
    ("google_developer", "models/", "models"),
    ("google_vertex", "publishers/google/models/", "publisherModels"),
])
async def test_google_discovery_uses_native_paths_and_pagination(provider, monkeypatch, kind, prefix, list_key):
    provider.update(type=kind, base_url="https://google.example.test/v1beta1")
    monkeypatch.setattr('config.catalog._adc_headers', lambda p: {"Authorization": "Bearer synthetic-adc"})
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.path == "/v1beta1/" + ("publishers/google/models" if kind == "google_vertex" else "models")
        if kind == "google_vertex":
            assert request.headers["authorization"] == "Bearer synthetic-adc"
        else:
            assert request.headers["x-goog-api-key"] == "synthetic-secret"
            assert "key" not in request.url.params
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={list_key: [{"name": prefix + "sample", "displayName": "Sample", "inputTokenLimit": 32000}], "nextPageToken": "second"})
        return httpx.Response(200, json={list_key: []})

    result = await fetch_catalog(provider, transport=httpx.MockTransport(respond))
    assert result["models"] == [{"name": "sample", "label": "Sample", "context_window": 32000}]
    assert calls[1].url.params["pageToken"] == "second"


@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
async def test_directory_errors_are_sanitized_and_redirects_are_not_followed(provider, status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://other.test/steal"}, text="secret synthetic-secret")

    with pytest.raises(ConfigurationError) as caught:
        await fetch_catalog(provider, transport=httpx.MockTransport(respond))
    assert "synthetic-secret" not in str(caught.value)
    assert len(calls) == 1


async def test_missing_credentials_do_not_send_requests(provider, monkeypatch):
    monkeypatch.delenv("CATALOG_TEST_KEY")

    def unexpected(request):
        pytest.fail("missing credentials must fail before sending a request")

    with pytest.raises(ConfigurationError, match="环境变量"):
        await fetch_catalog(provider, transport=httpx.MockTransport(unexpected))


async def test_repeated_pagination_is_bounded_and_marks_partial_result(provider):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"data": [{"id": "same"}], "has_more": True})

    result = await fetch_catalog(provider, transport=httpx.MockTransport(respond))
    assert count == 2 and result["truncated"]


@pytest.mark.parametrize("payload", [[], {"other": []}])
async def test_malformed_directory_is_rejected(provider, payload):
    with pytest.raises(ConfigurationError, match="格式"):
        await fetch_catalog(provider, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))


async def test_oversized_directory_is_rejected(provider, monkeypatch):
    monkeypatch.setattr('config.catalog.MAX_BYTES', 16)
    with pytest.raises(ConfigurationError, match="过大"):
        await fetch_catalog(provider, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 17)))


async def test_directory_timeout_is_sanitized(provider):
    async def respond(request):
        raise httpx.ReadTimeout("synthetic-secret")

    with pytest.raises(ConfigurationError, match="超时") as caught:
        await fetch_catalog(provider, transport=httpx.MockTransport(respond))
    assert "synthetic-secret" not in str(caught.value)


async def test_custom_model_directory_uses_exact_path_and_keeps_query_when_paging(provider):
    provider["models_url"] = "https://catalog.example.test/custom/list?api-version=2026-01-01&kind=text&kind=chat"
    calls = []

    def respond(request):
        calls.append(request)
        assert str(request.url.host) == "catalog.example.test"
        assert request.url.path == "/custom/list"
        assert request.url.params["api-version"] == "2026-01-01"
        assert request.url.params.get_list("kind") == ["text", "chat"]
        assert request.headers["authorization"] == "Bearer synthetic-secret"
        return httpx.Response(200, json={"data": [{"id": "model-one"}], "has_more": len(calls) == 1})

    result = await fetch_catalog(provider, transport=httpx.MockTransport(respond))
    assert len(calls) == 2 and calls[1].url.params["after"] == "model-one"
    assert result["models"][0]["name"] == "model-one"


async def test_result_limit_reports_partial_directory(provider, monkeypatch):
    monkeypatch.setattr('config.catalog.MAX_MODELS', 2)
    response = {"data": [{"id": "first"}, {"id": "second"}, {"id": "third"}]}
    result = await fetch_catalog(provider, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response)))
    assert len(result["models"]) == 2 and result["truncated"]


def test_import_candidates_skip_existing_models_and_generate_valid_ids():
    raw = {"providers": {"demo": {"type": "deepseek"}}, "models": {"original": {"provider": "demo", "name": "vendor/long.model@name"}}}
    result = import_candidates(raw, "demo", {"models": [{"name": "vendor/long.model@name", "label": "A"}, {"name": "other/模型", "label": "B"}], "truncated": False})
    assert result["models"][0]["configured"]
    assert result["models"][0]["import_id"] == "original"
    assert not result["models"][1]["configured"]
    assert result["models"][1]["import_id"] == "demo/other"
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9-]{0,63}(/[A-Za-z_0-9-]{1,64})?", m["import_id"]) for m in result["models"])
    assert len({m["import_id"] for m in result["models"]}) == 2


async def test_cancelled_request_propagates(provider):
    async def respond(request):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await fetch_catalog(provider, transport=httpx.MockTransport(respond))
