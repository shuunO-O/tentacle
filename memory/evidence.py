"""精确原文摘录、概念追查入口及紧凑呈现；不调用模型或推断关系。

移植自 dsh-neo4j-memory/src/evidence.ts；MIT 许可及版权声明见 memory/retrieval.py。
"""

import json
import math
import re
from dataclasses import dataclass

from memory.retrieval import canonical, terms


EVENTS = 4
CHAIN_EVENTS = 20
CONCEPTS_PER_EVENT = 64
BRANCHES = 6
EXCERPT = 120
QUOTE_BUDGET = 1000
HISTORY_IDS = 640
STATES = 32
TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class SearchSettings:
    event_top_k: int = 8
    content_mode: str = "excerpt"
    include_index: bool = True
    index_top_k: int = 24

    @classmethod
    def from_config(cls, raw):
        values = {key: raw.get(key, field.default) for key, field in cls.__dataclass_fields__.items()}
        for key, low, high in (("event_top_k", 1, 20), ("index_top_k", 0, 64)):
            if type(values[key]) is not int or not low <= values[key] <= high:
                raise ValueError(f"memory.search.{key} 必须为 {low}..{high} 的整数")
        if values["content_mode"] not in ("excerpt", "full"):
            raise ValueError("memory.search.content_mode 必须为 excerpt 或 full")
        if type(values["include_index"]) is not bool:
            raise ValueError("memory.search.include_index 必须为布尔值")
        return cls(**values)


def resolve_event_reference(value, delivered):
    match = re.fullmatch(r"(?:Event:)?(\d+)", value or "")
    if not match:
        return value
    candidate = "event_" + match[1]
    if candidate not in delivered:
        raise ValueError("数字简写仅适用于本会话已交付事件，请使用完整 event_id")
    return candidate


def evidence_excerpt(content: str, query: str, summary: str, budget: int) -> dict | None:
    if not content or budget < 1:
        return None
    size = min(EXCERPT, budget, len(content))
    query_terms = {t for t in terms(query) if len(t) >= 2}
    summary_terms = {t for t in terms(summary) if len(t) >= 2}
    starts = {0, max(0, len(content) - size)}
    for term in query_terms | summary_terms:
        for index, match in enumerate(re.finditer(re.escape(term), content, re.I)):
            if index == 3:
                break
            starts.add(max(0, min(len(content) - size, match.start() - int(size * 0.35))))

    def score(start):
        text = canonical(content[start:start + size])
        return sum(t in text for t in query_terms) + 0.5 * sum(t in text for t in summary_terms)

    start = min(starts, key=lambda offset: (-score(offset), offset))
    return {"range": [start, start + size], "text": content[start:start + size]}


def build_evidence(events: list[dict], links: list[dict], ids: list[str], query: str,
                   scope: str, limited: list[str] | None = None, *, related=(), content_mode="excerpt") -> dict:
    rows, groups, budget = [], {}, QUOTE_BUDGET
    seen, added = set(ids), {e["id"] for e in events}
    for event in events:
        content = event.get("content") or ""
        match = event.get("matched_source")
        valid = (isinstance(match, dict) and isinstance(match.get("text"), str)
                 and type(match.get("start")) is int and type(match.get("end")) is int
                 and 0 <= match["start"] < match["end"] <= event.get("content_length", len(content))
                 and match["end"] - match["start"] == len(match["text"])
                 and (match["end"] > len(content) or content[match["start"]:match["end"]] == match["text"]))
        if content_mode == "full":
            quote = {"range": [0, len(content)], "text": content} if content else None
        elif valid:
            quote = evidence_excerpt(match["text"], query, "", min(budget, QUOTE_BUDGET // max(1, len(events))))
            if quote:
                quote["range"] = [offset + match["start"] for offset in quote["range"]]
        else:
            quote = evidence_excerpt(content, query, event.get("summary") or "", budget)
        row = {"id": event["id"], "summary": event.get("summary") or ""}
        row.update({key: event[key] for key in ("create_time", "raw_session") if event.get(key) is not None})
        if quote:
            row["quote"] = quote
            if content_mode != "full":
                budget -= len(quote["text"])
        else:
            row["source"] = "read" if event.get("content_length") else "missing"
        if event.get("via"):
            row["via"] = event["via"]
        if event.get("content_length"):
            row["read"] = min(10000, event["content_length"])
            row["total"] = event["content_length"]
        rows.append(row)
    for link in links:
        if link["event_id"] not in seen or link["degree"] <= 1:
            continue
        group = groups.setdefault(link["concept_id"], {**link, "events": []})
        if link["event_id"] not in group["events"]:
            group["events"].append(link["event_id"])
    shared = [{"id": key, "concept": g["name"], "events": g["events"], "degree": g["degree"]}
              for key, g in groups.items() if len(g["events"]) > 1 and added.intersection(g["events"])]
    concepts = []
    for key, group in groups.items():
        if group["degree"] > len(group["events"]):
            concepts.append({"id": key, "name": group["name"], "from": group["events"][0],
                             "remaining": group["degree"] - len(group["events"]),
                             "priority": 0.6 * max(0, group.get("score") or 0) + 0.4 / math.log2(2 + group["degree"])})
    concepts.sort(key=lambda c: (-c["priority"], c["id"]))
    concepts = [{k: v for k, v in c.items() if k != "priority"} for c in concepts[:BRANCHES]]
    return {"kind": "evidence", "scope": scope, "seen": len(ids), "events": rows, "shared": shared,
            "related": [{"id": e["id"], "summary": (e.get("summary") or "")[:100],
                         **{key: e[key] for key in ("create_time", "raw_session") if e.get(key) is not None}} for e in related],
            "next": {"concepts": concepts}, "limited": limited or []}


def _event_metadata(event: dict) -> str:
    parts = []
    if event.get("create_time") is not None:
        parts.append("create_time(保存时间)=" + json.dumps(event["create_time"], ensure_ascii=False))
    if event.get("raw_session") is not None:
        parts.append("raw_session=" + json.dumps(event["raw_session"], ensure_ascii=False))
    return " | ".join(parts)


def render_evidence(data: dict) -> str:
    if data["kind"] == "source_matches":
        lines = [f'{data["id"]} 原文语义定位 / 共{data["total"]}字符（相关性分数不是事实置信度）']
        for part in data["matches"]:
            lines.append(f'原文[{part["range"][0]}:{part["range"][1]}) score={part["score"]:.4f}: {json.dumps(part["text"], ensure_ascii=False)}')
        if not data["matches"]:
            lines.append("没有符合条件的片段；不代表原文不存在答案，可分页读取核实。")
        return "\n".join(lines)
    if data["kind"] == "source":
        start, end = data["range"]
        output = f'{data["id"]} 原文[{start}:{end}) / 共{data["total"]}字符: {json.dumps(data["text"], ensure_ascii=False)}'
        if data["total"] == 0:
            output += "（缺原文）"
        if "next" in data:
            output += f'\n续读：operation=read event_id={data["id"]} start={data["next"]}'
        return output
    lines = [f'匹配:{len(data["events"])}（{"本次新增" if data["scope"] == "added" else "事件"}，追查累计{data["seen"]}条）',
             "摘要待原文核实；原文范围[起点:终点)按Unicode码点计。共享概念不代表因果或发生顺序。"]
    for event in data["events"]:
        lines.append(f'{event["id"]}{" [沿图补充]" if event.get("via") else ""} | {event["summary"]}')
        metadata = _event_metadata(event)
        if metadata:
            lines.append(metadata)
        if event.get("quote_ref"):
            ref = event["quote_ref"]
            lines.append(f'原文同 {ref["id"]} 原文[{ref["range"][0]}:{ref["range"][1]})（此前已交付）')
        elif event.get("quote"):
            quote = event["quote"]
            lines.append(f'原文[{quote["range"][0]}:{quote["range"][1]}): {json.dumps(quote["text"], ensure_ascii=False)}')
        else:
            lines.append("缺原文" if event["source"] == "missing" else "原文按需读")
        if event.get("read"):
            suffix = f'（共{event["total"]}字符，可分段）' if event["total"] > 10000 else ""
            lines.append(f'read event_id={event["id"]} limit={event["read"]}{suffix}')
    if data.get("related"):
        lines.append("候选索引（仅摘要，原文按需read，不占追查名额）：")
        for event in data["related"]:
            lines.append(f'{event["id"]} | {event["summary"]}')
            metadata = _event_metadata(event)
            if metadata:
                lines.append(metadata)
    if data["shared"]:
        lines.append("共享概念：")
        lines.extend(f'{g["id"]} | {g["concept"]}（全库{g["degree"]}条）：{",".join(g["events"])}' for g in data["shared"])
    lines.append("读取原文：operation=read event_id=<id>，可用start/limit分页。")
    if data.get("cursor"):
        lines.append(f'追查 cursor={data["cursor"]}（15分钟内有效；operation=expand concept_id=<下列入口>）')
        lines.extend(f'{c["id"]} | {c["from"]}→{c["name"]} | 全库未展示{c["remaining"]}条'
                     for c in data["next"]["concepts"])
    if data.get("limited"):
        lines.append("范围限制：" + "；".join(data["limited"]))
    return "\n".join(lines)
