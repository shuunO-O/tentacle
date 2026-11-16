"""事件原文分块与双路融合重排；纯函数，不访问配置、模型或数据库。

算法移植自 dsh-neo4j-memory/src/retrieval.ts（MIT），不包含小说章节启发式。

The retrieval and evidence algorithms are adapted from dsh-neo4j-memory.
Copyright (c) 2026 shuunO-O

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import hashlib
import math
import re
import unicodedata


CANDIDATES = 40
RRF_K = 60
MMR_LAMBDA = 0.85
KEYWORD_BONUS = 0.10
SOURCE_WINDOW = 32000
_WORDS = re.compile(r"[\u3400-\u9fff\U00020000-\U0003134f]+|[a-z0-9_]+")


def canonical(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def terms(text: str) -> set[str]:
    result = set()
    for word in _WORDS.findall(canonical(text)):
        if len(word) > 2 and ord(word[0]) >= 0x3400:
            result.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.add(word)
    return result


def source_chunks(content: str) -> list[dict]:
    """800 个 Unicode 码点一块，重叠 120；范围与 Python 切片一致。"""
    chunks = []
    for start in range(0, len(content), 680):
        end = min(len(content), start + 800)
        text = content[start:end]
        chunks.append({"start": start, "end": end, "text": text,
                       "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        if end == len(content):
            break
    return chunks


def cosine(a: list, b: list) -> float:
    if not a or len(a) != len(b):
        return 0.0
    aa, bb = sum(x * x for x in a), sum(x * x for x in b)
    return sum(x * y for x, y in zip(a, b)) / math.sqrt(aa * bb) if aa and bb else 0.0


def rerank_events(routes: list[list[dict]], query: str, limit: int) -> list[dict]:
    """RRF 融合独立排名，再用原文去重与 MMR 选择；不修改候选记录。"""
    fused = {}
    for route in routes:
        seen = set()
        for event in route:
            node_id = event["id"]
            if node_id in seen:
                continue
            seen.add(node_id)
            candidate = fused.setdefault(node_id, {"event": event, "relevance": 0.0})
            candidate["relevance"] += 1 / (RRF_K + len(seen))
    query_terms = terms(query)
    for candidate in fused.values():
        event = candidate["event"]
        content = event.get("content") or ""
        text = canonical((event.get("summary") or "") + "\n" + content)
        coverage = sum(t in text for t in query_terms) / len(query_terms) if query_terms else 0
        candidate["relevance"] = candidate["relevance"] / (2 / (RRF_K + 1)) + KEYWORD_BONUS * coverage
        # 截断前缀相同不能证明两份完整原文相同。
        signature = event["id"] if event.get("content_length", len(content)) > len(content) else content or event["id"]
        candidate["signature"] = hashlib.sha256(signature.encode("utf-8")).hexdigest()
    selected, signatures = [], set()
    while len(selected) < limit:
        best, best_key = None, None
        for candidate in fused.values():
            if candidate["signature"] in signatures:
                continue
            event = candidate["event"]
            redundancy = 0.0
            for chosen in selected:
                a, b = event.get("source_hashes") or [], chosen.get("source_hashes") or []
                overlap = len(set(a) & set(b)) / min(len(a), len(b)) if a and b else 0.0
                redundancy = max(redundancy, cosine(event.get("vector") or [], chosen.get("vector") or []), overlap)
            score = MMR_LAMBDA * candidate["relevance"] - (1 - MMR_LAMBDA) * redundancy
            key = (-score, event["id"])
            if best_key is None or key < best_key:
                best, best_key = candidate, key
        if best is None:
            break
        selected.append(best["event"])
        signatures.add(best["signature"])
    return selected
