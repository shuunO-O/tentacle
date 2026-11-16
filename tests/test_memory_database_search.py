"""记忆检索结果语义回归；替换存储层，不连接 Neo4j、embedding 或读取私人数据。"""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tools.memory_database_search as search_mod
from runtime.tool_registry import ToolRegistry


@pytest.mark.parametrize("data, expected", [
    ({"primary_match": None, "secondary_matches": []}, "匹配:0"),
    ({"primary_match": {"node": {"id": "synthetic-event"}, "context": {}}, "secondary_matches": []},
     "匹配:1\n\n[1]synthetic-event"),
])
async def test_success_preserves_matches_and_empty_results(monkeypatch, data, expected):
    search = AsyncMock(return_value=data)
    monkeypatch.setattr(search_mod, "LadybugStore", lambda: SimpleNamespace(search=search))
    result = await search_mod.memory_database_search({"query_text": " synthetic query ", "target_label": "Person"})
    assert result == expected
    search.assert_awaited_once_with("synthetic query", "Person", None, None,
                                   date_from=None, date_to=None, event_limit=3)


@pytest.mark.parametrize("label", ["Person", "Concept"])
@pytest.mark.parametrize("score", [0.0, 0.91])
async def test_text_preserves_full_matches_and_relations(monkeypatch, label, score):
    nodes = [
        {
            "id": f"synthetic-{index}", "name": None if index == 0 else "",
            "summary": "示例摘要", "content": "完整正文\n包含引号\"和\\路径" * 100,
            "timestamp": "2026-10-22T18:00:00", "type": None,
            "score": score, "label": label, "confidence": score,
        }
        for index in range(3)
    ]
    context = {
        "persons": [{"name": "示例人物", "relation": "PARTICIPATED_IN"}],
        "locations": [],
        "concepts": [{"name": "示例概念", "relation": "MENTIONS"}],
        "attitudes": [{"name": "示例概念", "relation": "HAS_ATTITUDE", "sentiment": "喜欢"}],
    }
    original = {"primary_match": {"node": nodes[0], "context": context}, "secondary_matches": nodes[1:]}
    snapshot = copy.deepcopy(original)
    monkeypatch.setattr(search_mod, "LadybugStore",
                        lambda: SimpleNamespace(search=AsyncMock(return_value=original)))
    output = await search_mod.memory_database_search({"query_text": "synthetic query", "target_label": label})
    assert output.startswith("匹配:3\n\n[1]synthetic-0 | 2026-10-22T18:00:00 | score:")
    assert output.count("正文:" + nodes[0]["content"]) == 3
    assert output.count("摘要:示例摘要") == 3
    assert output.count(f"score:{score}") == 3
    assert "[2]synthetic-1 | 名称: |" in output  # 空字符串不按 null 丢弃。
    assert "人物:示例人物(PARTICIPATED_IN)" in output
    assert "概念:示例概念(MENTIONS)" in output
    assert "态度:示例概念(HAS_ATTITUDE,sentiment:喜欢)" in output
    assert "地点:" not in output
    assert "confidence" not in output and "关联:未查询" not in output
    assert original == snapshot
    assert len(output) < len(json.dumps({"status": "ok", "data": original}, ensure_ascii=False))


@pytest.mark.parametrize("label", ["Person", "Concept"])
async def test_entity_text_includes_each_matches_event_summaries(monkeypatch, label):
    def context(index):
        return {"events": [{
            "id": f"event-{index}", "timestamp": "2026-10-22T23:59:59.900000", "type": "日常",
            "summary": f"事件摘要{index}", "content": "THIS FULL EVENT BODY MUST NOT BE SHOWN",
            "relations": [{"relation": "PARTICIPATED_IN", "properties": {}},
                          {"relation": "HAS_ATTITUDE", "properties": {"sentiment": "喜欢"}}],
        }], "events_has_more": index == 1}

    data = {
        "primary_match": {"node": {"id": "entity-1", "name": "实体1", "score": 0.3}, "context": context(1)},
        "secondary_matches": [{"id": "entity-2", "name": "实体2", "score": 0.1, "context": context(2)}],
    }
    search = AsyncMock(return_value=data)
    monkeypatch.setattr(search_mod, "LadybugStore", lambda: SimpleNamespace(search=search))
    output = await search_mod.memory_database_search({
        "target_label": label, "date_from": "2026-10-01", "date_to": "2026-10-22", "event_limit": 1,
    })
    assert output.startswith("匹配:2\n\n[1]entity-1 | 名称:实体1 | score:0.3")
    assert "关联事件:1(还有更多)" in output
    assert "摘要:事件摘要1" in output and "摘要:事件摘要2" in output
    assert "event-1 | 2026-10-22T23:59:59.900000 | 日常 | PARTICIPATED_IN" in output
    assert 'HAS_ATTITUDE{"sentiment":"喜欢"}' in output
    assert "THIS FULL EVENT BODY" not in output
    assert "关联:未查询" not in output
    search.assert_awaited_once_with(None, label, None, None,
                                   date_from="2026-10-01", date_to="2026-10-22", event_limit=1)


@pytest.mark.parametrize("args", [
    {}, {"target_label": "Location", "query_text": "query"},
    {"time_hint": "2026"}, {"time_hint": "2026-02-30"},
    {"time_hint": "2026-10-22", "date_from": "2026-10-01"},
    {"date_from": "2026-10-23", "date_to": "2026-10-22"},
    {"date_to": "2026-1-1"}, {"date_to": "9999-12-31"},
    {"query_text": "query", "event_limit": 0}, {"query_text": "query", "event_limit": 11},
    {"query_text": "query", "event_limit": True}, {"query_text": "query", "event_limit": 1.5},
    {"query_text": ["query"]},
])
async def test_invalid_filters_fail_before_store_initialization(monkeypatch, args):
    def must_not_initialize():
        pytest.fail("invalid parameters must not initialize database or embedding clients")
    monkeypatch.setattr(search_mod, "LadybugStore", must_not_initialize)
    result = json.loads(await search_mod.memory_database_search(args))
    assert result["status"] == "error"
    assert result["error"]


def test_tool_schema_documents_date_ranges_and_event_limit():
    registry = ToolRegistry()
    search_mod.register_tools(registry)
    tool = registry.get_definitions()[0]["function"]
    props = tool["parameters"]["properties"]
    assert {"date_from", "date_to", "time_hint", "event_limit"} <= props.keys()
    assert props["event_limit"]["maximum"] == 10
    assert props["event_limit"]["default"] == 3


@pytest.mark.parametrize("error", [ConnectionError("synthetic database unavailable"),
                                 TimeoutError("synthetic embedding timeout")])
async def test_search_failure_reaches_registry_as_error(monkeypatch, error):
    monkeypatch.setattr(search_mod, "LadybugStore",
                        lambda: SimpleNamespace(search=AsyncMock(side_effect=error)))
    registry = ToolRegistry()
    registry.register("memory_database_search", "synthetic tool", {},
                      search_mod.memory_database_search, is_async=True)
    result = json.loads(await registry.execute("memory_database_search", {"query_text": "synthetic query", "target_label": "Person"}))
    assert result["status"] == "error"
    assert str(error) in result["error"]
    assert "不能据此判断没有相关记忆" in result["error"]
    assert "data" not in result


async def test_store_initialization_failure_is_error(monkeypatch):
    def fail():
        raise ValueError("synthetic missing configuration")

    monkeypatch.setattr(search_mod, "LadybugStore", fail)
    result = json.loads(await search_mod.memory_database_search({"entity_filter": "synthetic person"}))
    assert result["status"] == "error"
    assert "synthetic missing configuration" in result["error"]
    assert "data" not in result


async def test_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(search_mod, "LadybugStore",
                        lambda: SimpleNamespace(search=AsyncMock(side_effect=asyncio.CancelledError)))
    with pytest.raises(asyncio.CancelledError):
        await search_mod.memory_database_search({"query_text": "synthetic query", "target_label": "Person"})
