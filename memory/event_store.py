"""事件证据查询与原文索引维护，复用 LadybugStore 的共享数据库、客户端及写锁。"""

import asyncio

from memory.evidence import CONCEPTS_PER_EVENT, EVENTS, SearchSettings
from memory.retrieval import CANDIDATES, SOURCE_WINDOW, rerank_events, source_chunks


class EventEvidenceStore:
    def evidence_settings(self):
        from config.manager import ConfigManager
        return SearchSettings.from_config(ConfigManager().get_memory_config().get("search", {}))

    async def _evidence_query(self, query: str, **params) -> list[dict]:
        async with self.get_read_driver().session() as session:
            result = await session.run(query, params)
            return [dict(row) async for row in result]

    async def evidence_vector(self, query: str) -> tuple[list[float], tuple]:
        if not self._initialized:
            self._init()
        vector = (await self.embedding_client.embed([query]))[0]
        await self._ensure_indexes()
        return vector, self.evidence_model()

    def evidence_model(self) -> tuple:
        return (self.embedding_client.model_name, self.embedding_client.dimensions,
                getattr(self, "_evidence_generation", 0))

    @staticmethod
    def _evidence_where(filters: dict) -> str:
        predicates = ["e.id IS NOT NULL", "NOT e.id IN $excluded"]
        if filters.get("entity_filter"):
            predicates.append("""(EXISTS { MATCH (e)-[r:PARTICIPATED_IN|OCCURRED_AT|MENTIONS|HAS_ATTITUDE]-(m:Person|Location)
                WHERE m.name=$entity_filter OR $entity_filter IN coalesce(m.aliases,[]) }
                OR EXISTS { MATCH (e)-[r:PARTICIPATED_IN|OCCURRED_AT|MENTIONS|HAS_ATTITUDE]-(m:Concept),(named:Concept)
                WHERE coalesce(m.equivalence_group,m.id)=coalesce(named.equivalence_group,named.id)
                AND (named.name=$entity_filter OR $entity_filter IN coalesce(named.aliases,[])) })""")
        if filters.get("session_id"):
            predicates.append("e.raw_session = $session_id")
        if filters.get("start_time"):
            predicates.append("e.create_time >= $start_time")
        if filters.get("end_time"):
            predicates.append("e.create_time < $end_time")
        return " AND ".join(predicates)

    async def search_evidence(self, vector: list | None, filters: dict, *, query: str = "",
                              excluded=(), concept_id: str | None = None,
                              anchors=(), limit: int = EVENTS) -> list[dict]:
        params = {**filters, "excluded": list(excluded), "limit": limit, "vector": vector,
                  "window": SOURCE_WINDOW, "candidates": CANDIDATES, "anchors": list(anchors),
                  "concept_id": concept_id}
        where = self._evidence_where(filters)
        if concept_id:
            where += """ AND EXISTS { MATCH (e)-[:MENTIONS]->(c:Concept),(entry:Concept {id:$concept_id}),
                (a:Event)-[:MENTIONS]->(ac:Concept) WHERE a.id IN $anchors
                AND coalesce(c.equivalence_group,c.id)=coalesce(entry.equivalence_group,entry.id)
                AND coalesce(ac.equivalence_group,ac.id)=coalesce(entry.equivalence_group,entry.id) }"""
        fields = """e.id AS id, coalesce(e.summary,'') AS summary,
            e.create_time AS create_time, e.raw_session AS raw_session,
            coalesce(e.content,'') AS content,
            e.embedding AS vector, collect(h.hash) AS source_hashes"""
        if vector is None:
            records = await self._evidence_query(f"""/* evidence:filter */ MATCH (e:Event) WHERE {where}
                WITH e ORDER BY e.create_time DESC,e.id LIMIT $limit
                OPTIONAL MATCH (e)-[:HAS_SOURCE]->(h:EventSource)
                RETURN {fields} ORDER BY e.create_time DESC,e.id""", **params)
        else:
            model, dimensions, _ = self.evidence_model()
            summary = await self._evidence_query(f"""/* evidence:summary */ MATCH (e:Event) WHERE {where}
                WITH e,{self._similarity('e.embedding')} AS score WHERE score IS NOT NULL
                WITH e,score ORDER BY score DESC,e.id LIMIT $candidates
                OPTIONAL MATCH (e)-[:HAS_SOURCE]->(h:EventSource)
                RETURN {fields},score ORDER BY score DESC,e.id""", **params)
            content = await self._evidence_query(f"""/* evidence:source */ MATCH (e:Event)-[:HAS_SOURCE]->(s:EventSource)
                WHERE {where} AND s.embedding_model=$model AND s.embedding_dim=$dimensions
                WITH e,s,{self._similarity('s.embedding')} AS score WHERE score IS NOT NULL
                WITH e,list_reduce(collect({{score:score,start:s.start,id:s.id,`end`:s.`end`}}),
                    (a,b) -> CASE WHEN b.score>a.score OR (b.score=a.score AND
                        (b.start<a.start OR (b.start=a.start AND b.id<a.id))) THEN b ELSE a END) AS best
                WITH e,best ORDER BY best.score DESC,e.id LIMIT $candidates
                MATCH (matched:EventSource {{id:best.id}})
                OPTIONAL MATCH (e)-[:HAS_SOURCE]->(h:EventSource)
                RETURN {fields},best.score AS score,
                    {{start:best.start,`end`:best.`end`,text:matched.text}} AS matched_source
                ORDER BY score DESC,e.id""",
                **params, model=model, dimensions=dimensions)
            for route in (summary, content):
                for event in route:
                    event.setdefault("content_length", len(event["content"]))
                    event["content"] = event["content"][:SOURCE_WINDOW]
                if len({e["id"] for e in route}) != len(route):
                    raise ValueError("事件 ID 不唯一，本次检索未确认")
            by_id = {e["id"]: e for e in summary}
            for event in content:
                previous = by_id.get(event["id"])
                if previous and (previous["content"] != event["content"]
                                 or previous["content_length"] != event["content_length"]):
                    raise ValueError("检索期间事件发生变化，请重新检索")
                if previous and event.get("matched_source"):
                    previous["matched_source"] = event["matched_source"]
            records = rerank_events([summary, content], query, limit)
        for event in records:
            event.setdefault("content_length", len(event["content"]))
            event["content"] = event["content"][:SOURCE_WINDOW]
        if len({e["id"] for e in records}) != len(records):
            raise ValueError("事件 ID 不唯一，本次检索未确认")
        return records

    async def evidence_links(self, ids: list[str], vector: list | None) -> tuple[list[dict], bool]:
        if not ids:
            return [], False
        found = await self._evidence_query("MATCH (e:Event) WHERE e.id IN $ids RETURN e.id AS id", ids=ids)
        if {r["id"] for r in found} != set(ids):
            raise ValueError("事件已删除，请重新检索")
        score = f"coalesce({self._similarity('member.embedding')},0.0)" if vector is not None else "0.0"
        links = await self._evidence_query(f"""/* evidence:links */
            MATCH (e:Event)-[:MENTIONS]->(c:Concept),(member:Concept)
            WHERE e.id IN $ids AND coalesce(member.equivalence_group,member.id)=coalesce(c.equivalence_group,c.id)
            WITH DISTINCT e,member,coalesce(c.equivalence_group,c.id) AS group_id
            OPTIONAL MATCH (related:Event)-[:MENTIONS]->(member)
            WITH e,group_id,count(DISTINCT related) AS degree,collect(DISTINCT member) AS members
            WHERE degree>1 UNWIND members AS member
            RETURN e.id AS event_id,group_id,member.id AS concept_id,member.name AS name,{score} AS score,degree
            ORDER BY event_id,score DESC,concept_id""", ids=ids, vector=vector)
        grouped = {node_id: {} for node_id in ids}
        for link in links:
            grouped[link["event_id"]].setdefault(link["group_id"], {k: link[k] for k in ("concept_id", "name", "degree", "score")})
        rows = [{"event_id": node_id, "links": list(groups.values())} for node_id, groups in grouped.items()]
        if len(rows) != len(ids) or len({r["event_id"] for r in rows}) != len(ids):
            raise ValueError("事件已删除或 ID 不唯一，请重新检索")
        return ([{"event_id": r["event_id"], **link} for r in rows for link in r["links"][:CONCEPTS_PER_EVENT]],
                any(len(r["links"]) > CONCEPTS_PER_EVENT for r in rows))

    async def read_evidence(self, event_id: str, start: int, limit: int) -> dict:
        rows = await self._evidence_query("""/* evidence:read */ MATCH (e:Event {id:$id})
            RETURN coalesce(e.content,'') AS content LIMIT 2""", id=event_id)
        if len(rows) != 1:
            raise ValueError("事件不存在或 ID 不唯一")
        row = ({"text": rows[0]["content"][start:start + limit], "total": len(rows[0]["content"])}
               if "content" in rows[0] else rows[0])
        if start > row["total"]:
            raise ValueError("原文偏移超出范围")
        end = min(row["total"], start + limit)
        return {"kind": "source", "id": event_id, "range": [start, end], **row,
                **({"next": end} if end < row["total"] else {})}

    async def semantic_read(self, event_id, query, top_k=4, limit=10000, min_score=None):
        # Verify identity before requesting embeddings, including events with no sources.
        source = await self.read_evidence(event_id, 0, 1)
        vector, (model, dimensions, _) = await self.evidence_vector(query)
        rows = await self._evidence_query(f"""/* evidence:semantic-read */
            MATCH (e:Event {{id:$id}})-[:HAS_SOURCE]->(s:EventSource)
            WHERE s.embedding_model=$model AND s.embedding_dim=$dimensions
            WITH s,{self._similarity('s.embedding')} AS score
            WHERE score IS NOT NULL
            RETURN s.start AS start,s.`end` AS `end`,s.text AS text,score
            ORDER BY score DESC,s.start,s.id LIMIT 12""",
            id=event_id, model=model, dimensions=dimensions, vector=vector)
        if not rows:
            raise ValueError("缺少兼容的原文向量，请补建原文索引或使用 start/limit 分页读取")
        selected = [r for r in rows if min_score is None or r["score"] >= min_score][:top_k]
        merged = []
        for row in selected:
            start, end, text = row["start"], row["end"], row["text"]
            if (type(start) is not int or type(end) is not int or not isinstance(text, str)
                    or not 0 <= start < end <= source["total"] or len(text) != end - start):
                raise ValueError("原文分块范围无效，请补建原文索引")
        for row in sorted(selected, key=lambda r: r["start"]):
            start, end, text = row["start"], row["end"], row["text"]
            if merged and start <= merged[-1]["range"][1]:
                previous = merged[-1]
                overlap = min(previous["range"][1], end) - start
                offset = start - previous["range"][0]
                if previous["text"][offset:offset + overlap] != text[:overlap]:
                    raise ValueError("原文分块重叠内容不一致，请补建原文索引")
                if end > previous["range"][1]:
                    previous["text"] += text[overlap:]
                    previous["range"][1] = end
                previous["score"] = max(previous["score"], row["score"])
            else:
                merged.append({"range": [start, end], "text": text, "score": row["score"]})
        # Apply the character budget to the most relevant merged spans first.
        matches = []
        for part in sorted(merged, key=lambda p: (-p["score"], p["range"][0])):
            if limit <= 0:
                break
            text = part["text"][:limit]
            matches.append({**part, "range": [part["range"][0], part["range"][0] + len(text)], "text": text})
            limit -= len(text)
        return {"kind": "source_matches", "id": event_id, "total": source["total"], "matches": matches}

    async def rebuild_event_sources(self) -> dict:
        """显式维护入口：先全部计算，再一次事务替换；不改摘要/概念向量。"""
        if not self._initialized:
            self._init()
        async with self._save_lock:
            rows = await self._evidence_query("""/* evidence:rebuild-snapshot */ MATCH (e:Event)
                RETURN e.id AS id,coalesce(e.content,'') AS content, e.embedding_model AS model,
                       size(e.embedding) AS dimensions ORDER BY e.id""")
            ids = [e["id"] for e in rows]
            if any(not node_id for node_id in ids) or len(set(ids)) != len(ids):
                raise ValueError("事件缺少 ID 或 ID 不唯一，未更新原文索引")
            sources = [{**chunk, "event_id": event["id"], "id": f'{event["id"]}:source:{chunk["start"]}'}
                       for event in rows for chunk in source_chunks(event["content"])]
            vectors = await self.embedding_client.embed([s["text"] for s in sources])
            model, dimensions, _ = self.evidence_model()
            if any((r["model"] and r["model"] != model) or (r["dimensions"] and r["dimensions"] != dimensions) for r in rows):
                raise ValueError("原事件向量模型或维度不同，不能只补建原文索引")
            if len(vectors) != len(sources):
                raise ValueError("原文向量数量不完整，未更新索引")
            for source, vector in zip(sources, vectors):
                source.update(embedding=vector, embedding_model=model, embedding_dim=dimensions)
            await self._ensure_indexes()
            async with self.driver.session() as session:
                await session.execute_write(self._replace_event_sources_tx, rows, sources)
            self._evidence_generation = getattr(self, "_evidence_generation", 0) + 1
            return {"events": len(rows), "chunks": len(sources)}

    @staticmethod
    async def lock_event_sources(tx):
        # 由宿主共享 Database 的写事务持有；独立 CLI 必须在宿主关闭后运行。
        await tx.run("""MERGE (m:MemoryMeta {id:'event_sources'})
            SET m.lock_revision=coalesce(m.lock_revision,0)+1""")

    @staticmethod
    async def _replace_event_sources_tx(tx, snapshot: list[dict], sources: list[dict]):
        await EventEvidenceStore.lock_event_sources(tx)
        # 数据库写锁覆盖所有经本存储写入的进程；再次核对快照以拒绝期间变化。
        result = await tx.run("""MATCH (e:Event) SET e.source_rebuild_lock=coalesce(e.source_rebuild_lock,0)+1
            RETURN e.id AS id,coalesce(e.content,'') AS content,e.embedding_model AS model,
                   size(e.embedding) AS dimensions ORDER BY e.id""")
        current = [dict(row) async for row in result]
        if current != snapshot:
            raise ValueError("事件在补建期间变化，原文索引未替换")
        await tx.run("MATCH (s:EventSource) DETACH DELETE s")
        from memory.ladybug_store import LadybugStore
        for source in sources:
            await LadybugStore._create_source(tx, source["event_id"], source)
        await tx.run("MATCH (e:Event) SET e.source_rebuild_lock=NULL")
        # 取消必须在事务提交前传播。
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
