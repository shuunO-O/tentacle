"""合成 Concept 对齐回归：不读取记忆库、不调用模型、不启动应用。"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import memory.concept_aligner as align_mod
from memory.concept_aligner import AlignmentConfig, ConceptAligner, JevClient
from memory.ladybug_store import LadybugStore


def answer(different=0.01, uncertain=0.01, same=0.98):
    return {"type": "score", "score": uncertain + 2 * same, "confidence": 0.9,
            "probabilities": {"0": different, "1": uncertain, "2": same}}


def response(*answers):
    return {"model": "jev-synthetic", "answers": {f"pair_{i}": a for i, a in enumerate(answers)}}


def entity(name="检索增强生成", node_id="concept_new"):
    return {"name": name, "id": node_id, "embedding": [1.0, 0.0], "label": "Concept"}


def candidate(node_id="concept_existing", name="RAG"):
    return {"id": node_id, "name": name, "aliases": [], "first_context": "检索增强生成技术",
            "events": ["合成技术讨论"]}


async def decide(payload, candidates=None, config=None):
    client = SimpleNamespace(evaluate=AsyncMock(return_value=payload))
    recall = AsyncMock(return_value=[candidate()] if candidates is None else candidates)
    aligner = ConceptAligner(config or AlignmentConfig(enabled=True), client)
    decisions = await aligner.align([entity()], "合成原始摘要", "合成短摘要", recall)
    return decisions[0], client, recall


async def test_synonym_joins_only_existing_candidate_group_and_preserves_original_evidence():
    decision, client, recall = await decide(response(answer()))
    assert (decision["action"], decision["target_id"], decision["status"]) == (
        "join", "concept_existing", "aligned")
    assert decision["mention"] == "检索增强生成"
    assert decision["candidates"][0]["probabilities"]["2"] == 0.98
    incoming, candidates = client.evaluate.await_args.args
    assert incoming["context"] == "合成原始摘要"
    assert incoming["summary"] == "合成短摘要"
    assert candidates[0]["events"] == ["合成技术讨论"]
    recall.assert_awaited_once_with("检索增强生成", [1.0, 0.0], 5)


@pytest.mark.parametrize("payload,status,reason", [
    (response(answer(0.98, 0.01, 0.01)), "new", "different"),
    (response(answer(0.1, 0.8, 0.1)), "pending", "uncertain"),
    (response(answer(0.03, 0.03, 0.94)), "pending", "uncertain"),
])
async def test_related_uncertain_and_below_threshold_stay_independent(payload, status, reason):
    decision, _, _ = await decide(payload)
    assert (decision["target_id"], decision["action"], decision["status"], decision["reason"]) == (
        "concept_new", "create", status, reason)


async def test_same_name_different_meaning_is_not_an_exact_name_shortcut():
    decision, client, _ = await decide(response(answer(0.98, 0.01, 0.01)), [candidate(name="检索增强生成")])
    assert decision["action"] == "create"
    client.evaluate.assert_awaited_once()


@pytest.mark.parametrize("other,expected", [
    (answer(), "conflicting_candidates"),
    (answer(0.1, 0.8, 0.1), "uncertain"),
    (answer(0.98, 0.01, 0.01), "same"),
])
async def test_multiple_candidates_need_a_single_clear_identity(other, expected):
    decision, _, _ = await decide(response(answer(), other), [candidate(), candidate("concept_other", "知识库")])
    assert decision["reason"] == expected
    assert decision["action"] == ("join" if expected == "same" else "create")


async def test_empty_recall_skips_jev():
    decision, client, _ = await decide({}, [])
    assert decision["reason"] == "no_candidates"
    assert decision["status"] == "new"
    client.evaluate.assert_not_awaited()


async def test_within_batch_synonyms_join_the_first_new_group():
    client = SimpleNamespace(evaluate=AsyncMock(return_value=response(answer())))
    recall = AsyncMock(return_value=[])
    decisions = await ConceptAligner(AlignmentConfig(enabled=True), client).align(
        [entity("RAG", "concept_1"), entity("检索增强生成", "concept_2")], "合成正文", "合成摘要", recall)
    assert decisions[0]["action"] == "create"
    assert decisions[1]["target_id"] == "concept_1"
    assert decisions[1]["action"] == "join"


async def test_local_candidates_compare_canonical_evidence_without_transitive_alias_chain():
    client = SimpleNamespace(evaluate=AsyncMock(side_effect=[response(answer()), response(answer(0.1, 0.8, 0.1))]))
    decisions = await ConceptAligner(AlignmentConfig(enabled=True), client).align(
        [entity("A", "c1"), entity("B", "c2"), entity("C", "c3")], "正文", "摘要", AsyncMock(return_value=[]))
    assert decisions[1]["action"] == "join"
    assert decisions[2]["action"] == "create"
    assert [c["name"] for c in client.evaluate.await_args.args[1]] == ["A"]


@pytest.mark.parametrize("bad", [
    {}, response(), response({"type": "noul", "noul": 1}),
    response(dict(answer(), probabilities={"0": 0, "1": 0})),
    response(dict(answer(), probabilities={"0": 0, "1": 0, "2": 2})),
    response(dict(answer(), probabilities={"0": 0, "1": 0, "2": 0})),
    response(dict(answer(), confidence=float("nan"))),
    response(dict(answer(), score=float("inf"))),
    response(dict(answer(), score=0)),
    response(dict(answer(), probabilities={"0": False, "1": 0, "2": 1})),
])
async def test_malformed_or_incomplete_answers_cannot_reuse(bad):
    decision, _, _ = await decide(bad)
    assert decision["action"] == "create"
    assert decision["status"] == "pending"
    assert decision["reason"] == "jev_error"
    json.dumps(decision, allow_nan=False)


@pytest.mark.parametrize("rounded", [
    {"type": "score", "score": 1.96, "confidence": 0.93, "probabilities": {"0": 0.02, "1": 0.01, "2": 0.97}},
    {"type": "score", "score": 1.99, "confidence": 1, "probabilities": {"0": 0, "1": 0, "2": 1}},
    {"type": "score", "score": 1.98, "confidence": 0.98, "probabilities": {"0": 0, "1": 0.01, "2": 0.99}},
    {"type": "score", "score": 1.98, "confidence": 0.94, "probabilities": {"0": 0.01, "1": 0.01, "2": 0.99}},
])
async def test_independently_rounded_openrouter_scores_are_valid(rounded):
    decision, _, _ = await decide(response(rounded))
    assert decision["action"] == "join"
    assert "error_code" not in decision
    assert decision["candidates"][0]["probabilities"] == rounded["probabilities"]


@pytest.mark.parametrize("stage", ["recall", "jev"])
async def test_failures_are_not_disguised_as_no_candidates(stage):
    recall = AsyncMock(return_value=[candidate()])
    client = SimpleNamespace(evaluate=AsyncMock(return_value=response(answer())))
    getattr(client, "evaluate").side_effect = TimeoutError if stage == "jev" else None
    recall.side_effect = ConnectionError if stage == "recall" else None
    decision = (await ConceptAligner(AlignmentConfig(enabled=True), client).align(
        [entity()], "正文", "摘要", recall))[0]
    assert (decision["action"], decision["status"], decision["reason"]) == ("create", "pending", f"{stage}_error")


async def test_cancellation_propagates():
    aligner = ConceptAligner(AlignmentConfig(enabled=True))
    with pytest.raises(asyncio.CancelledError):
        await aligner.align([entity()], "正文", "摘要", AsyncMock(side_effect=asyncio.CancelledError))


async def test_total_time_budget_and_count_budget_are_bounded():
    async def slow_recall(*args):
        await asyncio.Event().wait()

    config = replace(AlignmentConfig(enabled=True), total_timeout_seconds=0.01)
    recall = AsyncMock(side_effect=slow_recall)
    decisions = await ConceptAligner(config).align([entity(), entity("另一个", "c2")], "正文", "摘要", recall)
    assert [d["reason"] for d in decisions] == ["recall_error", "budget_exceeded"]
    assert recall.await_count == 1
    config = replace(config, total_timeout_seconds=60, max_concepts=1)
    recall = AsyncMock(return_value=[])
    decisions = await ConceptAligner(config).align([entity(), entity("另一个", "c2")], "正文", "摘要", recall)
    assert decisions[1]["reason"] == "budget_exceeded"
    assert recall.await_count == 1


@pytest.mark.parametrize("values", [
    {"same_probability": 0.5}, {"different_probability": float("nan")}, {"top_k": 0},
    {"enabled": "true"}, {"timeout_seconds": float("inf")}, {"max_concepts": True},
])
def test_invalid_configuration_rejected(values):
    with pytest.raises(ValueError):
        AlignmentConfig.from_dict(values)


@pytest.mark.parametrize("explicit_tracker", [False, True])
async def test_real_http_schema_auth_and_usage_with_mock_transport(monkeypatch, explicit_tracker):
    monkeypatch.setenv("SYNTHETIC_JEV_KEY", "synthetic-key")
    record_usage = AsyncMock()
    tracker = SimpleNamespace(record_usage=record_usage)

    def default_tracker():
        assert not explicit_tracker, "注入临时 tracker 后不能访问默认用量库"
        return tracker

    monkeypatch.setattr(align_mod, "UsageTracker", default_tracker)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=dict(response(answer()), usage={"input_tokens": 123, "output_tokens": 18}))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(align_mod.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    client = JevClient(replace(AlignmentConfig(), api_key_env="SYNTHETIC_JEV_KEY"),
                       usage_tracker=tracker if explicit_tracker else None)
    data = await client.evaluate({"name": "检索增强生成"}, [candidate()])
    assert data["model"] == "jev-synthetic"
    request = seen[0]
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer synthetic-key"
    payload = json.loads(request.content)
    assert payload["questions"]["pair_0"]["type"] == "score"
    assert len(payload["questions"]["pair_0"]["criteria"]) == 3
    assert "candidates[0]" in payload["questions"]["pair_0"]["instructions"]
    record_usage.assert_awaited_once_with(prompt_tokens=123, completion_tokens=18, total_tokens=141,
                                         platform="memory_alignment", model="jev-synthetic")


async def test_missing_key_does_not_attempt_network(monkeypatch):
    monkeypatch.delenv("SYNTHETIC_MISSING_KEY", raising=False)
    client = JevClient(replace(AlignmentConfig(), api_key_env="SYNTHETIC_MISSING_KEY"))
    with pytest.raises(ValueError, match="missing_api_key"):
        await client.evaluate({}, [candidate()])


async def test_http_failure_does_not_expose_provider_body(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_JEV_KEY", "synthetic-key")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(align_mod.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, text="private request echo")), **kw))
    with pytest.raises(ValueError, match="^jev_http_429$"):
        await JevClient(replace(AlignmentConfig(), api_key_env="SYNTHETIC_JEV_KEY")).evaluate({}, [candidate()])


async def test_jev_network_has_its_own_total_timeout(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_JEV_KEY", "synthetic-key")
    real_client = httpx.AsyncClient

    async def handler(request):
        await asyncio.Event().wait()

    monkeypatch.setattr(align_mod.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    config = replace(AlignmentConfig(), api_key_env="SYNTHETIC_JEV_KEY", timeout_seconds=0.01)
    with pytest.raises(TimeoutError):
        await JevClient(config).evaluate({}, [candidate()])


class Rows:
    def __init__(self, rows=()):
        self.rows = rows

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for row in self.rows:
            yield row


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(LadybugStore, "_instance", None)
    obj = LadybugStore()
    obj._initialized = True
    obj._save_lock = asyncio.Lock()
    obj._ensure_indexes = AsyncMock()
    return obj


async def test_save_lock_covers_recall_until_commit(store):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def save(content, *args):
        calls.append(content)
        entered.set()
        await release.wait()
        return "event_id"

    store._save_memory = save
    first = asyncio.create_task(store.save_memory("first", "s", {}))
    await entered.wait()
    second = asyncio.create_task(store.save_memory("second", "s", {}))
    await asyncio.sleep(0)
    assert calls == ["first"]
    release.set()
    await asyncio.gather(first, second)
    assert calls == ["first", "second"]


class Session:
    def __init__(self, run=None, execute_write=None):
        self.run = run
        self.execute_write = execute_write

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("include_report", [True, False])
async def test_save_pipeline_persists_audit_without_requiring_a_return_report(store, enabled, include_report, tmp_path):
    from memory.concept_audit import ConceptAuditLog

    store.concept_audit = ConceptAuditLog(tmp_path / "audit.db")
    store.alignment_config = AlignmentConfig(enabled=enabled)
    store.id_generator = SimpleNamespace(next_id=AsyncMock(side_effect=["event_1", "concept_1"]))
    store.embedding_client = SimpleNamespace(
        embed=AsyncMock(return_value=[[0, 1], [1, 0], [1, 0]]), model_name="synthetic", dimensions=2)
    decision = {"mention": "RAG", "target_id": "concept_existing", "action": "join"}
    store.concept_aligner = SimpleNamespace(align=AsyncMock(return_value=[decision]))
    execute = AsyncMock()
    store.driver = SimpleNamespace(session=lambda: Session(execute_write=execute))
    report = [] if include_report else None
    extraction = {"event": {"summary": "技术讨论"}, "entities": {"concepts": ["RAG"]}, "relationships": []}
    result = await store.save_memory("合成原始摘要", "s", extraction, report)
    assert result == "event_1"
    args = execute.await_args.kwargs
    assert args["source_records"][0]["text"] == "合成原始摘要"
    assert args["source_records"][0]["embedding_model"] == "synthetic"
    if enabled:
        assert args["entity_records"][0]["alignment"] == decision
        assert json.loads(args["alignment_json"]) == [decision]
        assert report == ([decision] if include_report else None)
    else:
        store.concept_aligner.align.assert_not_awaited()
        assert args["alignment_json"] is None
        assert report == ([] if include_report else None)
    execute.side_effect = RuntimeError("synthetic commit failure")
    rows = (await store.concept_audit.list())["entries"]
    assert len(rows) == (1 if enabled else 0)
    if enabled:
        assert rows[0]["status"] == "succeeded" and rows[0]["data"]["event_id"] == "event_1"
    store.id_generator.next_id.side_effect = ["event_2", "concept_2"]
    report = []
    with pytest.raises(RuntimeError):
        await store.save_memory("正文", "s", extraction, report)
    assert report == []
    if enabled:
        rows = (await store.concept_audit.list())["entries"]
        assert rows[0]["status"] == "unknown"
        assert rows[0]["data"]["error_type"] == "RuntimeError"


async def test_recall_combines_exact_alias_and_vector_candidates_with_bounded_context(store):
    store.dimensions = 2
    nodes = [{"n": {"id": node_id, "name": name, "first_context": "context"}} for node_id, name in [("c1", "RAG"), ("c2", "其他")]]
    store._query = AsyncMock(side_effect=[nodes, [{"id": "c2", "group_id": "c2"}, {"id": "c1", "group_id": "c1"}],
        [{"summary": "A" * 400}], [{"summary": "B"}]])
    candidates = await store._recall_concepts("RAG", [1, 0], 5)
    assert [c["id"] for c in candidates] == ["c1", "c2"]
    assert candidates[0]["events"] == ["A" * 300]
    assert candidates[1]["events"] == ["B"]


async def test_recall_failure_and_truncated_exact_candidates_are_not_empty_success(store):
    store.driver = SimpleNamespace(session=lambda: Session(run=AsyncMock(side_effect=ConnectionError)))
    with pytest.raises(ConnectionError):
        await store._recall_concepts("RAG", [1, 0], 5)
    run = AsyncMock(return_value=Rows([{"n": {"id": "c1", "name": "RAG"}}, {"n": {"id": "c2", "name": "RAG"}}]))
    store.driver = SimpleNamespace(session=lambda: Session(run=run))
    with pytest.raises(ValueError, match="ambiguous"):
        await store._recall_concepts("RAG", [1, 0], 1)
    assert run.await_count == 1


async def test_graph_distinguishes_homonymous_concepts(store):
    nodes = [{"labels": ["Concept"], "id": cid, "name": "苹果", "type": None, "summary": None,
              "content": None, "create_time": None} for cid in ("concept_fruit", "concept_company")]
    run = AsyncMock(side_effect=[Rows([{"n": {"_label": "Concept", **n}} for n in nodes]), Rows()])
    store.driver = SimpleNamespace(session=lambda: Session(run=run))
    graph = await store.fetch_graph()
    assert len({node["id"] for node in graph["nodes"]}) == 2


async def test_save_tool_returns_only_save_acknowledgement(monkeypatch):
    import tools.memory_database_save as save_mod

    monkeypatch.setattr(save_mod, "_session_manager", SimpleNamespace(
        current_session=SimpleNamespace(session_id="synthetic_session")))
    monkeypatch.setattr(save_mod, "MemoryExtractor", lambda: SimpleNamespace(extract=AsyncMock(return_value={})))

    save = AsyncMock(return_value="event_1")
    monkeypatch.setattr(save_mod, "LadybugStore", lambda: SimpleNamespace(save_memory=save))
    result = json.loads(await save_mod.memory_database_save({"content": " 合成正文 "}))
    assert result == {"status": "ok", "event_id": "event_1"}
    save.assert_awaited_once_with("合成正文", "synthetic_session", {})
