"""显式执行的 Jev 合成资料验收；不导入应用入口、不连接 Neo4j。

默认使用 OpenRouter；密钥只从已有环境或 --env-file 中选定变量读取。
用量库位于 TemporaryDirectory，报告只包含合成样例和 API 返回指标。
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memory.concept_aligner import AlignmentConfig, ConceptAligner, JevClient
from runtime.usage import UsageTracker


def candidate(name, context, node_id="concept_candidate"):
    return {"id": node_id, "name": name, "aliases": [], "first_context": context, "events": [context]}


# 预先规定期望，不按模型结果修改标签。ID 和内容均为合成资料。
CASES = [
    {"id": "zh_abbreviation", "names": ["检索增强生成"], "content": "讨论通过检索文档增强语言模型回答的检索增强生成技术。",
     "candidates": [candidate("RAG", "Retrieval-Augmented Generation，检索增强生成技术。")], "expected": ["reuse"]},
    {"id": "zh_translation", "names": ["机器学习"], "content": "学习让计算机从数据中学习规律的机器学习方法。",
     "candidates": [candidate("Machine Learning", "让计算机从数据中学习规律的机器学习领域。")], "expected": ["reuse"]},
    {"id": "zh_synonym", "names": ["番茄"], "content": "晚饭买了番茄，用来做番茄炒鸡蛋。",
     "candidates": [candidate("西红柿", "做西红柿炒鸡蛋用的红色蔬果。")], "expected": ["reuse"]},
    {"id": "broader_narrower", "names": ["长期记忆"], "content": "讨论人脑中可以保存数月乃至数年的长期记忆。",
     "candidates": [candidate("记忆", "人脑编码、存储和提取信息的整体能力，包含短期和长期记忆。")], "expected": ["create"]},
    {"id": "related_topic", "names": ["检索增强生成"], "content": "讨论先检索资料再让大模型生成回答的检索增强生成技术。",
     "candidates": [candidate("向量检索", "通过向量相似度查找相关文档的技术。")], "expected": ["create"]},
    {"id": "category_instance", "names": ["Neo4j"], "content": "使用 Neo4j 这个图数据库产品存储人物和事件关系。",
     "candidates": [candidate("图数据库", "用节点和边存储关系数据的数据库类别。")], "expected": ["create"]},
    {"id": "homonym_different", "names": ["苹果"], "content": "买了两个苹果水果，洗净削皮后吃掉。",
     "candidates": [candidate("苹果", "制造 iPhone 和 Mac 的苹果公司 Apple Inc.。")], "expected": ["create"]},
    {"id": "homonym_same", "names": ["苹果"], "content": "买了两个苹果水果，洗净削皮后吃掉。",
     "candidates": [candidate("苹果", "苹果树结出的可食用水果，可以削皮吃。")], "expected": ["reuse"]},
    {"id": "ambiguous_apple", "names": ["苹果"], "content": "今天聊到了苹果，没有进一步说明。",
     "candidates": [candidate("Apple", "Apple Inc.，制造 iPhone 的科技公司。")], "expected": ["create"]},
    {"id": "one_clear_candidate", "names": ["检索增强生成"], "content": "讨论检索增强生成：检索文档后供语言模型生成回答。",
     "candidates": [candidate("RAG", "Retrieval-Augmented Generation，检索增强生成。", "concept_rag"),
                    candidate("向量检索", "只按向量相似度寻找资料的检索技术。", "concept_search")], "expected": ["reuse"]},
    {"id": "existing_duplicate_conflict", "names": ["检索增强生成"], "content": "讨论检索增强生成技术，也叫 RAG。",
     "candidates": [candidate("RAG", "检索增强生成技术。", "concept_duplicate_a"),
                    candidate("检索增强生成", "RAG 技术。", "concept_duplicate_b")], "expected": ["create"]},
    {"id": "same_batch_synonyms", "names": ["RAG", "检索增强生成"], "content": "讨论检索增强生成技术，其英文缩写是 RAG。",
     "candidates": [], "expected": ["create", "reuse"]},
    {"id": "no_candidates", "names": ["量子纠错"], "content": "学习纠正量子计算中错误的量子纠错方法。",
     "candidates": [], "expected": ["create"]},
    {"id": "different_versions", "names": ["Python 3.13"], "content": "升级到 Python 3.13 这个具体版本。",
     "candidates": [candidate("Python 3.12", "Python 语言的 3.12 版本。")], "expected": ["create"]},
    {"id": "subset_preference", "names": ["喜欢爵士乐"], "content": "喜欢爵士乐，尤其是爵士乐的即兴演奏。",
     "candidates": [candidate("喜欢音乐", "笼统地喜欢音乐，未限定具体音乐类型。")], "expected": ["create"]},
]


class MeasuredClient:
    def __init__(self, client):
        self.client = client
        self.calls = []

    async def evaluate(self, incoming, candidates):
        started = time.monotonic()
        try:
            data = await self.client.evaluate(incoming, candidates)
        except Exception as exc:
            self.calls.append({"seconds": round(time.monotonic() - started, 3), "error_type": type(exc).__name__})
            raise
        self.calls.append({"seconds": round(time.monotonic() - started, 3), "usage": data.get("usage", {}),
                           "model": data.get("model"), "request_id": data.get("id"), "answers": data.get("answers")})
        return data


async def probe(args):
    config = AlignmentConfig.from_dict({
        "enabled": True, "base_url": args.base_url, "api_key_env": args.api_key_env, "model": args.model,
    })
    if args.env_file:
        # 不覆盖其它环境变量，不展开 .env 中的变量引用，不打印配置或密钥。
        key = dotenv_values(args.env_file, interpolate=False).get(args.api_key_env)
        if not key:
            raise ValueError("selected_api_key_missing")
        os.environ[args.api_key_env] = key
    if not os.getenv(args.api_key_env):
        raise ValueError("selected_api_key_missing")
    with tempfile.TemporaryDirectory(prefix="tentacle-concept-probe-") as temp:
        tracker = UsageTracker(str(Path(temp) / "usage.db"))
        client = MeasuredClient(JevClient(config, usage_tracker=tracker))
        aligner = ConceptAligner(config, client)
        results = []
        selected = [c for c in CASES if not args.case or c["id"] in args.case]
        if args.case and set(args.case) - {c["id"] for c in CASES}:
            raise ValueError("unknown_case_id")
        for case in selected:
            async def recall(name, vector, top_k):
                return case["candidates"]

            records = [{"name": name, "id": f"concept_synthetic_{i}", "embedding": [1.0, 0.0]}
                       for i, name in enumerate(case["names"])]
            start_call = len(client.calls)
            decisions = await aligner.align(records, case["content"], case["content"], recall)
            actions = [d["action"] for d in decisions]
            errors = [d for d in decisions if d["reason"] in {"jev_error", "recall_error", "budget_exceeded"}]
            passed = actions == case["expected"] and not errors
            results.append({"id": case["id"], "input": case, "expected_actions": case["expected"],
                            "passed": passed, "decisions": decisions, "calls": client.calls[start_call:]})
            print(json.dumps({"case": case["id"], "passed": passed, "actions": actions,
                              "reasons": [d["reason"] for d in decisions]}, ensure_ascii=False), flush=True)
            # 鉴权/限额等系统性错误不继续消耗请求；结果不足时不能算整套通过。
            if errors and errors[0].get("error_code") in {"missing_api_key", "jev_http_401", "jev_http_402", "jev_http_403"}:
                break
        total_cost = sum(c.get("usage", {}).get("cost", 0) or 0 for c in client.calls)
        summary = {"cases": len(results), "planned_cases": len(selected), "passed": sum(r["passed"] for r in results),
                   "requests": len(client.calls), "cost_usd": total_cost,
                   "input_tokens": sum(c.get("usage", {}).get("input_tokens", 0) or 0 for c in client.calls),
                   "output_tokens": sum(c.get("usage", {}).get("output_tokens", 0) or 0 for c in client.calls),
                   "models": sorted({c["model"] for c in client.calls if c.get("model")}),
                   "api_errors": sum("error_type" in c for c in client.calls),
                   "alignment_errors": sum(d["reason"] in {"jev_error", "recall_error", "budget_exceeded"}
                                           for r in results for d in r["decisions"])}
        report = {"created_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
                  "endpoint": config.base_url + "/systemone", "model": config.model,
                  "same_probability": config.same_probability, "different_probability": config.different_probability,
                  "results": results}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0 if summary["passed"] == len(selected) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="只取 api-key-env 指定变量，不输出文件内容")
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--model", default="typesafe/jev-1.13")
    parser.add_argument("--case", action="append", help="仅运行指定合成 case ID，可重复")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(probe(args))
    except Exception as exc:
        print(json.dumps({"probe_error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
