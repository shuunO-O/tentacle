"""原生 Ladybug、临时文件及合成向量；不读取配置、私人 prompt 或运行数据库。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from config.files import ConfigFiles
from memory.concept_aligner import AlignmentConfig, ConceptAligner
from memory.concept_audit import ConceptAuditLog
from memory.concept_dedup import ConceptDedupManager
from memory.concept_maintenance import ConceptMaintenance, ReviewRequired
from memory.evidence_service import EvidenceService
from memory.id_generator import MemoryIdGenerator
from memory.ladybug_driver import LadybugDriver, Session
from memory.ladybug_store import LadybugStore
from memory.reembedding import ReembeddingError, rebuild_vectors, reembed_saved_config
from scripts import memory_sources


@pytest_asyncio.fixture
async def store(tmp_path):
    obj = object.__new__(LadybugStore)
    obj._initialized = True
    obj._indexes_ensured = False
    obj._evidence_generation = 0
    obj._save_lock = asyncio.Lock()
    obj.dimensions, obj.top_k = 2, 3
    obj.driver = LadybugDriver(tmp_path / "memory.lbdb", 2)
    obj.id_generator = MemoryIdGenerator(str(tmp_path / "ids.db"))
    obj.embedding_client = SimpleNamespace(model_name="synthetic", dimensions=2,
        embed=AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts]))
    obj.alignment_config = AlignmentConfig()
    obj.concept_audit = ConceptAuditLog(tmp_path / "audit.db")
    obj.concept_aligner = ConceptAligner(obj.alignment_config)
    yield obj
    await obj.close()


def extraction(name="知识图谱"):
    return {"event": {"type": "study", "summary": "合成摘要"},
            "entities": {"persons": ["Alice"], "locations": ["Paris"], "concepts": [name]},
            "relationships": [
                {"source": "Alice", "target": "CURRENT_EVENT", "type": "PARTICIPATED_IN"},
                {"source": "CURRENT_EVENT", "target": "Paris", "type": "OCCURRED_AT"},
                {"source": "CURRENT_EVENT", "target": name, "type": "MENTIONS"},
                {"source": "Alice", "target": name, "type": "HAS_ATTITUDE",
                 "properties": {"sentiment": "喜欢", "note": "保留属性"}},
            ]}


async def test_native_save_search_read_graph_and_persisted_restart(store):
    body = "甲😀❤️é👩‍💻乙。地下暗号只在原文。"
    event_id = await store.save_memory(body, "session", extraction())
    for start in range(len(body)):
        found = await store.read_evidence(event_id, start, 3)
        assert found["text"] == body[start:start + 3]
        assert found["total"] == len(body)
    found = await store.search_evidence([1.0, 0.0], {"session_id": "session"}, query="地下暗号")
    assert found[0]["id"] == event_id and found[0]["content_length"] == len(body)
    semantic = await store.semantic_read(event_id, "暗号")
    assert semantic["matches"][0]["text"] == body
    assert (await store.count_summary())["relationships"] == 4
    graph = await store.fetch_graph()
    assert len(graph["nodes"]) == 4 and len(graph["relationships"]) == 4
    person = await store.search("Alice", "Person", "Alice", event_limit=1)
    assert person["primary_match"]["context"]["events"][0]["id"] == event_id
    assert person["primary_match"]["context"]["attitudes"][0]["sentiment"] == "喜欢"
    path = store.driver.path
    await store.close()
    store.driver = LadybugDriver(path, 2)
    assert (await store.read_evidence(event_id, 0, len(body)))["text"] == body


async def test_reembed_cli_uses_current_config_and_preserves_graph_and_source_evidence(store, monkeypatch, capsys):
    original = "😀这段原文已有切片"
    legacy = "这段原文此前没有切片"
    first = await store.save_memory(original, "session-a", extraction("知识图谱"))
    second = await store.save_memory(legacy, "session-b", extraction("Knowledge Graph"))
    concepts = await store._query("MATCH (n:Concept) RETURN n.id AS id ORDER BY n.id")
    left, right = [row["id"] for row in concepts]
    graph = ConceptMaintenance(store)
    preview = await graph.preview(left, right)
    await graph.merge(left, right, preview["fingerprint"], "synthetic-reembed-group")
    async with store.driver.session() as session:
        await session.run("MATCH (:Event {id:$id})-[:HAS_SOURCE]->(s:EventSource) DETACH DELETE s", id=second)
    before = await store.fetch_graph()
    audits = await store._query("MATCH (a:ConceptGroupAudit) RETURN a.id AS id,a.snapshot AS snapshot")
    source = store.driver.path
    await store.close()
    source_data = source.read_bytes()

    client = SimpleNamespace(model_name="replacement", dimensions=3, _init=lambda: None,
        client=SimpleNamespace(close=AsyncMock()),
        embed=AsyncMock(side_effect=lambda texts: [[1.0, 2.0, 3.0] for _ in texts]))
    monkeypatch.setattr("memory.reembedding.BATCH_SIZE", 1)
    monkeypatch.setattr(memory_sources, "EmbeddingClient", lambda: client)
    def current_config(path):
        assert path is None
        return SimpleNamespace(get_memory_config=lambda: {"ladybug": {"path": str(source)}})

    monkeypatch.setattr(memory_sources, "ConfigManager", current_config)
    code = await asyncio.to_thread(memory_sources.main, ["--reembed-all", "--apply"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["nodes"] == {"Event": 2, "Person": 1, "Concept": 2, "Location": 1, "EventSource": 2}
    assert result["model"] == "replacement" and result["dimensions"] == 3 and result["added_sources"] == 1
    texts = [text for call in client.embed.await_args_list for text in call.args[0]]
    assert set(texts) == {"合成摘要", "Alice", "Paris", "知识图谱", "Knowledge Graph", original, legacy}
    assert result["output"] == str(source.resolve())
    assert source.read_bytes() != source_data
    assert not list(source.parent.glob(".*.reembed.lock"))
    client.client.close.assert_awaited_once()

    store.driver = LadybugDriver(source, 3)
    store._initialized, store._indexes_ensured, store.dimensions = True, False, 3
    store.embedding_client = client
    assert await store.fetch_graph() == before
    assert await store._query("MATCH (a:ConceptGroupAudit) RETURN a.id AS id,a.snapshot AS snapshot") == audits
    for event_id, body in [(first, original), (second, legacy)]:
        response = await store.semantic_read(event_id, "原文")
        assert response["matches"][0]["text"] == body
    found = await store.search_evidence([1.0, 2.0, 3.0], {"session_id": "session-a"}, query="原文")
    assert [row["id"] for row in found] == [first]
    next_id = await store.save_memory("切换之后写入", "session-c", extraction())
    assert (await store.read_evidence(next_id, 0, 20))["text"] == "切换之后写入"


async def test_reembed_model_failure_leaves_original_bytes(store, tmp_path):
    await store.save_memory("合成的全文", "session", extraction())
    source = store.driver.path
    await store.close()
    original = source.read_bytes()
    client = SimpleNamespace(model_name="replacement", dimensions=3,
        embed=AsyncMock(side_effect=[[[1.0, 2.0, 3.0]], RuntimeError("synthetic model failure")]))
    with pytest.raises(ReembeddingError, match="目标库"):
        await rebuild_vectors(source, source, client)
    client.embed.assert_not_awaited()
    with pytest.raises(RuntimeError, match="synthetic model failure"):
        await rebuild_vectors(source, None, client)
    assert client.embed.await_count == 2
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".*.reembed.lock"))
    assert not list(tmp_path.glob(".reembed-*"))


@pytest.mark.parametrize("changed", [False, True])
async def test_restart_reembed_checks_the_saved_config_again_before_switching(store, tmp_path, monkeypatch, changed):
    await store.save_memory("合成全文", "session", extraction())
    source = store.driver.path
    await store.close()
    original = source.read_bytes()
    config = tmp_path / "synthetic.yaml"
    raw = {"memory": {"ladybug": {"path": str(source)}, "embedding": {"dimensions": 3}},
           "providers": {"p": {"type": "openai"}},
           "models": {"m": {"provider": "p", "name": "replacement"}},
           "roles": {"embedding": {"model": "m"}}}
    config.write_text(json.dumps(raw), encoding="utf-8")
    revision = ConfigFiles(config).read()[0]

    async def embed(texts):
        if changed:
            config.write_text(json.dumps(raw) + "\n# changed during reembedding\n", encoding="utf-8")
        return [[1.0, 2.0, 3.0] for _ in texts]

    initialized = []
    client = SimpleNamespace(model_name="replacement", dimensions=3, embed=embed, _init=initialized.append,
                             close=AsyncMock())
    monkeypatch.setattr("memory.reembedding.EmbeddingClient", lambda: client)
    result = await reembed_saved_config(config, revision)
    assert initialized == [raw]
    client.close.assert_awaited_once()
    if changed:
        assert result["state"] == "failed" and "配置已改变" in result["message"]
        assert source.read_bytes() == original
    else:
        assert result["state"] == "succeeded" and source.read_bytes() != original


async def test_native_equivalent_groups_keep_edges_and_expand_all_members(store):
    first = await store.save_memory("第一条合成原文", "s", extraction("知识图谱"))
    second = await store.save_memory("第二条合成原文", "s", extraction("Knowledge Graph"))
    concepts = await store._query("MATCH (n:Concept) RETURN n.id AS id ORDER BY id")
    left, right = [n["id"] for n in concepts]
    graph = ConceptMaintenance(store)
    before = await store.fetch_graph()
    preview = await graph.preview(left, right)
    joined = await graph.merge(left, right, preview["fingerprint"], "operation-1")
    assert joined["group_id"] == left
    after = await store.fetch_graph()
    assert after["relationships"] == before["relationships"]
    assert len(after["nodes"]) == len(before["nodes"])
    assert await graph.merge(left, right, "stale", "operation-1") == joined
    for name in ["知识图谱", "Knowledge Graph"]:
        hits = await store.search_evidence(None, {"entity_filter": name})
        assert {e["id"] for e in hits} == {first, second}
    links, _ = await store.evidence_links([first], [1.0, 0.0])
    assert links[0]["degree"] == 2
    expanded = await store.search_evidence([1.0, 0.0], {}, concept_id=left, anchors=[first], excluded=[first])
    assert [e["id"] for e in expanded] == [second]
    recalled = await store._recall_concepts("KG", [1.0, 0.0], 5)
    assert len(recalled) == 1 and len(recalled[0]["members"]) == 2
    await graph.detach(left)
    assert [e["id"] for e in await store.search_evidence(None, {"entity_filter": "知识图谱"})] == [first]
    assert (await store.fetch_graph())["relationships"] == before["relationships"]


async def test_native_rejects_invalid_relations_and_stale_group_decisions(store):
    bad = extraction()
    bad["relationships"].append({"source": "Alice", "target": "missing", "type": "MENTIONS"})
    with pytest.raises(ValueError):
        await store.save_memory("必须原子回滚", "s", bad)
    assert not (await store.fetch_graph())["nodes"]
    await store.save_memory("原文一", "s", extraction("甲概念"))
    await store.save_memory("原文二", "s", extraction("乙概念"))
    ids = [r["id"] for r in await store._query("MATCH (n:Concept) RETURN n.id AS id ORDER BY id")]
    graph = ConceptMaintenance(store)
    preview = await graph.preview(*ids)
    await store._query("MATCH (n:Concept {id:$id}) SET n.first_context='changed'", id=ids[0])
    with pytest.raises(ReviewRequired, match="已变化"):
        await graph.merge(*ids, preview["fingerprint"], "stale-op")
    assert not await store._query("MATCH (a:ConceptGroupAudit) RETURN a")


async def test_native_parallel_saves_and_parameter_isolation(store):
    ids = await asyncio.gather(*(store.save_memory(f"合成正文{i}", "s", extraction()) for i in range(3)))
    assert len(set(ids)) == 3
    rows = await asyncio.gather(*(store.read_evidence(node_id, 0, 100) for node_id in ids))
    assert [r["text"] for r in rows] == [f"合成正文{i}" for i in range(3)]
    assert (await store.count_summary())["nodes"]["Concept"] == 1


async def test_native_alignment_joins_group_but_retains_mention_nodes_and_edges(store):
    store.alignment_config = AlignmentConfig(enabled=True)
    store.concept_aligner = ConceptAligner(store.alignment_config, SimpleNamespace(evaluate=AsyncMock(return_value={
        "model": "synthetic-jev", "answers": {"pair_0": {"type": "score", "score": 2,
            "probabilities": {"0": 0, "1": 0, "2": 1}, "confidence": 1}}})))
    first = await store.save_memory("知识图谱的合成原文", "s", extraction())
    report = []
    second = await store.save_memory("KG 的合成原文", "s", extraction("KG"), report)
    assert report[0]["action"] == "join"
    nodes = await store._query("MATCH (n:Concept) RETURN n.id AS id,n.name AS name,n.equivalence_group AS group_id ORDER BY id")
    assert len(nodes) == 2 and len({n["group_id"] for n in nodes}) == 1
    edges = await store._query("MATCH (e:Event)-[:MENTIONS]->(c:Concept) RETURN e.id AS event,c.id AS concept ORDER BY event")
    assert edges == [{"event": first, "concept": nodes[0]["id"]}, {"event": second, "concept": nodes[1]["id"]}]
    await store.save_memory("重复 KG 原文", "s", extraction("KG"))
    assert (await store.count_summary())["nodes"]["Concept"] == 2


async def test_native_within_batch_alignment_and_homonymous_distinct_concepts(store):
    store.alignment_config = AlignmentConfig(enabled=True)
    store.concept_aligner = ConceptAligner(store.alignment_config, SimpleNamespace(evaluate=AsyncMock(return_value={
        "model": "synthetic", "answers": {"pair_0": {"type": "score", "score": 2,
            "probabilities": {"0": 0, "1": 0, "2": 1}, "confidence": 1}}})))
    data = extraction("RAG")
    data["entities"]["concepts"].append("检索增强生成")
    data["relationships"].append({"source": "CURRENT_EVENT", "target": "检索增强生成", "type": "MENTIONS"})
    await store.save_memory("同一批次两个提及", "s", data)
    groups = await store._query("MATCH (n:Concept) RETURN n.equivalence_group AS group_id")
    assert len(groups) == 2 and len({g["group_id"] for g in groups}) == 1
    # 判定不同的同名提及必须建立新的独立节点。
    store.concept_aligner = SimpleNamespace(align=AsyncMock(side_effect=lambda records, *_: [
        {"mention": r["name"], "target_id": r["id"], "action": "create"} for r in records]))
    await store.save_memory("另一种 RAG 含义", "s", extraction("RAG"))
    graph = await store.fetch_graph(["Concept"])
    assert len(graph["nodes"]) == 3 and len({n["id"] for n in graph["nodes"]}) == 3
    assert len({n["equivalence_group"] for n in graph["nodes"]}) == 2


async def test_native_group_union_includes_all_members_without_collapsing_conflicting_edges(store):
    for name in ("甲", "乙", "丙"):
        data = extraction(name)
        data["relationships"][-1]["properties"]["sentiment"] = name
        await store.save_memory(f"{name}的原文", "s", data)
    ids = [r["id"] for r in await store._query("MATCH (n:Concept) RETURN n.id AS id ORDER BY id")]
    graph = ConceptMaintenance(store)
    before = (await store.fetch_graph())["relationships"]
    preview = await graph.preview(ids[0], ids[1])
    await graph.merge(ids[0], ids[1], preview["fingerprint"], "one")
    preview = await graph.preview(ids[1], ids[2])
    assert preview["conflicts"] == [] and len(preview["left"]["members"]) == 2
    await graph.merge(ids[2], ids[1], preview["fingerprint"], "two")
    groups = await store._query("MATCH (n:Concept) RETURN n.equivalence_group AS group_id")
    assert {r["group_id"] for r in groups} == {ids[2]}
    assert (await store.fetch_graph())["relationships"] == before


async def test_detach_representative_preserves_remaining_group_events_audit_and_invalidates_cursor(store):
    events = [await store.save_memory(f"{name}合成原文", "s", extraction(name)) for name in ("甲", "乙", "丙")]
    ids = [row["id"] for row in await store._query("MATCH (n:Concept) RETURN n.id AS id ORDER BY id")]
    graph = ConceptMaintenance(store)
    for other in ids[1:]:
        preview = await graph.preview(ids[0], other)
        await graph.merge(ids[0], other, preview["fingerprint"], f"group-{other}")
    # Multiple mentions in a group must not inflate degrees or duplicate events.
    await store._query("MATCH (e:Event {id:$event}),(c:Concept {id:$concept}) CREATE (e)-[:MENTIONS]->(c)",
                       event=events[1], concept=ids[2])
    before = await store.fetch_graph()
    links, _ = await store.evidence_links(events, None)
    assert len(links) == 3 and all(row["degree"] == 3 for row in links)
    assert len(await store.search_evidence(None, {"entity_filter": "乙"})) == 3
    service = EvidenceService(store)
    await service.call(operation="search", owner="synthetic", filters={"entity_filter": "乙"})
    cursor = next(iter(service.cursors))
    generation = store._evidence_generation
    result = await graph.detach("Concept:" + ids[0])
    assert result["previous_group_id"] == ids[0] and result["group_id"] != ids[0]
    assert store._evidence_generation == generation + 1
    with pytest.raises(ValueError, match="概念分组变化"):
        await service.call(operation="expand", owner="synthetic", cursor=cursor, concept_id=ids[0])
    assert [row["id"] for row in await store.search_evidence(None, {"entity_filter": "甲"})] == [events[0]]
    for name in ("乙", "丙"):
        assert {row["id"] for row in await store.search_evidence(None, {"entity_filter": name})} == set(events[1:])
    assert await store.search_evidence(None, {}, concept_id=ids[0], anchors=[events[0]], excluded=[events[0]]) == []
    after = await store.fetch_graph()
    assert after["relationships"] == before["relationships"] and len(after["nodes"]) == len(before["nodes"])
    audit = await store._query("MATCH (a:ConceptGroupAudit) RETURN a.snapshot AS snapshot,a.result AS result")
    detached = next(row for row in audit if json.loads(row["result"]).get("concept_id") == ids[0])
    assert json.loads(detached["snapshot"])[0]["equivalence_group"] == ids[0]
    assert json.loads(detached["result"]) == result
    generation = store._evidence_generation
    with pytest.raises(ValueError, match="不存在"):
        await graph.detach("missing")
    assert store._evidence_generation == generation


@pytest.mark.parametrize("concept_id", [None, "", " ", "Concept:", "x" * 501])
async def test_detach_rejects_invalid_ids_without_initializing_store(concept_id):
    store = SimpleNamespace(_initialized=False)
    with pytest.raises(ValueError, match="concept_id"):
        await ConceptMaintenance(store).detach(concept_id)


async def test_detach_needs_no_model_initialization_and_audit_failure_rolls_back(store, monkeypatch):
    await store._query("CREATE (:Concept {id:'synthetic',name:'合成概念',equivalence_group:'original'})")
    store._initialized = False
    monkeypatch.setattr(store, "_init", Mock(side_effect=AssertionError("must not initialize models")))
    graph = ConceptMaintenance(store)
    run = Session.run

    async def fail_audit(session, query, *args, **kwargs):
        if "CREATE (:ConceptGroupAudit" in query:
            raise RuntimeError("synthetic audit failure")
        return await run(session, query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "run", fail_audit)
        with pytest.raises(RuntimeError, match="audit failure"):
            await graph.detach("synthetic")
    nodes = await store._query("MATCH (n:Concept) RETURN n.equivalence_group AS group_id")
    assert nodes == [{"group_id": "original"}] and store._evidence_generation == 0
    assert not await store._query("MATCH (n:ConceptGroupAudit) RETURN n")
    result = await graph.detach("synthetic")
    assert result["previous_group_id"] == "original" and store._evidence_generation == 1
    store._init.assert_not_called()


async def test_native_cancelled_transaction_does_not_persist_partial_write(store):
    entered = asyncio.Event()
    async def save(tx):
        await tx.run("CREATE (:Event {id:'cancelled',summary:'never committed'})")
        entered.set()
        await asyncio.Event().wait()
    async def run():
        async with store.driver.session() as session:
            await session.execute_write(save)
    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await store._query("MATCH (n:Event {id:'cancelled'}) RETURN n")
    await store.save_memory("取消后可以继续写入", "s", extraction())


async def test_native_embedding_metadata_mismatch_and_score_scale(store):
    await store.save_memory("原文", "s", extraction())
    rows = await store._query(f"MATCH (e:Event) RETURN {store._similarity('e.embedding')} AS score", vector=[-1.0, 0.0])
    assert abs(rows[0]["score"]) < 1e-6
    store._indexes_ensured = False
    store.embedding_client.model_name = "other-model"
    with pytest.raises(ValueError, match="模型或维度不同"):
        await store._ensure_indexes()


async def test_grouping_respects_independent_members_across_entire_groups(store, tmp_path):
    for name in ("甲", "乙", "丙"):
        await store.save_memory(f"{name}的原文", "s", extraction(name))
    ids = [r["id"] for r in await store._query("MATCH (n:Concept) RETURN n.id AS id ORDER BY id")]
    graph = ConceptMaintenance(store)
    preview = await graph.preview(ids[0], ids[1])
    await graph.merge(ids[0], ids[1], preview["fingerprint"], "join")
    obj = ConceptDedupManager(tmp_path / "dedup.db", graph=graph, config=AlignmentConfig(), client=SimpleNamespace())
    await obj.db("write", "INSERT INTO independent VALUES(?,?)", (ids[0], ids[2]))
    preview = await graph.preview(ids[1], ids[2])
    from memory.concept_maintenance import encode
    await obj.db("write", "INSERT INTO pairs(id,left_id,right_id,state,data) VALUES(?,?,?,?,?)",
                 ("candidate", ids[1], ids[2], "pending", encode({"preview": preview})))
    with pytest.raises(ReviewRequired, match="保持独立"):
        await obj.decide("candidate", "merge", ids[1])


async def test_native_filter_only_search_does_not_initialize_embedding_credentials(store):
    store.embedding_client = SimpleNamespace(_initialized=False, embed=AsyncMock())
    assert await store.search(None, "Concept", "不存在") == {"primary_match": None, "secondary_matches": []}
    store.embedding_client.embed.assert_not_awaited()


async def test_native_ambiguous_name_can_still_create_independent_concept_with_alignment(store):
    store.alignment_config = AlignmentConfig(enabled=True)
    store.concept_aligner = SimpleNamespace(align=AsyncMock(side_effect=lambda records, *_: [
        {"mention": r["name"], "target_id": r["id"], "action": "create"} for r in records]))
    for index in range(3):
        await store.save_memory(f"第{index}种同名含义", "s", extraction("Apple"))
    nodes = (await store.fetch_graph(["Concept"]))["nodes"]
    assert len(nodes) == 3 and len({n["equivalence_group"] for n in nodes}) == 3
    store.alignment_config = AlignmentConfig(enabled=False)
    with pytest.raises(ValueError, match="多个节点"):
        await store.save_memory("不能猜测名称指向", "s", extraction("Apple"))


async def test_native_entity_filter_keeps_non_mentions_concept_relationships(store):
    data = extraction("概念")
    data["relationships"] = [{"source": "CURRENT_EVENT", "target": "概念", "type": "HAS_ATTITUDE",
                               "properties": {"sentiment": "喜欢"}}]
    event_id = await store.save_memory("概念态度原文", "s", data)
    found = await store.search_evidence(None, {"entity_filter": "概念"})
    assert [e["id"] for e in found] == [event_id]
    found = await store.search("问题", "Event", "概念")
    assert found["primary_match"]["node"]["id"] == event_id
