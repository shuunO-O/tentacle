"""临时审计库与模拟 HTTP；不调用真实 Jev，也不读取应用数据。"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import memory.concept_aligner as align_mod
from memory.concept_aligner import RULE_VERSION, AlignmentConfig, JevClient
from memory.concept_audit import ConceptAuditLog


async def test_audit_is_durable_classified_and_cannot_overwrite_finished_history(tmp_path):
    audit = ConceptAuditLog(tmp_path / "audit.db")
    evidence = {"incoming": {"name": "SillyTavern"}, "rule_version": RULE_VERSION}
    entry_id = await audit.begin(actor="automatic", source="history", action="evaluate", data=evidence)
    evidence["incoming"]["name"] = "changed later"
    await audit.finish(entry_id, "succeeded", {"scores": [{"probabilities": {"0": 0, "1": 0, "2": 1}}]})
    manual_id = await audit.begin(actor="manual", source="history", action="merge", data={}, parent_id=entry_id)
    restarted = ConceptAuditLog(audit.path)
    rows = await restarted.list("automatic")
    assert rows["total"] == 1 and rows["entries"][0]["data"]["incoming"]["name"] == "SillyTavern"
    assert rows["counts"] == {"automatic": 1, "manual": 1}
    assert (await restarted.list("manual"))["entries"][0]["status"] == "running"
    assert (await restarted.list("manual"))["entries"][0]["parent_id"] == entry_id
    with pytest.raises(ValueError):
        await audit.finish(entry_id, "failed", {"replacement": True})
    await audit.finish(manual_id, "unknown", {"error_type": "ConnectionError"})
    assert (await restarted.list("manual"))["entries"][0]["status"] == "unknown"


async def test_audit_pagination_and_invalid_filters(tmp_path):
    audit = ConceptAuditLog(tmp_path / "audit.db")
    for index in range(32):
        await audit.begin(actor="automatic", source="history", action="evaluate", data={"index": index})
    first, second = await audit.list(), await audit.list(offset=30)
    assert first["total"] == 32 and len(first["entries"]) == 30 and len(second["entries"]) == 2
    assert not {row["id"] for row in first["entries"]} & {row["id"] for row in second["entries"]}
    with pytest.raises(ValueError):
        await audit.list(actor="unsupported")
    with pytest.raises(ValueError):
        await audit.list(offset=-1)


def payload():
    return {"model": "synthetic-jev", "answers": {"pair_0": {
        "type": "score", "score": 1.97, "confidence": 0.9,
        "probabilities": {"0": 0.01, "1": 0.01, "2": 0.98},
    }}, "usage": {"input_tokens": 10, "output_tokens": 2}}


def client_with_transport(monkeypatch, audit, handler, **config):
    monkeypatch.setenv("SYNTHETIC_AUDIT_JEV_KEY", "do-not-store-this-key")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(align_mod.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return JevClient(replace(AlignmentConfig(), api_key_env="SYNTHETIC_AUDIT_JEV_KEY", **config),
                     usage_tracker=SimpleNamespace(record_usage=AsyncMock()), audit_log=audit, source="history")


async def test_new_prompt_and_each_evaluation_result_are_audited_without_credentials(monkeypatch, tmp_path):
    audit = ConceptAuditLog(tmp_path / "audit.db")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload())

    client = client_with_transport(monkeypatch, audit, handler)
    result = await client.evaluate({"name": "系统提示词", "events": ["合成语境"]}, [{"id": "c2", "name": "项目提示词文件"}])
    instructions = requests[0]["questions"]["pair_0"]["instructions"]
    assert "memory retrieval entry" in instructions
    assert "Do not require exact dictionary equivalence" in instructions
    assert len(requests[0]["questions"]["pair_0"]["criteria"]) == 3
    entry = (await audit.list())["entries"][0]
    assert entry["id"] == result["_audit_id"] and entry["status"] == "succeeded"
    assert entry["data"]["scores"][0]["probabilities"]["2"] == 0.98
    assert entry["data"]["rule_version"] == RULE_VERSION
    assert entry["data"]["incoming"]["events"] == ["合成语境"]
    assert "do-not-store-this-key" not in json.dumps(entry)


@pytest.mark.parametrize("kind", ["http_error", "malformed", "timeout", "cancel"])
async def test_failed_or_cancelled_evaluations_leave_safe_audit_and_do_not_retry(monkeypatch, tmp_path, kind):
    audit = ConceptAuditLog(tmp_path / "audit.db")
    entered = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        if kind in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        if kind == "http_error":
            return httpx.Response(429, text="PRIVATE_PROVIDER_ECHO")
        return httpx.Response(200, json={"model": "synthetic-jev", "answers": {}})

    client = client_with_transport(monkeypatch, audit, handler, timeout_seconds=0.02 if kind == "timeout" else 15)
    task = asyncio.create_task(client.evaluate({"name": "synthetic"}, [{"id": "c2"}]))
    await entered.wait()
    if kind == "cancel":
        task.cancel()
    with pytest.raises((ValueError, KeyError, TimeoutError, asyncio.CancelledError)):
        await task
    entry = (await audit.list())["entries"][0]
    assert entry["status"] == ("cancelled" if kind == "cancel" else "failed")
    assert len(calls) == 1
    assert "PRIVATE_PROVIDER_ECHO" not in json.dumps(entry)
    assert "do-not-store-this-key" not in json.dumps(entry)


async def test_unavailable_audit_prevents_unaudited_model_request(monkeypatch):
    handler = AsyncMock()
    audit = SimpleNamespace(begin=AsyncMock(side_effect=PermissionError("synthetic")))
    client = client_with_transport(monkeypatch, audit, handler)
    with pytest.raises(PermissionError):
        await client.evaluate({"name": "synthetic"}, [{"id": "c2"}])
    handler.assert_not_awaited()
