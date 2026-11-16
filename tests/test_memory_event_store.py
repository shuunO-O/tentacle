"""事件存储的故障边界，使用假驱动与合成记录。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memory.ladybug_store import LadybugStore


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for row in self.rows:
            yield row


def event(node_id="a", content="原文", **extra):
    return {"id": node_id, "summary": "摘要", "content": content,
            "content_length": len(content), "source_hashes": [], **extra}


def store():
    obj = object.__new__(LadybugStore)
    obj._initialized, obj._evidence_generation = True, 0
    obj._save_lock = asyncio.Lock()
    obj.embedding_client = SimpleNamespace(model_name="synthetic", dimensions=2,
                                          embed=AsyncMock(return_value=[[1.0, 0.0]]))
    obj._ensure_indexes = AsyncMock()
    return obj


@pytest.mark.parametrize("routes", [
    [[event(), event()], []], [[event()], [event(content="changed")]],
    [[event(content_length=40000)], [event(content_length=50000)]],
])
async def test_fusion_refuses_duplicate_ids_and_changed_records(routes):
    obj = store()
    obj._evidence_query = AsyncMock(side_effect=routes)
    with pytest.raises(ValueError):
        await obj.search_evidence([1.0, 0.0], {})


async def test_filtered_queries_exclude_before_candidate_limit_on_both_routes():
    obj = store()
    obj._evidence_query = AsyncMock(side_effect=[[], []])
    filters = {"entity_filter": "别名", "start_time": "2026-10-01", "end_time": "2026-10-23"}
    assert not await obj.search_evidence([1.0, 0.0], filters, excluded=["old"], concept_id="concept", anchors=["anchor"])
    for call in obj._evidence_query.await_args_list:
        query = call.args[0]
        assert query.index("NOT e.id IN $excluded") < query.index("LIMIT $candidates")
        assert "e.create_time < $end_time" in query and "m:Person|Location" in query
        assert "a.id IN $anchors" in query
        assert call.kwargs["excluded"] == ["old"] and call.kwargs["candidates"] == 40


@pytest.mark.parametrize("rows,start", [([], 0), ([{"text": "a", "total": 1}] * 2, 0),
                                        ([{"text": "", "total": 1}], 2)])
async def test_read_refuses_missing_duplicate_or_out_of_range_sources(rows, start):
    obj = store()
    obj._evidence_query = AsyncMock(return_value=rows)
    with pytest.raises(ValueError):
        await obj.read_evidence("event", start, 10)
    obj.embedding_client.embed.assert_not_awaited()


async def test_backfill_waits_for_save_lock_and_never_writes_after_embedding_failure():
    obj = store()
    obj._evidence_query = AsyncMock(return_value=[{"id": "a", "content": "原文", "model": None, "dimensions": 2}])
    obj.embedding_client.embed.side_effect = RuntimeError("synthetic embedding failure")
    async with obj._save_lock:
        task = asyncio.create_task(obj.rebuild_event_sources())
        await asyncio.sleep(0)
        obj._evidence_query.assert_not_awaited()
    with pytest.raises(RuntimeError, match="embedding failure"):
        await task
    obj._ensure_indexes.assert_not_awaited()
    assert obj._evidence_query.await_count == 1


async def test_stale_backfill_snapshot_aborts_before_deleting_existing_sources():
    snapshot = [{"id": "a", "content": "原文", "model": None, "dimensions": 2}]
    tx = SimpleNamespace(run=AsyncMock(return_value=Rows([{**snapshot[0], "content": "已改变"}])))
    with pytest.raises(ValueError, match="补建期间变化"):
        await LadybugStore._replace_event_sources_tx(tx, snapshot, [])
    assert tx.run.await_count == 2
    assert "MemoryMeta" in tx.run.await_args_list[0].args[0]
    assert all("DELETE" not in call.args[0] for call in tx.run.await_args_list)


@pytest.mark.parametrize("change", ["dimensions", "model", "count"])
async def test_backfill_refuses_incompatible_or_incomplete_vectors_before_any_write(change):
    obj = store()
    row = {"id": "a", "content": "原文", "model": None, "dimensions": 2}
    if change == "model":
        row["model"] = "different-model"
    elif change == "dimensions":
        row["dimensions"] = 3
    else:
        obj.embedding_client.embed.return_value = []
    obj._evidence_query = AsyncMock(return_value=[row])
    with pytest.raises(ValueError):
        await obj.rebuild_event_sources()
    obj._ensure_indexes.assert_not_awaited()


async def test_duplicate_event_ids_abort_backfill_before_requesting_embeddings():
    obj = store()
    obj._evidence_query = AsyncMock(return_value=[{"id": "duplicate"}] * 2)
    with pytest.raises(ValueError, match="ID"):
        await obj.rebuild_event_sources()
    obj.embedding_client.embed.assert_not_awaited()


@pytest.mark.parametrize("vector", [None, [1.0, 0.0]])
async def test_session_filter_and_metadata_apply_to_every_candidate_route(vector):
    obj = store()
    obj._evidence_query = AsyncMock(return_value=[])
    source = "source-session"
    await obj.search_evidence(vector, {"session_id": source})
    assert obj._evidence_query.await_count == (1 if vector is None else 2)
    for call in obj._evidence_query.await_args_list:
        query = call.args[0]
        assert query.index("e.raw_session = $session_id") < query.index("LIMIT")
        assert call.kwargs["session_id"] == source
        assert "e.create_time AS create_time, e.raw_session AS raw_session" in query
