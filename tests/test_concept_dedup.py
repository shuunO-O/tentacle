"""合成图、临时审核库和 ASGI 验证；不启动应用或读取真实记忆。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from memory.concept_aligner import AlignmentConfig, JevError
from memory.concept_dedup import ConceptDedupManager, DedupJournal, canonical_id
from memory.concept_maintenance import CandidateGone, ReviewRequired
from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


def test_canonical_selection_uses_numeric_id_not_lexical_order():
    assert canonical_id("concept_10", "concept_2") == "concept_2"


class Rows:
    def __init__(self, rows=()):
        self.rows = rows

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for row in self.rows:
            yield row


class FakeGraph:
    def __init__(self, conflict=False):
        self.nodes = {"concept_1": "RAG", "concept_2": "检索增强生成"}
        self.conflict = conflict
        self.validate = AsyncMock()
        self.merge = AsyncMock(side_effect=self.apply)

    async def page(self, after="", limit=50):
        return [{"id": k, "name": v, "embedding": [1, 0]} for k, v in sorted(self.nodes.items()) if k > after]

    async def candidates(self, node, top_k):
        return [{"id": k} for k in self.nodes if k != node["id"]] if node["id"] in self.nodes else []

    async def preview(self, left, right):
        if left not in self.nodes or right not in self.nodes:
            raise CandidateGone("已经合并")
        return {"fingerprint": "synthetic-fingerprint", "left": {"id": left, "name": self.nodes[left], "events": ["合成摘要"]},
                "right": {"id": right, "name": self.nodes[right], "events": ["合成摘要"]},
                "conflicts": [{"property": "sentiment", "preferred": "喜欢", "other": "讨厌"}] if self.conflict else []}

    async def apply(self, keep, drop, expected, operation, policy):
        del self.nodes[drop]
        return {"keep_id": keep, "drop_id": drop, "policy": policy, "operation_id": operation}


def model_response(same=0.98, different=0.01, uncertain=0.01):
    return {"model": "synthetic-jev", "answers": {"pair_0": {"type": "score", "score": uncertain + 2 * same,
            "confidence": 0.9, "probabilities": {"0": different, "1": uncertain, "2": same}}}}


def manager(tmp_path, result=None, conflict=False):
    graph = FakeGraph(conflict)
    client = SimpleNamespace(evaluate=AsyncMock(return_value=result or model_response()))
    obj = ConceptDedupManager(tmp_path / "dedup.db", graph=graph, client=client, config=AlignmentConfig())
    return obj, graph, client


async def finish_scan(obj, **options):
    await obj.start(**options)
    await obj._task
    return await obj.status("all")


async def test_manual_start_is_required_and_status_does_not_read_graph(tmp_path):
    obj, graph, client = manager(tmp_path)
    assert not (tmp_path / "dedup.db").exists()
    status = await obj.status()
    assert status["run"] is None
    graph.validate.assert_not_awaited()
    client.evaluate.assert_not_awaited()


async def test_high_confidence_automatically_merges_once_and_persists(tmp_path):
    obj, graph, client = manager(tmp_path)
    status = await finish_scan(obj)
    assert status["run"]["status"] == "completed"
    assert status["counts"] == {"merged": 1}
    assert status["pairs"][0]["data"]["automatic"] is True
    graph.merge.assert_awaited_once()
    assert graph.merge.await_args.args[:2] == ("concept_1", "concept_2")
    client.evaluate.assert_awaited_once()
    other = ConceptDedupManager(tmp_path / "dedup.db", graph=graph, client=client, config=AlignmentConfig())
    assert (await other.status("merged"))["counts"] == {"merged": 1}
    client.evaluate.assert_awaited_once()


@pytest.mark.parametrize("result,conflict", [(model_response(0.1, 0.1, 0.8), False), (model_response(), True)])
async def test_uncertain_or_relationship_conflict_waits_for_human(tmp_path, result, conflict):
    obj, graph, _ = manager(tmp_path, result, conflict)
    status = await finish_scan(obj)
    assert status["counts"] == {"pending": 1}
    graph.merge.assert_not_awaited()
    pair = status["pairs"][0]
    await obj.decide(pair["id"], "merge", "concept_2", "reject")
    assert graph.merge.await_args.args[0:2] == ("concept_2", "concept_1")
    assert graph.merge.await_args.args[-1] == "reject"


async def test_independent_choice_survives_restart_and_is_not_asked_again(tmp_path):
    obj, graph, client = manager(tmp_path, model_response(0.1, 0.1, 0.8))
    status = await finish_scan(obj)
    await obj.decide(status["pairs"][0]["id"], "independent")
    restarted = ConceptDedupManager(tmp_path / "dedup.db", graph=graph, client=client, config=AlignmentConfig())
    status = await finish_scan(restarted)
    assert status["counts"] == {"independent": 1}
    client.evaluate.assert_awaited_once()
    graph.merge.assert_not_awaited()


async def test_unknown_commit_result_can_only_retry_same_operation_and_selection(tmp_path):
    obj, graph, _ = manager(tmp_path, model_response(0.1, 0.1, 0.8))
    status = await finish_scan(obj)
    pair_id = status["pairs"][0]["id"]
    graph.merge.side_effect = ConnectionError("synthetic lost reply")
    with pytest.raises(ReviewRequired, match="待确认"):
        await obj.decide(pair_id, "merge", "concept_2", "reject")
    status = await obj.status()
    operation = status["pairs"][0]["operation"]
    assert status["pairs"][0]["choice"] == {"keep_id": "concept_2", "policy": "reject"}
    with pytest.raises(ReviewRequired):
        await obj.decide(pair_id, "independent")
    with pytest.raises(ReviewRequired):
        await obj.decide(pair_id, "merge", "concept_1")
    graph.merge.side_effect = graph.apply
    await obj.decide(pair_id, "merge", "concept_2", "reject")
    assert graph.merge.await_args.args[3] == operation


async def test_stale_fingerprint_can_be_refreshed_before_human_merge(tmp_path):
    obj, graph, _ = manager(tmp_path, model_response(0.1, 0.1, 0.8))
    status = await finish_scan(obj)
    pair_id = status["pairs"][0]["id"]
    graph.merge.side_effect = ReviewRequired("关系已变化")
    with pytest.raises(ReviewRequired):
        await obj.decide(pair_id, "merge", "concept_1")
    assert (await obj.status())["pairs"][0]["operation"] is None
    await obj.refresh(pair_id)
    graph.merge.side_effect = graph.apply
    await obj.decide(pair_id, "merge", "concept_1")
    assert (await obj.status("merged"))["counts"]["merged"] == 1


async def test_auth_error_stops_scan_and_records_safe_error_code(tmp_path):
    obj, graph, client = manager(tmp_path)
    client.evaluate.side_effect = JevError("jev_http_401")
    status = await finish_scan(obj)
    assert status["run"]["status"] == "failed"
    assert status["pairs"][0]["data"]["error_code"] == "jev_http_401"
    graph.merge.assert_not_awaited()


async def test_unsupported_graph_review_is_visible_instead_of_silently_skipped(tmp_path):
    obj, graph, client = manager(tmp_path)
    graph.preview = AsyncMock(side_effect=ReviewRequired("关联关系超过 5000 条，需在主机另行维护"))
    status = await finish_scan(obj)
    assert status["run"]["status"] == "failed"
    assert "5000" in status["run"]["message"]
    client.evaluate.assert_not_awaited()


async def test_duplicate_start_rejected_and_cancel_never_starts_another_scan(tmp_path):
    import asyncio
    obj, graph, client = manager(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        entered.set()
        await release.wait()
        return model_response()

    client.evaluate.side_effect = slow
    await obj.start()
    await entered.wait()
    with pytest.raises(ReviewRequired, match="已有扫描"):
        await obj.start()
    await obj.cancel()
    release.set()
    await obj._task
    assert (await obj.status())["run"]["status"] == "cancelled"
    graph.merge.assert_not_awaited()


def test_journal_restarts_mark_running_and_inflight_operations_without_replaying(tmp_path):
    db = DedupJournal(tmp_path / "journal.db")
    db.write("INSERT INTO runs(id,status) VALUES('r','running')")
    db.write("INSERT INTO pairs(id,state,operation,choice) VALUES('p','applying','op','choice')")
    recovered = DedupJournal(tmp_path / "journal.db")
    assert recovered.query("SELECT status FROM runs")[0]["status"] == "interrupted"
    pair = recovered.query("SELECT * FROM pairs")[0]
    assert (pair["state"], pair["operation"], pair["choice"]) == ("pending", "op", "choice")


def test_independent_choices_remain_attached_to_original_nodes_after_grouping(tmp_path):
    db = DedupJournal(tmp_path / "journal.db")
    db.write("INSERT INTO independent VALUES('concept_2','concept_3')")
    db.write("INSERT INTO pairs(id,state) VALUES('p','applying')")
    db.finalize("p", {"result": "synthetic"}, "concept_1", "concept_2")
    assert not db.query("SELECT 1 FROM independent WHERE left_id='concept_1' AND right_id='concept_3'")
    assert db.query("SELECT 1 FROM independent WHERE left_id='concept_2' AND right_id='concept_3'")
    assert db.query("SELECT state FROM pairs")[0]["state"] == "merged"


@pytest.fixture
async def web(tmp_path):
    settings = WebSettings(str(tmp_path / "settings.json"))
    app = create_app(ConnectionManager(), None, None, None, None,
                     SimpleNamespace(db_path=tmp_path / "chronicle.db"), settings, None)
    fake = SimpleNamespace(status=AsyncMock(return_value={"run": None, "pairs": [], "counts": {}}),
                           start=AsyncMock(return_value={"run_id": "synthetic"}), cancel=AsyncMock(return_value={"ok": True}),
                           refresh=AsyncMock(return_value={}), reevaluate=AsyncMock(return_value={"state": "pending"}),
                           audit=SimpleNamespace(list=AsyncMock(return_value={"entries": [], "counts": {}, "total": 0})),
                           detach=AsyncMock(return_value={"concept_id": "concept_1", "group_id": "isolated"}),
                           decide=AsyncMock(return_value={"state": "merged"}))
    app.state.concept_dedup = fake
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        yield client, fake, settings


@pytest.mark.parametrize("prefix", ["/api/memory", "/api/neo4j"])
@pytest.mark.parametrize("suffix,body", [
    ("/dedup/start", {}), ("/dedup/cancel", {}),
    ("/dedup/p/refresh", {}), ("/dedup/p/decide", {"action": "merge", "keep_id": "a"}),
    ("/dedup/p/reevaluate", {}),
    ("/concepts/detach", {"concept_id": "Concept:concept_1"}),
])
async def test_dedup_mutations_require_both_auth_layers(web, prefix, suffix, body):
    client, fake, settings = web
    path = prefix + suffix
    settings.set_password("synthetic-password")
    settings.set_device_auth(True)
    assert (await client.post(path, json=body)).status_code in {401, 403}
    token = settings.issue_token()
    client.headers["Authorization"] = "Bearer " + token
    assert (await client.post(path, json=body)).status_code in {401, 403}
    client.cookies.set(DEVICE_COOKIE, settings.create_device("synthetic"))
    assert (await client.post(path, json=body)).status_code == 200


async def test_legacy_memory_clients_reach_ladybug_and_group_without_overwriting_relationships(web, monkeypatch):
    client, fake, settings = web
    store = SimpleNamespace(count_summary=AsyncMock(return_value={"nodes": {"Concept": 2}, "relationships": 1}),
                            fetch_graph=AsyncMock(return_value={"nodes": [], "relationships": []}))
    monkeypatch.setattr("web.server.LadybugStore", lambda: store)
    settings.set_password("synthetic-password")
    settings.set_device_auth(True)
    paths = ["/api/neo4j/summary", "/api/neo4j/graph?labels=Event,Concept", "/api/neo4j/dedup"]
    for path in paths:
        assert (await client.get(path)).status_code in {401, 403}
    store.count_summary.assert_not_awaited()
    store.fetch_graph.assert_not_awaited()

    client.headers["Authorization"] = "Bearer " + settings.issue_token()
    client.cookies.set(DEVICE_COOKIE, settings.create_device("synthetic"))
    for path in paths:
        legacy = await client.get(path)
        current = await client.get(path.replace("/api/neo4j/", "/api/memory/"))
        assert legacy.status_code == current.status_code == 200
        assert legacy.json() == current.json()
    store.fetch_graph.assert_awaited_with(["Event", "Concept"])
    result = await client.post("/api/neo4j/dedup/p/decide",
                               json={"action": "merge", "keep_id": "a", "policy": "keep_source"})
    assert result.status_code == 200
    fake.decide.assert_awaited_once_with("p", "merge", "a", "reject")


async def test_dedup_api_rejects_bad_limits_and_surfaces_stale_review(web):
    client, fake, _ = web
    assert (await client.post("/api/memory/dedup/start", json={"max_pairs": 1001})).status_code == 422
    fake.start.assert_not_awaited()
    fake.decide.side_effect = ReviewRequired("证据已变化")
    response = await client.post("/api/memory/dedup/p/decide", json={"action": "merge", "keep_id": "a"})
    assert response.status_code == 409
    assert response.json()["detail"] == "证据已变化"


async def test_detach_api_validates_input_and_surfaces_failures(web):
    client, fake, _ = web
    for body in ({}, {"concept_id": ""}, {"concept_id": "x" * 501}, {"concept_id": []}):
        assert (await client.post("/api/memory/concepts/detach", json=body)).status_code == 422
    fake.detach.assert_not_awaited()
    response = await client.post("/api/memory/concepts/detach", json={"concept_id": "Concept:concept_1"})
    assert response.status_code == 200 and response.json()["group_id"] == "isolated"
    fake.detach.assert_awaited_once_with("Concept:concept_1")
    fake.detach.side_effect = ValueError("概念不存在")
    assert (await client.post("/api/memory/concepts/detach", json={"concept_id": "missing"})).status_code == 400
    fake.detach.side_effect = ConnectionError("synthetic-secret-not-for-response")
    response = await client.post("/api/memory/concepts/detach", json={"concept_id": "concept_1"})
    assert response.status_code == 503 and "synthetic-secret" not in response.text


async def test_detach_without_model_configuration_records_manual_audit(tmp_path):
    graph = SimpleNamespace(detach=AsyncMock(return_value={
        "concept_id": "concept_1", "group_id": "isolated", "previous_group_id": "concept_2"}))
    obj = ConceptDedupManager(tmp_path / "dedup.db", graph=graph)
    result = await obj.detach("concept_1")
    assert result["previous_group_id"] == "concept_2"
    assert obj.client is None and obj.config is None and obj._task is None
    entries = (await obj.audit.list("manual"))["entries"]
    assert len(entries) == 1 and entries[0]["action"] == "detach" and entries[0]["source"] == "graph"
    assert entries[0]["status"] == "succeeded" and entries[0]["data"]["result"] == result


@pytest.mark.parametrize("error,status", [(ValueError("missing"), "failed"),
                                          (ConnectionError("lost reply"), "unknown")])
async def test_detach_failure_is_audited_and_never_retried(tmp_path, error, status):
    graph = SimpleNamespace(detach=AsyncMock(side_effect=error))
    obj = ConceptDedupManager(tmp_path / "dedup.db", graph=graph)
    with pytest.raises(type(error)):
        await obj.detach("concept_1")
    graph.detach.assert_awaited_once()
    assert (await obj.audit.list())["entries"][0]["status"] == status


async def test_audit_read_requires_both_auth_layers(web):
    client, fake, settings = web
    settings.set_password("synthetic-password")
    settings.set_device_auth(True)
    assert (await client.get("/api/memory/dedup/audit")).status_code in {401, 403}
    client.headers["Authorization"] = "Bearer " + settings.issue_token()
    assert (await client.get("/api/memory/dedup/audit")).status_code in {401, 403}
    client.cookies.set(DEVICE_COOKIE, settings.create_device("synthetic"))
    assert (await client.get("/api/memory/dedup/audit?actor=manual&offset=30")).status_code == 200
    fake.audit.list.assert_awaited_once_with("manual", 30)


async def test_reevaluation_preserves_audit_history_and_never_auto_merges(tmp_path):
    obj, graph, client = manager(tmp_path, model_response(0.1, 0.1, 0.8))
    status = await finish_scan(obj)
    pair_id = status["pairs"][0]["id"]
    before = (await obj.audit.list())["entries"]
    client.evaluate.return_value = model_response()
    assert await obj.reevaluate(pair_id) == {"state": "pending"}
    graph.merge.assert_not_awaited()
    automatic = (await obj.audit.list("automatic"))["entries"]
    assert len([row for row in automatic if row["action"] == "decision"]) == 2
    assert {row["id"] for row in before} <= {row["id"] for row in (await obj.audit.list())["entries"]}
    manual = (await obj.audit.list("manual"))["entries"]
    recheck = next(row for row in manual if row["action"] == "reevaluate")
    assert recheck["status"] == "succeeded" and recheck["data"]["previous"]["reason"] == "uncertain"
    await obj.decide(pair_id, "merge", "concept_1")
    assert any(row["action"] == "merge" and row["status"] == "succeeded"
               for row in (await obj.audit.list("manual"))["entries"])


async def test_auto_merges_and_human_independence_have_separate_audit(tmp_path):
    automatic, _, _ = manager(tmp_path / "automatic")
    await finish_scan(automatic)
    assert any(row["action"] == "merge" and row["status"] == "succeeded"
               for row in (await automatic.audit.list("automatic"))["entries"])
    manual, _, _ = manager(tmp_path / "manual", model_response(0.1, 0.1, 0.8))
    status = await finish_scan(manual)
    await manual.decide(status["pairs"][0]["id"], "independent")
    assert any(row["action"] == "independent" and row["status"] == "succeeded"
               for row in (await manual.audit.list("manual"))["entries"])
