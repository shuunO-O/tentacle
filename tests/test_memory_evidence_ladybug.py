"""仅 GitHub runner 的临时 Ladybug 合成图：双路召回、事务写入、旧库补建及过滤追查。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from memory.ladybug_driver import LadybugDriver
import pytest
import pytest_asyncio

from memory.evidence_service import EvidenceService
from memory.evidence import SearchSettings
from memory.ladybug_store import LadybugStore
from memory.retrieval import source_chunks


@pytest_asyncio.fixture
async def graph(tmp_path):
    prefix = "synthetic-evidence-" + uuid4().hex
    driver = LadybugDriver(tmp_path / "synthetic.lbdb", 2)
    store = object.__new__(LadybugStore)
    store.driver, store._initialized, store._evidence_generation = driver, True, 0
    store.dimensions = 2
    store._save_lock = asyncio.Lock()
    store._ensure_indexes = AsyncMock()
    store.evidence_settings = lambda: SearchSettings(event_top_k=4, include_index=False)
    store.embedding_client = SimpleNamespace(model_name="synthetic", dimensions=2,
                                            embed=AsyncMock(side_effect=lambda texts: [[0.0, 1.0] for _ in texts]))
    try:
        yield SimpleNamespace(store=store, prefix=prefix)
    finally:
        async with driver.session() as session:
            await session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=prefix)
        await driver.close()


async def test_source_only_candidate_outside_summary_top40_survives_filters_and_exclusion(graph):
    store, prefix = graph.store, graph.prefix
    await store._evidence_query("""CREATE (p:Person {id:$person,name:$person,aliases:[$alias]})
        WITH p UNWIND range(0,44) AS i
        CREATE (e:Event {id:$prefix+CAST(i AS STRING),summary:'无关摘要',content:'无关正文'+CAST(i AS STRING),
                        create_time:'2026-10-22T12:00:00',embedding:[0.6,0.8]}),
               (p)-[:PARTICIPATED_IN]->(e)
        WITH DISTINCT p
        CREATE (t:Event {id:$target,summary:'普通摘要',content:'原文里有地下暗号',
                        create_time:'2026-10-22T23:59:59.999999',embedding:[1.0,0.0]}),
               (p)-[:PARTICIPATED_IN]->(t),
               (t)-[:HAS_SOURCE]->(:EventSource {id:$target+':source:0',hash:'target-source',
                   embedding:[0.0,1.0],embedding_model:'synthetic',embedding_dim:2})""",
        prefix=prefix, person=prefix + "person", alias=prefix + "alias", target=prefix + "target")
    filters = {"entity_filter": prefix + "alias", "start_time": "2026-10-22T00:00:00",
               "end_time": "2026-10-23T00:00:00"}
    found = await store.search_evidence([0.0, 1.0], filters, query="地下暗号")
    assert found[0]["id"] == prefix + "target"
    assert all(e["content"] for e in found)
    again = await store.search_evidence([0.0, 1.0], filters, query="地下暗号", excluded=[e["id"] for e in found])
    assert len(again) == 4 and not {e["id"] for e in found}.intersection(e["id"] for e in again)
    assert not await store.search_evidence([0.0, 1.0], {**filters, "end_time": "2026-10-22T00:00:00"})


async def test_matched_tail_semantic_read_and_full_delivery_on_synthetic_graph(graph):
    store, prefix = graph.store, graph.prefix
    text = "😀" * 33000 + "任职时长是三年九个月，其他细节也在这里。"
    chunks = [{"id": prefix + f"source-{i}", "start": start, "end": len(text), "text": text[start:]}
              for i, start in enumerate((33000, 33005))]
    await store._evidence_query("""CREATE (p:Person {id:$person,name:$person}),
        (e:Event {id:$id,summary:'其他主题',content:$text,embedding:[1.0,0.0],
                  create_time:'2026-11-01T12:00:00'}), (p)-[:PARTICIPATED_IN]->(e)
        WITH e UNWIND $chunks AS chunk
        CREATE (s:EventSource {id:chunk.id,start:chunk.start,`end`:chunk.`end`,text:chunk.text,
            hash:chunk.id,embedding:[0.0,1.0],embedding_model:'synthetic',embedding_dim:2}),
            (e)-[:HAS_SOURCE]->(s)""", id=prefix + "tail", person=prefix + "person", text=text, chunks=chunks)
    found = await store.search_evidence([0.0, 1.0], {"entity_filter": prefix + "person"}, query="任职时长")
    assert found[0]["matched_source"]["start"] == 33000
    assert len(found[0]["content"]) == 32000
    result = await store.semantic_read(prefix + "tail", "任职时长", limit=9)
    assert result["matches"][0]["range"] == [33000, 33009]
    assert result["matches"][0]["text"] == text[33000:33009]
    service = EvidenceService(store)
    excerpt = await service.call(operation="search", owner="excerpt", query="任职时长",
                                 filters={"entity_filter": prefix + "person"})
    assert "任职时长是三年九个月" in excerpt and "原文[33000:" in excerpt
    store.evidence_settings = lambda: SearchSettings(event_top_k=1, content_mode="full", include_index=False)
    full = await service.call(operation="search", owner="full", query="任职时长",
                              filters={"entity_filter": prefix + "person"})
    assert text in full


async def test_event_and_sources_commit_together_and_failed_relation_rolls_both_back(graph):
    store, prefix = graph.store, graph.prefix
    sources = [{**chunk, "id": prefix + "saved:source:0", "embedding": [0.0, 1.0],
                "embedding_model": "synthetic", "embedding_dim": 2} for chunk in source_chunks("😀精确原文")]

    async def save(event_id, relationships):
        async with store.driver.session() as session:
            await session.execute_write(LadybugStore._save_memory_tx, event_id, "synthetic", "摘要", "😀精确原文",
                                        "2026-10-22", "synthetic-session", [1.0, 0.0], [], relationships, {},
                                        source_records=sources, embedding_model="synthetic")

    await save(prefix + "saved", [])
    record = await store._evidence_query("""MATCH (e:Event {id:$id})-[:HAS_SOURCE]->(s:EventSource)
        RETURN e.content AS content,s.text AS text,s.start AS start,s.`end` AS `end`,s.embedding_model AS model""",
        id=prefix + "saved")
    assert record == [{"content": "😀精确原文", "text": "😀精确原文", "start": 0, "end": 5, "model": "synthetic"}]
    assert await store.count_summary() == {"nodes": {"Event": 1}, "relationships": 0}
    # 使用新来源ID，确保回滚由非法关系触发，而非唯一性约束。
    sources[0]["id"] = prefix + "failed:source:0"
    with pytest.raises(ValueError, match="invalid_relationship"):
        await save(prefix + "failed", [{"type": "INVALID"}])
    assert not await store._evidence_query("MATCH (n) WHERE n.id IN $ids RETURN n.id AS id",
                                          ids=[prefix + "failed", prefix + "failed:source:0"])


async def test_old_events_backfill_sources_atomically_and_failed_embedding_keeps_old_sources(graph):
    store, prefix = graph.store, graph.prefix
    content = "😀甲" * 1100
    await store._evidence_query("""CREATE (e:Event {id:$id,summary:'原摘要',content:$content,embedding:[1.0,0.0]}),
        (e)-[:HAS_SOURCE]->(:EventSource {id:$id+':old',text:'旧来源',embedding:[1.0,0.0]})""",
        id=prefix + "old", content=content)
    store.embedding_client.embed.side_effect = RuntimeError("synthetic embedding failure")
    with pytest.raises(RuntimeError):
        await store.rebuild_event_sources()
    assert await store._evidence_query("MATCH (s:EventSource {id:$id}) RETURN s.text AS text", id=prefix + "old:old") == [{"text": "旧来源"}]
    store.embedding_client.embed.side_effect = lambda texts: [[0.0, 1.0] for _ in texts]
    result = await store.rebuild_event_sources()
    assert result == {"events": 1, "chunks": 4} and store._evidence_generation == 1
    chunks = await store._evidence_query("""MATCH (e:Event {id:$id})-[:HAS_SOURCE]->(s:EventSource)
        RETURN s.start AS start,s.`end` AS `end`,s.text AS text,e.summary AS summary,e.embedding AS vector
        ORDER BY s.start""", id=prefix + "old")
    assert all(content[c["start"]:c["end"]] == c["text"] and c["summary"] == "原摘要" and c["vector"] == [1.0, 0.0] for c in chunks)
    source = await store.read_evidence(prefix + "old", 799, 5)
    assert source["text"] == content[799:804] and source["range"] == [799, 804]


async def test_graph_expansion_retains_alias_dates_and_counts_actual_global_degree(graph):
    store, prefix = graph.store, graph.prefix
    await store._evidence_query("""CREATE (p:Person {id:$person,name:$person,aliases:[$alias]}),
        (c:Concept {id:$concept,equivalence_group:$concept,name:'合成概念',embedding:[0.0,1.0]})
        WITH p,c UNWIND range(0,6) AS i
        CREATE (e:Event {id:$prefix+CAST(i AS STRING),summary:'合成摘要',content:'合成原文'+CAST(i AS STRING),
            create_time:CASE WHEN i=6 THEN '2026-10-23T00:00:00' ELSE '2026-10-22T12:00:00' END,
            embedding:[0.0,1.0]}), (p)-[:PARTICIPATED_IN]->(e),
            (p)-[:HAS_ATTITUDE]->(e), (e)-[:MENTIONS]->(c)""",
        prefix=prefix, person=prefix + "person", alias=prefix + "alias", concept=prefix + "concept")
    service = EvidenceService(store)
    first = await service.call(operation="search", owner="synthetic", query="合成",
                               filters={"entity_filter": prefix + "alias", "end_time": "2026-10-23T00:00:00"})
    token = next(reversed(service.cursors))
    assert "全库7条" in first and "全库未展示3条" in first
    added = await service.call(operation="expand", owner="synthetic", cursor=token, concept_id=prefix + "concept")
    assert "匹配:2" in added and "沿图补充" in added
    state = service.cursors[next(reversed(service.cursors))]
    assert len(state["ids"]) == 6 and prefix + "6" not in state["ids"]
    store.embedding_client.embed.assert_awaited_once_with(["合成"])


async def test_session_filter_isolates_both_routes_index_and_expansion(graph):
    store, prefix = graph.store, graph.prefix
    store.evidence_settings = lambda: SearchSettings(event_top_k=1, include_index=True, index_top_k=2)
    await store._evidence_query("""CREATE (c:Concept {id:$concept,equivalence_group:$concept,name:$concept})
        WITH c UNWIND range(0,5) AS i
        CREATE (e:Event {id:$prefix+CAST(i AS STRING),summary:'合成摘要',content:'合成正文'+CAST(i AS STRING),
            raw_session:CASE WHEN i<3 THEN $source ELSE $other END,
            create_time:'2026-11-02T12:00:00',embedding:[0.0,1.0]}),
            (e)-[:MENTIONS]->(c),
            (e)-[:HAS_SOURCE]->(:EventSource {id:$prefix+'chunk'+CAST(i AS STRING),hash:CAST(i AS STRING),
                embedding:[0.0,1.0],embedding_model:'synthetic',embedding_dim:2})""",
        prefix=prefix, concept=prefix+'concept', source=prefix+'source', other=prefix+'other')
    filters = {"session_id": prefix+'source'}
    for vector in (None, [0.0, 1.0]):
        found = await store.search_evidence(vector, filters, limit=8)
        assert len(found) == 3
        assert all(e["raw_session"] == prefix+'source' for e in found)
        assert all(e["create_time"] == '2026-11-02T12:00:00' for e in found)
    service = EvidenceService(store)
    result = await service.call(operation="search", owner="host", query="正文", filters=filters)
    assert "候选索引" in result and result.count('raw_session=') == 3
    assert prefix+'other' not in result
    token = next(iter(service.cursors))
    result = await service.call(operation="expand", owner="host", cursor=token, concept_id=prefix+'concept')
    assert prefix+'source' in result and prefix+'other' not in result
