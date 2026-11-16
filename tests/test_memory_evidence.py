"""宿主会话证据交付：纯合成事件、假存储与受控时钟，无运行数据。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.loop import current_tool_context
from memory.evidence_service import EvidenceService
from memory.evidence import SearchSettings
from agent.protocol import Context
import tools.memory_database_search as search_mod


def rows(count=30):
    return [{"id": f"event_{i}", "summary": f"合成摘要{i}", "content": "共同的原文摘录",
             "content_length": 8, "source_hashes": ["synthetic"]} for i in range(count)]


def fake_store():
    events = rows()

    async def search(vector, filters, *, excluded=(), limit=4, **kwargs):
        return [e for e in events if e["id"] not in excluded][:limit]

    async def links(ids, vector):
        return ([{"event_id": node_id, "concept_id": "concept_1", "name": "合成概念", "degree": 30,
                  "score": 0.9} for node_id in ids], False)

    store = SimpleNamespace(_evidence_generation=0, events=events)
    store.evidence_settings = lambda: SearchSettings(event_top_k=4, include_index=False)
    store.evidence_model = lambda: ("synthetic-model", 2, store._evidence_generation)
    store.evidence_vector = AsyncMock(side_effect=lambda _: ([1.0, 0.0], store.evidence_model()))
    store.search_evidence = AsyncMock(side_effect=search)
    store.evidence_links = AsyncMock(side_effect=links)
    store.read_evidence = AsyncMock(return_value={"kind": "source", "id": "event_0", "range": [2, 6],
                                                "text": "😀合成文", "total": 9, "next": 6})
    return store


def cursor(service):
    return next(reversed(service.cursors))


async def test_search_dedup_owner_isolation_reset_and_shared_quote_references():
    store = fake_store()
    service = EvidenceService(store)
    first = await service.call(operation="search", owner="session-a", query="暗号")
    old_cursor = cursor(service)
    assert "匹配:4" in first and "原文同 event_0" in first
    assert list(service.histories["session-a"]["ids"]) == [f"event_{i}" for i in range(4)]
    second = await service.call(operation="search", owner="session-a", query="暗号")
    assert "event_4 |" in second and "event_0 |" not in second
    await service.call(operation="search", owner="session-b", query="暗号")
    assert list(service.histories["session-b"]["ids"]) == [f"event_{i}" for i in range(4)]
    await service.call(operation="search", owner="session-a", query="新问题", reset=True)
    assert old_cursor not in service.cursors
    assert list(service.histories["session-a"]["ids"]) == [f"event_{i}" for i in range(4)]
    assert not service._locks


async def test_expand_reuses_query_vector_filters_and_old_cursors_can_branch_without_duplicates():
    store = fake_store()
    service = EvidenceService(store)
    filters = {"session_id": "source-session", "entity_filter": "合成别名", "start_time": "2026-10-01", "end_time": "2026-10-23"}
    await service.call(operation="search", owner="a", query="原问题", filters=filters)
    initial = cursor(service)
    await service.call(operation="expand", owner="a", cursor=initial, concept_id="concept_1", limit=3)
    state = service.cursors[cursor(service)]
    assert state["ids"] == [f"event_{i}" for i in range(7)]
    await service.call(operation="expand", owner="a", cursor=initial, concept_id="concept_1", limit=8)
    state = service.cursors[cursor(service)]
    assert state["ids"] == [f"event_{i}" for i in range(4)] + [f"event_{i}" for i in range(7, 11)]
    call = store.search_evidence.await_args
    assert call.args == ([1.0, 0.0], filters)
    assert call.kwargs["query"] == "原问题" and call.kwargs["limit"] == 4
    store.evidence_vector.assert_awaited_once_with("原问题")


async def test_expand_delivers_final_partial_batch_and_never_exceeds_chain_budget():
    service = EvidenceService(fake_store())
    await service.call(operation="search", owner="a", query="问题")
    for _ in range(5):
        await service.call(operation="expand", owner="a", cursor=cursor(service), concept_id="concept_1", limit=3)
    assert len(service.cursors[cursor(service)]["ids"]) == 19
    result = await service.call(operation="expand", owner="a", cursor=cursor(service), concept_id="concept_1", limit=8)
    assert "匹配:1" in result and len(service.cursors[cursor(service)]["ids"]) == 20
    with pytest.raises(ValueError, match="20"):
        await service.call(operation="expand", owner="a", cursor=cursor(service), concept_id="concept_1")


@pytest.mark.parametrize("failure", [RuntimeError("synthetic database error"), asyncio.CancelledError()])
async def test_failed_or_cancelled_search_preserves_history_reset_and_old_cursor(failure):
    store = fake_store()
    service = EvidenceService(store)
    await service.call(operation="search", owner="a", query="问题")
    token, before = cursor(service), list(service.histories["a"]["ids"])
    store.evidence_links.side_effect = failure
    with pytest.raises(type(failure)):
        await service.call(operation="search", owner="a", query="新问题", reset=True)
    assert token in service.cursors and list(service.histories["a"]["ids"]) == before
    assert not service._locks


async def test_render_failure_does_not_commit_history(monkeypatch):
    import memory.evidence_service as module
    service = EvidenceService(fake_store())
    monkeypatch.setattr(module, "render_evidence", lambda _: (_ for _ in ()).throw(ValueError("render error")))
    with pytest.raises(ValueError, match="render error"):
        await service.call(operation="search", owner="a", query="问题")
    assert not service.histories and not service.cursors


async def test_concurrent_searches_in_same_session_commit_disjoint_batches():
    service = EvidenceService(fake_store())
    await asyncio.gather(*(service.call(operation="search", owner="a", query="问题") for _ in range(3)))
    assert list(service.histories["a"]["ids"]) == [f"event_{i}" for i in range(12)]
    assert not service._locks


@pytest.mark.parametrize("change,match", [("owner", "当前会话"), ("concept", "追查入口"),
                                        ("model", "模型或维度"), ("generation", "索引已更新"), ("expiry", "过期")])
async def test_continuations_reject_wrong_owner_concept_model_generation_or_expiry(change, match):
    now, store = [0], fake_store()
    service = EvidenceService(store, clock=lambda: now[0])
    await service.call(operation="search", owner="a", query="问题")
    token, owner, concept = cursor(service), "a", "concept_1"
    if change == "owner":
        owner = "b"
    elif change == "concept":
        concept = "concept_other"
    elif change == "model":
        store.evidence_model = lambda: ("other-model", 2, 0)
    elif change == "generation":
        store._evidence_generation += 1
    else:
        now[0] = 901
    with pytest.raises(ValueError, match=match):
        await service.call(operation="expand", owner=owner, cursor=token, concept_id=concept)


async def test_filter_only_search_and_source_read_never_request_embedding():
    store = fake_store()
    service = EvidenceService(store)
    await service.call(operation="search", owner="a", filters={"entity_filter": "合成别名"})
    output = await service.call(operation="read", owner="a", event_id="event_0", start=2, limit=4)
    assert "原文[2:6) / 共9字符" in output and "start=6" in output
    store.evidence_vector.assert_not_awaited()
    store.read_evidence.assert_awaited_once_with("event_0", 2, 4)


async def test_expired_history_releases_events_and_state_tables_are_bounded():
    now = [0]
    service = EvidenceService(fake_store(), clock=lambda: now[0])
    for index in range(40):
        await service.call(operation="search", owner=str(index), query="问题")
    assert len(service.histories) == len(service.cursors) == 32
    assert "0" not in service.histories
    now[0] = 901
    await service.call(operation="search", owner="39", query="问题")
    assert len(service.histories) == len(service.cursors) == 1
    assert list(service.histories["39"]["ids"])[0] == "event_0"


async def test_tool_uses_host_session_and_event_evidence_route(monkeypatch):
    store = fake_store()
    monkeypatch.setattr(search_mod, "LadybugStore", lambda: store)
    context = Context("", [], "", "web", session_id="trusted-host-session")
    token = current_tool_context.set((context, []))
    try:
        output = await search_mod.memory_database_search({"query_text": "问题", "date_to": "2026-10-22"})
        assert "匹配:4" in output
        assert list(store._evidence_service.histories) == ["trusted-host-session"]
        assert store.search_evidence.await_args.args[1]["end_time"] == "2026-10-23T00:00:00"
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("args", [
    {"operation": "unknown"}, {"operation": "read"}, {"operation": "expand"},
    {"operation": "read", "event_id": "e", "start": -1},
    {"operation": "read", "event_id": "e", "limit": True},
    {"operation": "read", "event_id": "e", "query_text": "x", "start": 0},
    {"operation": "read", "event_id": "e", "reset_seen": False},
    {"operation": "expand", "cursor": "x", "concept_id": "c", "limit": 9},
    {"operation": "expand", "cursor": "x", "concept_id": "c", "date_from": "2026-10-01"},
    {"query_text": "x", "reset_seen": "yes"}, {"query_text": "x", "start": 0},
])
async def test_invalid_operation_parameters_fail_before_store_creation(monkeypatch, args):
    monkeypatch.setattr(search_mod, "LadybugStore", lambda: pytest.fail("invalid input initialized storage"))
    assert json.loads(await search_mod.memory_database_search(args))["status"] == "error"


async def test_session_only_search_keeps_host_owner_separate_from_source_filter(monkeypatch):
    store = fake_store()
    monkeypatch.setattr(search_mod, "LadybugStore", lambda: store)
    context = Context("", [], "", "web", session_id="host-session")
    token = current_tool_context.set((context, []))
    try:
        output = await search_mod.memory_database_search({"session_id": " source-session "})
        assert "匹配:4" in output
        assert list(store._evidence_service.histories) == ["host-session"]
        assert store.search_evidence.await_args.args[1]["session_id"] == "source-session"
        store.evidence_vector.assert_not_awaited()
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("args", [
    {"session_id": 42}, {"session_id": "x" * 501}, {"session_id": "  "},
    {"session_id": "source", "target_label": "Person"},
    {"operation": "expand", "cursor": "c", "concept_id": "c", "session_id": "source"},
    {"operation": "read", "event_id": "e", "session_id": "source"},
])
async def test_invalid_session_filter_fails_before_storage(monkeypatch, args):
    monkeypatch.setattr(search_mod, "LadybugStore", lambda: pytest.fail("invalid input initialized storage"))
    assert json.loads(await search_mod.memory_database_search(args))["status"] == "error"
