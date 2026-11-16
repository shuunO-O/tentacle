"""公开网页工具离线验收：真实 HTTP 编码、错误边界与方言路由。"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

import agent.web_sources as web_sources
from dialects import get_dialect
from agent.protocol import Context, ModelConfig
from runtime.tool_registry import registry


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(web_sources, "_config", lambda: {"api_key_env": "PARALELL_API_KEY"})
    monkeypatch.setenv("PARALELL_API_KEY", "synthetic-key")
    original = httpx.AsyncClient
    requests = []

    def install(handler):
        def handle(request):
            requests.append(request)
            return handler(request)

        def client(**kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            return original(**kwargs, transport=httpx.MockTransport(handle))

        monkeypatch.setattr(web_sources.httpx, "AsyncClient", client)
    return install, requests


async def test_registered_tools_search_and_fetch(offline, caplog):
    caplog.set_level("INFO", logger="agent.web_sources")
    saved = dict(registry._tools)
    try:
        from tools import web_fetch, web_search
        web_search.register_tools(registry)
        web_fetch.register_tools(registry)
        assert registry.has("web_fetch") and not registry.has("fetch")
        install, requests = offline
        install(lambda request: httpx.Response(200, json={"results": [{
            "url": "https://example.org/docs", "title": "Docs", "publish_date": "2026-10-15",
            "excerpts": ["relevant excerpt"], "full_content": "# Full document"}],
            "errors": [{"url": "https://example.org/missing", "error": "not found"}],
            "search_id": "synthetic-search-id", "usage": [{"name": "sku_search", "count": 1}]}))
        result = json.loads(await registry.execute("web_search", {
            "queries": ["example docs"], "objective": "Find official docs"}))
        assert result["results"][0]["url"] == "https://example.org/docs"
        assert "usage" not in result and "provider" not in result and "search_id" not in result
        assert "sku_search" in caplog.text and "synthetic-search-id" in caplog.text
        assert result["results"][0]["excerpts"] == ["relevant excerpt"]
        assert "full_content" not in result["results"][0]
        assert result["errors"][0]["error"] == "not found"
        result = json.loads(await registry.execute("web_fetch", {"urls": ["https://example.org/docs"]}))
        assert result["results"][0]["full_content"] == "# Full document"
        assert "excerpts" not in result["results"][0]
        search, extract = requests
        assert search.headers["x-api-key"] == "synthetic-key"
        assert search.url == "https://api.parallel.ai/v1/search"
        assert json.loads(search.content) == {
            "search_queries": ["example docs"], "objective": "Find official docs", "mode": "fast",
            "max_chars_total": 25000,
            "advanced_settings": {"max_results": 5, "excerpt_settings": {"max_chars_per_result": 5000}}}
        assert extract.url == "https://api.parallel.ai/v1/extract"
        body = json.loads(extract.content)
        assert "full_content" not in body
        assert body["advanced_settings"]["full_content"] == {"max_chars_per_result": 25000}
        assert all(t["type"] == "function" for t in registry.get_definitions(["web_search", "web_fetch"]))
    finally:
        registry._tools.clear()
        registry._tools.update(saved)


@pytest.mark.parametrize("status", [301, 401, 403, 422, 429, 500])
async def test_http_error_no_body_leak_or_retry(offline, status):
    install, requests = offline
    install(lambda request: httpx.Response(status, text="synthetic-key private request text",
                                          headers={"location": "https://other.example"}))
    raw = await web_sources.execute_web_tool("search", {"queries": ["public docs"]})
    assert json.loads(raw)["status"] == status
    assert "synthetic-key" not in raw and "private request" not in raw
    assert len(requests) == 1


@pytest.mark.parametrize("operation,args", [
    ("search", {"queries": []}), ("search", {"queries": [123]}),
    ("search", {"queries": ["x"], "max_results": True}),
    ("search", {"queries": ["x"], "mode": "invalid"}),
    ("extract", {"urls": ["file:///etc/passwd"]}),
    ("extract", {"urls": ["https://user:password@example.org"]}),
    ("extract", {"urls": ["https://example.org"], "full_content": "false"}),
])
async def test_invalid_input_never_requests(offline, operation, args):
    install, requests = offline
    install(lambda request: pytest.fail("invalid input reached network"))
    assert "error" in json.loads(await web_sources.execute_web_tool(operation, args))
    assert not requests


async def test_missing_key_and_unknown_provider(offline, monkeypatch):
    install, requests = offline
    install(lambda request: pytest.fail("missing key reached network"))
    monkeypatch.delenv("PARALELL_API_KEY")
    assert "PARALELL_API_KEY" in await web_sources.execute_web_tool("search", {"queries": ["x"]})
    monkeypatch.setattr(web_sources, "_config", lambda: {"provider": "unknown"})
    assert "provider" in await web_sources.execute_web_tool("search", {"queries": ["x"]})
    assert not requests


@pytest.mark.parametrize(
    "response",
    [b"not json", b"[]", b'{"results":[{"url":"x","excerpts":null}]}', b"x" * (2 * 1024 * 1024 + 1)],
    ids=["not_json", "array", "null_excerpts", "oversized"],
)
async def test_invalid_or_oversized_response(offline, response):
    install, _ = offline
    install(lambda request: httpx.Response(200, content=response))
    assert "error" in json.loads(await web_sources.execute_web_tool("search", {"queries": ["x"]}))


async def test_timeout_and_cancellation(offline, monkeypatch):
    install, _ = offline
    install(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic-key")))
    assert "超时" in await web_sources.execute_web_tool("search", {"queries": ["x"]})
    install(lambda request: (_ for _ in ()).throw(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await web_sources.execute_web_tool("search", {"queries": ["x"]})


def test_output_limit_preserves_json_and_url():
    data = {"results": [{"url": "https://example.org/docs", "title": "Docs", "excerpts": ["摘录" * 20000],
                         "full_content": "正文" * 50000}], "warnings": None}
    raw = web_sources._bounded_result(data, 4000, full_content=True)
    result = json.loads(raw)
    assert len(raw) <= 4000 and result["truncated"]
    assert result["results"][0]["url"] == "https://example.org/docs"


@pytest.mark.parametrize("full_content", [True, False])
async def test_fetch_selects_one_content_representation(offline, full_content):
    install, requests = offline
    install(lambda request: httpx.Response(200, json={"results": [{
        "url": "https://example.org/docs", "publish_date": "2026-10-16",
        "full_content": "Full document", "excerpts": ["Relevant excerpt"],
    }], "warnings": ["Partial page"], "errors": [{"url": "https://example.org/missing", "error": "not found"}]}))
    raw = await web_sources.execute_web_tool("extract", {
        "urls": ["https://example.org/docs"], "full_content": full_content,
    })
    result = json.loads(raw)
    entry = result["results"][0]
    assert ("full_content" in entry) is full_content
    assert ("excerpts" in entry) is not full_content
    assert entry["publish_date"] == "2026-10-16"
    assert result["warnings"] == ["Partial page"]
    assert result["errors"][0]["error"] == "not found"
    assert len(requests) == 1


def test_fetch_falls_back_to_excerpts_and_preserves_upstream_truncation():
    data = {"results": [{"url": "https://example.org/docs", "full_content": "", "excerpts": ["Fallback"]}],
            "warnings": None, "errors": [], "truncated": True}
    result = json.loads(web_sources._bounded_result(data, 4000, full_content=True))
    assert result == {"results": [{"url": "https://example.org/docs", "excerpts": ["Fallback"]}], "truncated": True}


def test_deduplication_avoids_unnecessary_truncation():
    content = "synthetic text " * 100
    data = {"results": [{"url": "https://example.org/docs", "full_content": content, "excerpts": [content]}]}
    result = json.loads(web_sources._bounded_result(data, 2000, full_content=True))
    assert result["results"][0]["full_content"] == content
    assert "truncated" not in result


@pytest.mark.parametrize("style", ["interactions", "gemini_generate", "responses"])
async def test_local_web_search_prevents_native_injection(style):
    tools = [{"type": "function", "function": {"name": "web_search", "description": "Search",
                                               "parameters": {"type": "object", "properties": {}}}}]
    cfg = ModelConfig("test", "https://example.test", "fake", api_style=style, provider="google",
                      stream=False, tool_names=["web_search"])
    context = Context("", [], "", "test")
    dialect = get_dialect(style)
    messages = [{"role": "user", "content": "Find docs"}]
    if style == "responses":
        client = AsyncMock()
        client.responses.create.return_value = type("Response", (), {"output": [], "usage": None})()
        await dialect.call(client, cfg, messages, tools, context, AsyncMock())
        assert client.responses.create.call_args.kwargs["tools"] == [{
            "type": "function", **tools[0]["function"]}]
    else:
        body = await dialect._request(cfg, messages, tools, context)
        if style == "interactions":
            assert body["tools"] == [{"type": "function", **tools[0]["function"]}]
        else:
            assert body["tools"] == [{"functionDeclarations": [tools[0]["function"]]}]


async def test_tavily_search_and_extract(offline, monkeypatch):
    monkeypatch.setattr(web_sources, "_config", lambda: {"provider": "tavily"})
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-synthetic")
    install, requests = offline

    def handler(request):
        if request.url == "https://api.tavily.com/search":
            q = json.loads(request.content)["query"]
            return httpx.Response(200, json={"results": [
                {"url": "https://example.org/shared", "title": "Shared", "content": f"excerpt {q}",
                 "published_date": "2026-11-01"},
                {"url": f"https://example.org/{q}", "title": q.upper(), "content": "detail"},
            ]})
        return httpx.Response(200, json={
            "results": [{"url": "https://example.org/docs", "raw_content": "# Markdown body"}],
            "failed_results": [{"url": "https://example.org/fail", "error": "blocked"}],
        })

    install(handler)
    search_res = json.loads(await web_sources.execute_web_tool("search", {
        "queries": ["alpha", "beta"], "mode": "advanced", "max_results": 3,
    }))
    assert [r["url"] for r in search_res["results"]] == [
        "https://example.org/shared", "https://example.org/alpha", "https://example.org/beta",
    ]
    assert search_res["results"][0]["publish_date"] == "2026-11-01"
    assert search_res["results"][0]["excerpts"] == ["excerpt alpha"]
    extract_res = json.loads(await web_sources.execute_web_tool("extract", {
        "urls": ["https://example.org/docs", "https://example.org/fail"], "full_content": True,
    }))
    assert extract_res["results"] == [{"url": "https://example.org/docs", "full_content": "# Markdown body"}]
    assert extract_res["errors"] == [{"url": "https://example.org/fail", "error": "blocked"}]
    assert requests[0].headers["authorization"] == "Bearer tvly-synthetic"
    assert json.loads(requests[0].content)["search_depth"] == "advanced"
    assert requests[-1].url == "https://api.tavily.com/extract"


async def test_deepseek_search_and_fetch(offline, monkeypatch):
    monkeypatch.setattr(web_sources, "_config", lambda: {"provider": "deepseek", "api_key_env": ""})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-synthetic")
    install, requests = offline

    def handler(request):
        if request.url == "https://api.deepseek.com/v1/tools/search":
            return httpx.Response(200, json={"results": [{
                "url": "https://example.org/ds", "title": "DS Docs", "date": "2026-11-10T00:00:00Z",
                "snippet": "short", "summary": "detailed summary",
            }]})
        url = json.loads(request.content)["url"]
        if url.endswith("/bad"):
            return httpx.Response(403, text="forbidden secret")
        return httpx.Response(200, json={"title": "Fetched Page", "content": "# Full MD"})

    install(handler)
    search_res = json.loads(await web_sources.execute_web_tool("search", {
        "queries": ["deepseek tools"], "objective": "find api", "max_results": 4,
    }))
    assert search_res["results"] == [{
        "url": "https://example.org/ds", "title": "DS Docs",
        "publish_date": "2026-11-10T00:00:00Z", "excerpts": ["detailed summary"],
    }]
    fetch_res = json.loads(await web_sources.execute_web_tool("extract", {
        "urls": ["https://example.org/ok", "https://example.org/bad"], "full_content": False,
    }))
    assert fetch_res["results"] == [{
        "url": "https://example.org/ok", "title": "Fetched Page", "excerpts": ["# Full MD"],
    }]
    assert len(fetch_res["errors"]) == 1 and fetch_res["errors"][0]["url"] == "https://example.org/bad"
    assert requests[0].headers["authorization"] == "Bearer ds-synthetic"
    assert json.loads(requests[0].content) == {
        "query": "deepseek tools", "count": 4, "need_content": False, "ori_question": "find api",
    }
