"""宿主会话内的证据历史和追查游标；只有成功构建、渲染的结果才占交付记录。"""

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import secrets
import time

from memory.evidence import (
    CHAIN_EVENTS, HISTORY_IDS, STATES, TTL_SECONDS, SearchSettings,
    build_evidence, render_evidence, resolve_event_reference,
)
from memory.retrieval import SOURCE_WINDOW


class EvidenceService:
    def __init__(self, store, *, clock=time.monotonic):
        self.store, self.clock = store, clock
        self.histories, self.cursors, self._locks = OrderedDict(), OrderedDict(), {}

    @asynccontextmanager
    async def _serial(self, owner):
        key = owner if owner is not None else object()
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(key, None)

    def _prune(self):
        now = self.clock()
        for states in (self.histories, self.cursors):
            for key in list(states):
                if states[key]["expires"] <= now:
                    del states[key]

    async def call(self, *, operation: str, owner: str | None, query: str | None = None,
                   filters: dict | None = None, reset: bool = False, event_id: str | None = None,
                   cursor: str | None = None, concept_id: str | None = None,
                   start: int = 0, limit: int = 3, top_k: int = 4, min_score=None) -> str:
        if operation == "read":
            self._prune()
            history = self.histories.get(owner)
            event_id = resolve_event_reference(event_id, history["ids"] if history else [])
            if query:
                return render_evidence(await self.store.semantic_read(event_id, query, top_k, limit, min_score))
            return render_evidence(await self.store.read_evidence(event_id, start, limit))
        async with self._serial(owner):
            settings = self.store.evidence_settings() if hasattr(self.store, "evidence_settings") else SearchSettings()
            related = []
            self._prune()
            old = None if reset or owner is None else self.histories.get(owner)
            delivered = list(old["ids"]) if old else []
            if operation == "expand":
                state = self.cursors.get(cursor)
                if owner is None or not state or state["owner"] != owner:
                    raise ValueError("追查游标已过期或不属于当前会话，请重新检索")
                if state["generation"] != getattr(self.store, "_evidence_generation", 0):
                    raise ValueError("记忆索引已更新（含概念分组变化），请重新检索")
                if state["vector"] is not None and state["model"] != self.store.evidence_model():
                    raise ValueError("向量模型或维度已改变，请重新检索")
                if concept_id not in state["concepts"]:
                    raise ValueError("concept_id 必须选自当前游标提供的追查入口")
                room = CHAIN_EVENTS - len(state["ids"])
                if room <= 0:
                    raise ValueError("本条追查链已达20条事件，请重新检索")
                # 核对全部锚点，删除或重复 ID 不得继续追查。
                await self.store.evidence_links(state["ids"], state["vector"])
                query, filters, vector = state["query"], state["filters"], state["vector"]
                anchors = state["ids"]
                excluded = list(dict.fromkeys(delivered + anchors))
                events = await self.store.search_evidence(vector, filters, query=query or "", excluded=excluded,
                                                         concept_id=concept_id, anchors=anchors,
                                                         limit=min(room, settings.event_top_k, limit))
                cap, scope = min(room, settings.event_top_k, limit), "added"
                model = state["model"]
            else:
                vector, model = await self.store.evidence_vector(query) if query else (None, None)
                filters = filters or {}
                anchors, excluded, cap, scope = [], delivered, settings.event_top_k, "initial"
                index_count = settings.index_top_k if settings.include_index else 0
                candidates = await self.store.search_evidence(vector, filters, query=query or "", excluded=excluded,
                                                              limit=cap + index_count)
                fresh = [e for e in candidates if e["id"] not in excluded]
                if len({e["id"] for e in fresh}) != len(fresh):
                    raise ValueError("事件 ID 不唯一，未确认本次结果")
                events, related = fresh[:cap], fresh[cap:cap + index_count]
            events = [e for e in events if e["id"] not in excluded][:cap]
            if settings.content_mode == "full":
                complete = []
                for event in events:
                    source = await self.store.read_evidence(event["id"], 0, max(1, event.get("content_length", 0)))
                    if (source["total"] != event.get("content_length", 0)
                            or len(source["text"]) != source["total"]
                            or not source["text"].startswith(event.get("content", ""))):
                        raise ValueError("检索期间事件发生变化，请重新检索")
                    complete.append({**event, "content": source["text"]})
                events = complete
            ids = anchors + [e["id"] for e in events]
            if len(set(ids)) != len(ids):
                raise ValueError("事件 ID 不唯一，未确认本次结果")
            links, limited = await self.store.evidence_links(ids, vector)
            warnings = ["每事件最多取64个非叶子概念，连接和入口可能有遗漏"] if limited else []
            if settings.content_mode == "excerpt" and any(e.get("content_length", 0) > SOURCE_WINDOW and not e.get("matched_source") for e in events):
                warnings.append("短摘录只在原文前32000字符选择，其余原文可用read继续读取")
            if vector is not None and any(e.get("content_length") and not e.get("source_hashes") for e in events):
                warnings.append("部分旧记忆尚无原文分块向量，当前使用摘要召回；可运行原文索引补建")
            if delivered and not events:
                warnings.append("本会话已交付事件被排除；空结果不证明记忆不存在，独立新问题可用reset_seen=true")
            if scope == "added":
                events = [{**e, "via": "graph"} for e in events]
            data = build_evidence(events, links, ids, query or "", scope, warnings,
                                  related=related, content_mode=settings.content_mode)
            new_ids = OrderedDict((node_id, None) for node_id in delivered)
            quotes = OrderedDict(old["quotes"]) if old else OrderedDict()
            for row in data["events"]:
                new_ids[row["id"]] = None
                quote = row.get("quote")
                if quote and settings.content_mode != "full":
                    previous = quotes.get(quote["text"])
                    if previous:
                        del row["quote"]
                        row["quote_ref"] = previous
                    else:
                        quotes[quote["text"]] = {"id": row["id"], "range": quote["range"]}
            for history in (new_ids, quotes):
                while len(history) > HISTORY_IDS:
                    history.popitem(last=False)
            token = secrets.token_urlsafe(12) if owner is not None and ids else None
            if token:
                data["cursor"] = token
            rendered = render_evidence(data)
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            # 以下无 await：渲染或数据库失败、任务取消均不消耗去重记录或旧游标。
            if owner is not None:
                if reset:
                    for key in list(self.cursors):
                        if self.cursors[key]["owner"] == owner:
                            del self.cursors[key]
                self.histories.pop(owner, None)
                self.histories[owner] = {"expires": self.clock() + TTL_SECONDS, "ids": new_ids, "quotes": quotes}
                while len(self.histories) > STATES:
                    self.histories.popitem(last=False)
            if token:
                self.cursors[token] = {"owner": owner, "expires": self.clock() + TTL_SECONDS,
                                       "ids": ids, "query": query, "filters": dict(filters), "vector": vector,
                                       "model": model, "concepts": [c["id"] for c in data["next"]["concepts"]],
                                       "generation": getattr(self.store, "_evidence_generation", 0)}
                while len(self.cursors) > STATES:
                    self.cursors.popitem(last=False)
            return rendered


def evidence_service(store) -> EvidenceService:
    service = getattr(store, "_evidence_service", None)
    if service is None:
        service = store._evidence_service = EvidenceService(store)
    return service
