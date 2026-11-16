"""原文索引及检索算法的合成回归；不访问任何配置或模型。"""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memory.embeddings import EmbeddingClient
from memory.evidence import build_evidence, evidence_excerpt
from memory.retrieval import rerank_events, source_chunks


def event(node_id, text, vector=None, **extra):
    return {"id": node_id, "summary": "摘要", "content": text, "content_length": len(text),
            "vector": vector or [1.0, 0.0], "source_hashes": [], **extra}


def test_source_chunks_cover_unicode_and_overlap_without_losing_suffix():
    content = "😀甲" * 1100
    chunks = source_chunks(content)
    assert [(c["start"], c["end"]) for c in chunks] == [(0, 800), (680, 1480), (1360, 2160), (2040, 2200)]
    assert all(content[c["start"]:c["end"]] == c["text"] for c in chunks)
    assert chunks[0]["text"][-120:] == chunks[1]["text"][:120]
    assert source_chunks("") == []
    assert len(source_chunks("甲" * 800)) == 1


def test_source_only_candidates_survive_and_identical_full_sources_are_deduplicated():
    a, b, c = event("a", "相同来源"), event("b", "相同来源"), event("c", "地下暗号", [0.0, 1.0])
    routes = [[a, b], [c, a, b]]
    snapshot = copy.deepcopy(routes)
    result = rerank_events(routes, "地下暗号", 4)
    assert {e["id"] for e in result} == {"a", "c"}
    assert routes == snapshot


def test_mmr_selects_a_different_source_with_stable_ties():
    a, b, c = event("a", "来源甲"), event("b", "来源乙"), event("c", "来源丙", [0.0, 1.0])
    assert [e["id"] for e in rerank_events([[a, b, c], [a, b, c]], "", 2)] == ["a", "c"]


def test_matching_truncated_prefixes_do_not_prove_duplicate_sources():
    a, b = [event(key, "同样前缀", content_length=100000) for key in ("a", "b")]
    assert len(rerank_events([[a, b]], "", 4)) == 2


def test_source_overlap_penalizes_duplicates_even_if_summary_vectors_differ():
    a = event("a", "甲", source_hashes=["shared"], vector=[1.0, 0.0])
    b = event("b", "乙", source_hashes=["shared"], vector=[0.0, 1.0])
    c = event("c", "丙", source_hashes=["other"], vector=[0.0, 1.0])
    assert [e["id"] for e in rerank_events([[a, b, c]] * 2, "", 2)] == ["a", "c"]


def test_excerpt_offsets_are_exact_unicode_and_preserve_original_qualifiers():
    content = "😀开头" * 80 + "未确认地下暗号是否已经失效。" + "尾部" * 100
    quote = evidence_excerpt(content, "地下暗号是否失效", "摘要", 120)
    start, end = quote["range"]
    assert content[start:end] == quote["text"]
    assert len(quote["text"]) <= 120 and "未确认地下暗号" in quote["text"]


def test_same_named_concepts_stay_separate_and_use_real_global_degree():
    events = [event("a", "来源甲"), event("b", "来源乙")]
    links = [{"event_id": "a", "concept_id": "fruit", "name": "Apple", "degree": 8, "score": 0.8},
             {"event_id": "b", "concept_id": "company", "name": "Apple", "degree": 2, "score": 0.9}]
    result = build_evidence(events, links, ["a", "b"], "", "initial")
    assert result["shared"] == []
    assert {c["id"]: c["remaining"] for c in result["next"]["concepts"]} == {"fruit": 7, "company": 1}


@pytest.mark.parametrize("items", [[], [SimpleNamespace(index=0, embedding=[1.0, 0.0])] * 2,
                                   [SimpleNamespace(index=0, embedding=[float("nan"), 0.0])],
                                   [SimpleNamespace(index=0, embedding=[0.0, 0.0])],
                                   [SimpleNamespace(index=0, embedding=[1.0])]])
async def test_incomplete_or_invalid_embedding_responses_fail_before_writing(items):
    client = object.__new__(EmbeddingClient)
    client._initialized, client.model_name, client.dimensions = True, "synthetic", 2
    client.client = SimpleNamespace(embeddings=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(data=items))))
    with pytest.raises(ValueError):
        await client.embed(["synthetic input"])


async def test_embedding_response_indices_restore_input_order_after_deduplication():
    client = object.__new__(EmbeddingClient)
    client._initialized, client.model_name, client.dimensions = True, "synthetic", 2
    create = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                                                        SimpleNamespace(index=0, embedding=[1.0, 0.0])]))
    client.client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    assert await client.embed(["甲", "乙", "甲"]) == [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]
    assert create.call_count == 1
