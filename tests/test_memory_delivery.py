"""检索交付改造：仅使用合成原文、配置与替身，不连接运行服务。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memory.evidence import SearchSettings, build_evidence, render_evidence
from memory.evidence_service import EvidenceService
from memory.ladybug_store import LadybugStore
from config.store import fields_for, validate_value
import tools.memory_database_search as search_mod


def fake_store(settings):
    events = [{"id": f"event_{i}", "summary": f"摘要{i}", "content": f"原文{i}",
               "content_length": len(f"原文{i}"), "source_hashes": [str(i)]} for i in range(40)]

    async def search(vector, filters, *, limit, excluded, **kwargs):
        return [e for e in events if e["id"] not in excluded][:limit]

    return SimpleNamespace(evidence_settings=lambda: settings, _evidence_generation=0,
                           evidence_model=lambda: ("synthetic", 2, 0),
                           evidence_vector=AsyncMock(return_value=([1.0, 0.0], ("synthetic", 2, 0))),
                           search_evidence=AsyncMock(side_effect=search),
                           evidence_links=AsyncMock(return_value=([], False)))


async def test_layered_results_index_not_seen_or_cursor_and_can_be_disabled():
    store = fake_store(SearchSettings())
    service = EvidenceService(store)
    result = await service.call(operation="search", owner="a", query="合成问题")
    assert "匹配:8" in result and "候选索引" in result and "event_31 |" in result
    assert len(service.histories["a"]["ids"]) == 8
    assert len(next(iter(service.cursors.values()))["ids"]) == 8
    second = await service.call(operation="search", owner="a", query="另一个问题")
    assert "event_8 |" in second and "read event_id=event_8" in second
    store.evidence_settings = lambda: SearchSettings(event_top_k=2, include_index=False)
    result = await service.call(operation="search", owner="b", query="合成问题")
    assert "匹配:2" in result and "候选索引" not in result
    assert store.search_evidence.await_args.kwargs["limit"] == 2


def test_matched_source_beyond_prefix_produces_exact_unicode_excerpt():
    text = "😀" * 33000 + "这里是命中的原文，包含任职时长三年九个月。"
    event = {"id": "event_1", "summary": "其他主题", "content": text[:32000],
             "content_length": len(text), "matched_source": {"start": 33000, "end": len(text), "text": text[33000:]}}
    data = build_evidence([event], [], ["event_1"], "任职时长", "initial")
    quote = data["events"][0]["quote"]
    assert quote["text"] == text[slice(*quote["range"])] and quote["range"][0] >= 33000
    assert "read event_id=event_1 limit=10000" in render_evidence(data)
    assert "matched_source" not in render_evidence(data)


async def test_full_mode_fetches_suffix_without_excerpt_truncation_or_quote_dedup():
    store = fake_store(SearchSettings(event_top_k=1, content_mode="full", include_index=False))
    text = "😀完整原文" * 8000
    store.search_evidence = AsyncMock(return_value=[{"id": "event_1", "summary": "摘要",
                                                     "content": text[:32000], "content_length": len(text)}])
    store.read_evidence = AsyncMock(return_value={"text": text, "total": len(text)})
    service = EvidenceService(store)
    result = await service.call(operation="search", owner="a", query="问题")
    assert text in result
    store.read_evidence.assert_awaited_once_with("event_1", 0, len(text))
    assert not service.histories["a"]["quotes"]


async def test_failed_full_read_does_not_commit_history():
    store = fake_store(SearchSettings(content_mode="full"))
    store.read_evidence = AsyncMock(side_effect=RuntimeError("synthetic failure"))
    service = EvidenceService(store)
    with pytest.raises(RuntimeError):
        await service.call(operation="search", owner="a", query="问题")
    assert not service.histories and not service.cursors


async def test_short_ids_bound_to_owner_and_expiry():
    store = fake_store(SearchSettings(event_top_k=1, include_index=False))
    store.read_evidence = AsyncMock(return_value={"kind": "source", "id": "event_0", "range": [0, 2],
                                                 "text": "原文", "total": 2})
    service = EvidenceService(store, clock=lambda: 0)
    await service.call(operation="search", owner="a", query="问题")
    await service.call(operation="read", owner="a", event_id="Event:0")
    store.read_evidence.assert_awaited_once_with("event_0", 0, 3)
    with pytest.raises(ValueError, match="完整"):
        await service.call(operation="read", owner="b", event_id="0")
    service.clock = lambda: 901
    with pytest.raises(ValueError, match="完整"):
        await service.call(operation="read", owner="a", event_id="0")


async def test_semantic_read_merges_unicode_overlaps_and_honors_budget_and_threshold():
    obj = object.__new__(LadybugStore)
    text = "😀甲乙丙丁戊己庚辛壬"
    obj.read_evidence = AsyncMock(return_value={"total": len(text)})
    obj.evidence_vector = AsyncMock(return_value=([1.0, 0.0], ("synthetic", 2, 0)))
    obj._evidence_query = AsyncMock(return_value=[
        {"start": 0, "end": 6, "text": text[:6], "score": 0.9},
        {"start": 4, "end": 10, "text": text[4:], "score": 0.8}])
    result = await obj.semantic_read("event_1", "问题", 4, 8)
    assert result["matches"] == [{"range": [0, 8], "text": text[:8], "score": 0.9}]
    assert not (await obj.semantic_read("event_1", "问题", min_score=0.95))["matches"]
    obj._evidence_query.return_value = []
    with pytest.raises(ValueError, match="兼容"):
        await obj.semantic_read("event_1", "问题")


async def test_semantic_read_accepts_limit_and_does_not_consume_history(monkeypatch):
    store = fake_store(SearchSettings())
    store.semantic_read = AsyncMock(return_value={"kind": "source_matches", "id": "event_1", "total": 9,
                                                "matches": [{"range": [0, 2], "text": "原文", "score": 0.9}]})
    monkeypatch.setattr(search_mod, "LadybugStore", lambda: store)
    result = await search_mod.memory_database_search({"operation": "read", "event_id": "event_1",
                                                     "query_text": "问题", "top_k": 2, "limit": 900})
    assert "原文语义定位" in result
    store.semantic_read.assert_awaited_once_with("event_1", "问题", 2, 900, None)
    assert not store._evidence_service.histories


@pytest.mark.parametrize("extra", [{"top_k": True}, {"top_k": 5}, {"min_score": float("nan")},
                                    {"min_score": True}, {"min_score": 1.1}, {"start": 0}, {"limit": 10001}])
async def test_invalid_semantic_read_rejected_before_storage(monkeypatch, extra):
    import json
    monkeypatch.setattr(search_mod, "LadybugStore", lambda: pytest.fail("invalid arguments initialized storage"))
    result = await search_mod.memory_database_search({"operation": "read", "event_id": "event_1",
                                                     "query_text": "问题", **extra})
    assert json.loads(result)["status"] == "error"


async def test_fusion_keeps_source_hit_even_when_summary_route_contains_event():
    obj = object.__new__(LadybugStore)
    source = {"start": 0, "end": 2, "text": "原文"}
    event = {"id": "event_1", "summary": "摘要", "content": "原文", "content_length": 2}
    obj.evidence_model = lambda: ("synthetic", 2, 0)
    obj._evidence_query = AsyncMock(side_effect=[[dict(event)], [{**event, "matched_source": source}]])
    found = await obj.search_evidence([1.0, 0.0], {})
    assert found[0]["matched_source"] == source


@pytest.mark.parametrize("raw", [{"event_top_k": True}, {"event_top_k": 21}, {"index_top_k": -1},
                                     {"include_index": 1}, {"content_mode": "unknown"}])
def test_invalid_delivery_configuration_is_rejected(raw):
    with pytest.raises(ValueError):
        SearchSettings.from_config(raw)


def test_delivery_options_exposed_and_validated_in_settings():
    fields = fields_for({"providers": {}, "models": {}, "roles": {}})
    for key, value in (("event_top_k", 12), ("content_mode", "full"), ("include_index", False), ("index_top_k", 0)):
        validate_value(fields["memory.search." + key], value)



@pytest.mark.parametrize("mode", ["excerpt", "full"])
def test_event_source_metadata_in_detail_and_index_and_legacy_omission(mode):
    event = {"id": "event_1", "summary": "摘要", "content": "原文", "content_length": 2,
             "create_time": "2026-11-02T10:00:00", "raw_session": "source-session"}
    data = build_evidence([event], [], ["event_1"], "", "initial", related=[event], content_mode=mode)
    for row in (data["events"][0], data["related"][0]):
        assert row["create_time"] == event["create_time"]
        assert row["raw_session"] == event["raw_session"]
    rendered = render_evidence(data)
    assert rendered.count('create_time(保存时间)="2026-11-02T10:00:00"') == 2
    assert rendered.count('raw_session="source-session"') == 2
    legacy = {**event, "create_time": None, "raw_session": None}
    rendered = render_evidence(build_evidence([legacy], [], ["event_1"], "", "initial", related=[legacy]))
    assert "create_time" not in rendered and "raw_session" not in rendered
