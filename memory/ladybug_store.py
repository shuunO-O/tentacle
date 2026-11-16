"""
Ladybug 图/向量混合存储模块

职责：
1. 初始化本地图表与向量模型元数据
2. 写入事件、实体、关系
3. 执行图遍历 + 向量相似度混合检索

使用单例模式，确保全局只维护一个 Database。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from memory.ladybug_driver import LABELS, LadybugDriver, properties
from memory.concept_groups import group_key, name_key, similarity
from runtime.timezone import local_datetime

from memory.embeddings import EmbeddingClient
from memory.event_store import EventEvidenceStore
from memory.concept_aligner import RULE_VERSION, AlignmentConfig, ConceptAligner, JevClient
from memory.concept_audit import ConceptAuditLog
from memory.id_generator import MemoryIdGenerator
from config.manager import ConfigManager

logger = logging.getLogger(__name__)

_VALID_RELATIONS = {"PARTICIPATED_IN", "OCCURRED_AT", "MENTIONS", "HAS_ATTITUDE"}

DEFAULT_EVENT_LIMIT = 3
MAX_EVENT_LIMIT = 10


def search_date_bounds(
    time_hint: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """本地日历日期转为左闭右开的创建时间边界，非法输入不能降级成全库查询。"""
    if time_hint and (date_from or date_to):
        raise ValueError("time_hint 不能与 date_from/date_to 同时提供")
    if time_hint:
        date_from = date_to = time_hint

    def parse(value):
        if value is None:
            return None
        try:
            day = datetime.strptime(value, "%Y-%m-%d")
            if day.date().isoformat() != value:
                raise ValueError
            return day
        except (ValueError, TypeError):
            raise ValueError("日期必须是有效的 YYYY-MM-DD") from None

    start, end = parse(date_from), parse(date_to)
    if start and end and start > end:
        raise ValueError("date_from 不能晚于 date_to")
    try:
        return start.isoformat() if start else None, (end + timedelta(days=1)).isoformat() if end else None
    except OverflowError:
        raise ValueError("date_to 超出支持范围") from None


def _date_predicates(var: str, start_time: Optional[str], end_time: Optional[str]) -> list[str]:
    return ([f"{var}.create_time >= $start_time"] if start_time else []) + (
        [f"{var}.create_time < $end_time"] if end_time else []
    )


def _node_projection(var, fields):
    """明确选择业务字段，图谱和实体复用不把向量复制到 Python。"""
    return "{" + f"_label:label({var})," + ",".join(f"{key}:{var}.{key}" for key in fields) + "}"


class LadybugStore(EventEvidenceStore):
    """
    Ladybug 存储（单例）
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def _init(self):
        """从配置初始化驱动和参数"""
        manager = ConfigManager()
        memory_cfg = manager.get_memory_config()
        search_cfg = memory_cfg.get("search", {})
        self.driver = self.get_read_driver()
        self.dimensions = memory_cfg.get("embedding", {}).get("dimensions", 1024)
        self.top_k = search_cfg.get("top_k", 3)
        self.id_generator = MemoryIdGenerator()
        self.embedding_client = EmbeddingClient()
        self.alignment_config = AlignmentConfig.from_app_config(manager.snapshot())
        self.concept_audit = getattr(self, "concept_audit", ConceptAuditLog())
        self.concept_aligner = ConceptAligner(self.alignment_config, JevClient(
            self.alignment_config, audit_log=self.concept_audit, source="alignment"))
        self._save_lock = getattr(self, "_save_lock", asyncio.Lock())
        self._evidence_generation = getattr(self, "_evidence_generation", 0)
        self._indexes_ensured = False
        self._initialized = True

        logger.info("LadybugStore 初始化完成")

    def set_concept_audit(self, audit):
        """入口注入与 chronicle 同目录的审计库；不初始化图谱或读取配置。"""
        self.concept_audit = audit
        if self._initialized:
            self.concept_aligner.client.audit_log = audit

    def get_read_driver(self):
        """惰性取得共享数据库，不初始化 embedding 或 ID 序列。"""
        driver = getattr(self, "driver", None)
        if driver is None:
            cfg = ConfigManager().get_memory_config()
            driver = LadybugDriver(cfg.get("ladybug", {}).get("path", "data/memory.lbdb"),
                                   cfg.get("embedding", {}).get("dimensions", 1024))
            self.driver = driver
        return driver

    async def close(self):
        """关闭驱动"""
        self._evidence_service = None
        if getattr(self, "driver", None) is not None:
            await self.driver.close()
            self.driver = None
            self._initialized = False

    async def _ensure_indexes(self):
        """确保本地图表与向量模型一致；不自动迁移或重新向量化。"""
        if self._indexes_ensured:
            return
        if not getattr(self.embedding_client, "_initialized", True):
            # 无向量的定向查询不应要求模型 Key；首次 embed 后再验证模型元数据。
            await self.driver.ready()
            return
        model = self.embedding_client.model_name
        dimensions = self.embedding_client.dimensions
        async with self.driver.session() as session:
            async def validate(tx):
                rows = [dict(r) async for r in await tx.run(
                    "MATCH (m:MemoryMeta {id:'embedding'}) RETURN m.model AS model,m.dimensions AS dimensions")]
                if rows and (rows[0]["model"] != model or rows[0]["dimensions"] != dimensions):
                    raise ValueError("记忆数据库的向量模型或维度不同")
                if dimensions != self.driver.dimensions:
                    raise ValueError("记忆数据库的向量维度不同")
                if not rows:
                    await tx.run("CREATE (:MemoryMeta {id:'embedding',model:$model,dimensions:$dimensions})",
                                 model=model, dimensions=dimensions)
            await session.execute_write(validate)
        self._indexes_ensured = True

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def save_memory(
        self, content: str, session_id: str, extraction: dict, alignment_report: list | None = None,
    ) -> str:
        """串行化召回→对齐→写入，避免同进程并发首次写入重复 Concept。"""
        if not self._initialized:
            self._init()
        async with self._save_lock:
            return await self._save_memory(content, session_id, extraction, alignment_report)

    async def _save_memory(
        self, content: str, session_id: str, extraction: dict, alignment_report: list | None,
    ) -> str:
        """
        将提取结果写入 Ladybug。

        Args:
            content: 主模型传入的原始记忆文本
            session_id: 当前 session_id
            extraction: extractor 输出的结构化 JSON

        Returns:
            新创建的事件节点 ID
        """
        if not self._initialized:
            self._init()
        await self._ensure_indexes()

        event_data = extraction.get("event", {})
        event_type = event_data.get("type", "interaction")
        event_summary = event_data.get("summary", "")
        if not event_summary:
            event_summary = content[:100]

        entities = extraction.get("entities", {})
        persons = list(dict.fromkeys(name.strip() for name in entities.get("persons", []) if name.strip()))
        locations = list(dict.fromkeys(name.strip() for name in entities.get("locations", []) if name.strip()))
        concepts = list(dict.fromkeys(name.strip() for name in entities.get("concepts", []) if name.strip()))
        if set(persons) & set(locations) or set(persons) & set(concepts) or set(locations) & set(concepts):
            raise ValueError("提取结果包含跨类型同名实体，关系端点不明确，请分开命名")
        if "CURRENT_EVENT" in persons + locations + concepts:
            raise ValueError("实体名称不能使用保留引用 CURRENT_EVENT")
        relationships = extraction.get("relationships", [])

        # 生成 ID
        event_id = await self.id_generator.next_id("event")
        name_to_label: dict[str, str] = {}
        name_to_id: dict[str, str] = {}
        for name in persons:
            name_to_label[name] = "Person"
            name_to_id[name] = await self.id_generator.next_id("person")
        for name in locations:
            name_to_label[name] = "Location"
            name_to_id[name] = await self.id_generator.next_id("location")
        for name in concepts:
            name_to_label[name] = "Concept"
            name_to_id[name] = await self.id_generator.next_id("concept")

        # 摘要、实体与原文共用同一客户端；原文分块与事件在同一事务提交。
        from memory.retrieval import source_chunks
        chunks = source_chunks(content)
        entity_names = list(name_to_label.keys())
        embeddings = await self.embedding_client.embed([event_summary] + entity_names + [c["text"] for c in chunks])
        await self._ensure_indexes()
        if len(embeddings) != 1 + len(entity_names) + len(chunks):
            raise ValueError("记忆向量数量不完整，未写入事件")
        event_embedding = embeddings[0]
        entity_embeddings = embeddings[1:1 + len(entity_names)]
        source_records = [{**chunk, "id": f'{event_id}:source:{chunk["start"]}', "embedding": vector,
                           "embedding_model": self.embedding_client.model_name,
                           "embedding_dim": self.embedding_client.dimensions}
                          for chunk, vector in zip(chunks, embeddings[1 + len(entity_names):])]

        entity_records = []
        for idx, (name, label) in enumerate(name_to_label.items()):
            entity_records.append(
                {
                    "name": name,
                    "label": label,
                    "id": name_to_id[name],
                    "embedding": entity_embeddings[idx],
                }
            )

        create_time = local_datetime().replace(tzinfo=None).isoformat()
        decisions = []
        if self.alignment_config.enabled and self.alignment_config.model and concepts:
            decisions = await self.concept_aligner.align(
                [r for r in entity_records if r["label"] == "Concept"],
                content, event_summary, self._recall_concepts,
            )
            by_name = {d["mention"]: d for d in decisions}
            for rec in entity_records:
                if rec["label"] == "Concept":
                    rec["alignment"] = by_name[rec["name"]]
                    rec["first_context"] = event_summary[:500]

        audit = getattr(self, "concept_audit", None)
        audit_id = None
        if audit is not None and decisions:
            audit_id = await audit.begin(actor="automatic", source="alignment", action="alignment_save", reference=event_id,
                                         data={"rule_version": RULE_VERSION, "summary": event_summary, "decisions": decisions})
        try:
            async with self.driver.session() as session:
                await session.execute_write(
                    self._save_memory_tx,
                    event_id=event_id,
                    event_type=event_type,
                    event_summary=event_summary,
                    content=content,
                    create_time=create_time,
                    raw_session=session_id,
                    event_embedding=event_embedding,
                    entity_records=entity_records,
                    relationships=relationships,
                    name_to_label=name_to_label,
                    alignment_json=json.dumps(decisions, ensure_ascii=False, allow_nan=False) if decisions else None,
                    source_records=source_records,
                    embedding_model=self.embedding_client.model_name,
                )
        except BaseException as exc:
            if audit_id:
                await audit.finish(audit_id, "unknown", {"error_type": type(exc).__name__})
            raise
        if audit_id:
            try:
                await audit.finish(audit_id, "succeeded", {"event_id": event_id})
            except Exception:
                # 图事务已提交，不能因第二个存储的确认失败诱导再次保存；running 记录留待核对。
                logger.error("记忆已提交但 Concept 审计完成状态写入失败: %s", audit_id)

        if alignment_report is not None:
            alignment_report.extend(decisions)
        logger.info(f"记忆已保存: {event_id}")
        return event_id

    async def _recall_concepts(self, name: str, vector: list[float], top_k: int, *, allow_truncated_exact=False) -> list[dict]:
        """精确名称与向量召回按组去重，评估包含同组成员的原始证据。"""
        projection = _node_projection("n", ("id", "name", "aliases", "first_context", "equivalence_group"))
        nodes = await self._query(f"MATCH (n:Concept) RETURN {projection} AS n ORDER BY n.id")
        exact = [properties(r["n"]) for r in nodes
                 if any(name_key(value) == name_key(name)
                        for value in [r["n"]["name"], *(r["n"].get("aliases") or [])])]
        exact_groups = list(dict.fromkeys(group_key(n) for n in exact))
        if len(exact_groups) > top_k and not allow_truncated_exact:
            raise ValueError("ambiguous_or_invalid_exact_candidates")
        exact_groups = exact_groups[:top_k]
        ranked = await self._query(f"""MATCH (n:Concept)
            WITH n,{self._similarity('n.embedding')} AS score WHERE score IS NOT NULL
            WITH coalesce(n.equivalence_group,n.id) AS group_id,max(score) AS score
            RETURN group_id,score ORDER BY score DESC,group_id LIMIT $top_k""", vector=vector, top_k=top_k)
        vector_groups = list(dict.fromkeys(r["group_id"] for r in ranked))[:top_k]
        groups = list(dict.fromkeys(exact_groups + vector_groups))
        result = []
        for group in groups:
            members = [properties(r["n"]) for r in nodes if group_key(r["n"]) == group]
            if not members:
                raise ValueError("candidate_changed")
            representative = next((n for n in members if n["id"] == group), members[0])
            events = await self._query("""MATCH (e:Event)-[:MENTIONS]->(n:Concept)
                WHERE coalesce(n.equivalence_group,n.id)=$group_id
                WITH DISTINCT e ORDER BY e.create_time DESC,e.id LIMIT 3 RETURN e.summary AS summary""",
                group_id=group)
            result.append({"id": representative["id"], "group_id": group, "name": representative["name"],
                           "aliases": list(dict.fromkeys(v for n in members
                               for v in [n["name"], *n.get("aliases", [])] if v != representative["name"]))[:20],
                           "first_context": representative.get("first_context", "")[:500],
                           "members": [{"id": n["id"], "name": n["name"]} for n in members],
                           "events": [(r["summary"] or "")[:300] for r in events]})
        return result

    @staticmethod
    async def _save_memory_tx(tx, event_id, event_type, event_summary, content, create_time, raw_session,
                              event_embedding, entity_records, relationships, name_to_label,
                              alignment_json=None, source_records=None, embedding_model=None):
        name_to_id = {}
        for rec in entity_records:
            label = rec["label"]
            if label not in {"Person", "Location", "Concept"}:
                raise ValueError("invalid_entity_label")
            alignment = rec.get("alignment")
            # 名称完全一致且指向同一个节点时复用；同名异义保留独立节点。
            projection = _node_projection("n", ("id", "name", "aliases", "equivalence_group"))
            existing = [properties(r["n"]) async for r in await tx.run(
                f"MATCH (n:{label}) RETURN {projection} AS n")]
            exact = [n for n in existing if any(name_key(v) == name_key(rec["name"])
                     for v in [n["name"], *n.get("aliases", [])])]
            target = None
            if alignment and alignment["action"] not in {"join", "create"}:
                raise ValueError("invalid_alignment_action")
            if alignment and alignment["action"] == "join":
                target = next((n for n in existing if n["id"] == alignment["target_id"]), None)
                if target is None:
                    raise ValueError("对齐目标不存在，记忆写入已回滚")
            if not alignment:
                identities = {group_key(n) if label == "Concept" else n["id"] for n in exact}
                if len(identities) > 1:
                    raise ValueError("名称或别名指向多个节点，记忆写入已回滚")
                reusable = exact
            else:
                reusable = [n for n in exact if target and group_key(n) == group_key(target)]
            reuse = min(reusable, key=lambda n: n["id"]) if reusable else None
            if reuse:
                name_to_id[rec["name"]] = reuse["id"]
                continue
            group = group_key(target) if target else rec["id"]
            await tx.run(f"""CREATE (n:{label} {{id:$id,name:$name,embedding:$embedding,
                first_context:$context,equivalence_group:$group_id,group_joined_at:$joined_at}})""",
                id=rec["id"], name=rec["name"], embedding=rec["embedding"],
                context=rec.get("first_context", event_summary[:500]) if label == "Concept" else None,
                group_id=group if label == "Concept" else None,
                joined_at=create_time if target else None)
            name_to_id[rec["name"]] = rec["id"]
        await tx.run("""CREATE (:Event {id:$id,type:$type,summary:$summary,content:$content,
            create_time:$create_time,raw_session:$raw_session,embedding:$embedding,
            concept_alignment:$alignment_json,embedding_model:$embedding_model})""",
            id=event_id, type=event_type, summary=event_summary, content=content, create_time=create_time,
            raw_session=raw_session, embedding=event_embedding, alignment_json=alignment_json,
            embedding_model=embedding_model)
        for row in source_records or []:
            await LadybugStore._create_source(tx, event_id, row)
        for rel in relationships:
            kind = rel.get("type")
            if kind not in _VALID_RELATIONS:
                raise ValueError("invalid_relationship_type")
            a_label, a = LadybugStore._resolve_node_ref(rel.get("source"), event_id, name_to_label)
            b_label, b = LadybugStore._resolve_node_ref(rel.get("target"), event_id, name_to_label)
            if not a_label or not b_label:
                raise ValueError("关系端点不明确，记忆写入已回滚")
            a = a if a_label == "Event" else name_to_id[a]
            b = b if b_label == "Event" else name_to_id[b]
            props = rel.get("properties") or {}
            if not isinstance(props, dict):
                raise ValueError("关系属性必须为对象")
            # JSON 保留提取器提供的全部属性；sentiment 同时支持图查询。
            previous = [dict(r) async for r in await tx.run(f"""MATCH (a:{a_label} {{id:$a}}),(b:{b_label} {{id:$b}})
                MERGE (a)-[r:{kind}]->(b) RETURN r""", a=a, b=b)]
            if len(previous) != 1:
                raise ValueError("关系端点不存在，记忆写入已回滚")
            props = {**LadybugStore._relation_props(previous[0]["r"]), **props}
            rows = [dict(r) async for r in await tx.run(f"""MATCH (a:{a_label} {{id:$a}})-[r:{kind}]->(b:{b_label} {{id:$b}})
                SET r.properties_json=$props,r.sentiment=$sentiment
                RETURN count(r) AS written""", a=a, b=b,
                props=json.dumps(props, ensure_ascii=False, allow_nan=False), sentiment=props.get("sentiment"))]
            if len(rows) != 1 or rows[0]["written"] != 1:
                raise ValueError("关系端点不存在，记忆写入已回滚")

    @staticmethod
    def _resolve_node_ref(
        ref: str, event_id: str, name_to_label: dict[str, str]
    ) -> tuple[Optional[str], Optional[str]]:
        """
        将关系中的节点引用解析为 (label, 匹配值)。

        CURRENT_EVENT -> (Event, event_id)
        普通名称 -> (label, name)
        """
        if ref == "CURRENT_EVENT":
            return "Event", event_id
        if ref in name_to_label:
            return name_to_label[ref], ref
        return None, None

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    async def search(
        self,
        query_text: Optional[str],
        target_label: str,
        entity_filter: Optional[str],
        time_hint: Optional[str] = None,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict:
        """
        混合检索入口。

        Args:
            query_text: 查询文本，为空时按时间/实体过滤
            target_label: 目标节点标签
            entity_filter: 明确提及的实体名称
            time_hint: 单日，与 date_from/date_to 互斥
            date_from/date_to: 含首尾日期的创建时间范围，可只提供一端
            event_limit: 每个 Person/Concept 的关联事件摘要数，1..10

        Returns:
            {"primary_match": {...}, "secondary_matches": [...]}
        """
        start_time, end_time = search_date_bounds(time_hint, date_from, date_to)
        if type(event_limit) is not int or not 1 <= event_limit <= MAX_EVENT_LIMIT:
            raise ValueError(f"event_limit 必须是 1..{MAX_EVENT_LIMIT} 的整数")
        if target_label not in {"Event", "Person", "Concept"}:
            raise ValueError("target_label 必须是 Event、Person 或 Concept")
        if not self._initialized:
            self._init()
        await self._ensure_indexes()

        query_vector = None
        if query_text:
            query_vector = (await self.embedding_client.embed([query_text]))[0]
            await self._ensure_indexes()

        # 所有入口按精确余弦排序，并保留硬过滤语义
        if query_vector is not None and not entity_filter and not (start_time or end_time):
            records = await self._vector_search(target_label, query_vector, self.top_k)
        else:
            records = await self._filtered_search(
                target_label,
                query_vector,
                entity_filter,
                start_time,
                end_time,
                self.top_k,
            )

        if not records:
            return {"primary_match": None, "secondary_matches": []}

        primary = records[0]
        primary["label"] = target_label
        primary["confidence"] = primary.get("score", 0.0)

        async def context_for(node_id):
            context = await self._get_context(node_id)
            if target_label in {"Person", "Concept"}:
                context.update(await self._get_related_events(node_id, start_time, end_time, event_limit))
            return context

        context = await context_for(primary["id"])

        primary_match = {
            "node": primary,
            "context": context,
        }

        secondary_matches = []
        for r in records[1:]:
            r["label"] = target_label
            r["confidence"] = r.get("score", 0.0)
            if target_label in {"Person", "Concept"}:
                r["context"] = await context_for(r["id"])
            secondary_matches.append(r)

        return {
            "primary_match": primary_match,
            "secondary_matches": secondary_matches,
        }

    async def _vector_search(self, label, query_vector, top_k):
        return await self._filtered_search(label, query_vector, None, None, None, top_k)

    async def _filtered_search(self, label, query_vector, entity_filter, start_time, end_time, top_k):
        if label not in LABELS:
            raise ValueError("invalid_entity_label")
        params = {"top_k": top_k}
        predicates = []
        if entity_filter:
            if label == "Concept":
                predicates.append("""EXISTS { MATCH (member:Concept),(named:Concept)
                    WHERE coalesce(member.equivalence_group,member.id)=coalesce(n.equivalence_group,n.id)
                    AND coalesce(named.equivalence_group,named.id)=coalesce(n.equivalence_group,n.id)
                    AND (named.name=$entity_filter OR $entity_filter IN coalesce(named.aliases,[])) }""")
            elif label == "Event":
                predicates.append(self._evidence_where({"entity_filter": entity_filter})
                                  .replace("(e)", "(n)").replace("e.", "n."))
                params["excluded"] = []
            else:
                predicates.append("(n.name=$entity_filter OR $entity_filter IN coalesce(n.aliases,[]))")
            params["entity_filter"] = entity_filter
        dates = _date_predicates("n" if label == "Event" else "e", start_time, end_time)
        if dates:
            condition = " AND ".join(dates)
            if label == "Concept":
                condition = ("EXISTS { MATCH (e:Event)-[]-(member:Concept) WHERE "
                             "coalesce(member.equivalence_group,member.id)=coalesce(n.equivalence_group,n.id) AND "
                             + condition + " }")
            elif label != "Event":
                condition = "EXISTS { MATCH (n)-[]-(e:Event) WHERE " + condition + " }"
            predicates.append(condition)
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        score = self._similarity("n.embedding", "queryVector") if query_vector is not None else "0.0"
        if query_vector is not None:
            params["queryVector"] = query_vector
        where = "WHERE " + " AND ".join(predicates) if predicates else ""
        order = "score DESC,n.id" if query_vector is not None else "n.create_time DESC,n.id" if label == "Event" else "n.name,n.id"
        projection = _node_projection("n", ("id", "name", "summary", "content", "create_time", "type", "equivalence_group"))
        ranking = f"MATCH (n:{label}) {where} WITH n,{score} AS score WHERE score IS NOT NULL"
        if label == "Concept":
            prefer = ("b.score>a.score OR (b.score=a.score AND b.id<a.id)" if query_vector is not None else
                      "b.name<a.name OR (b.name=a.name AND b.id<a.id)")
            best_order = "best.score DESC,best.id" if query_vector is not None else "best.name,best.id"
            ranking += f""" WITH coalesce(n.equivalence_group,n.id) AS group_id,
                list_reduce(collect({{id:n.id,name:n.name,score:score}}),
                    (a,b) -> CASE WHEN {prefer} THEN b ELSE a END) AS best
                WITH group_id,best ORDER BY {best_order} LIMIT $top_k
                MATCH (n:Concept {{id:best.id}}) RETURN {projection} AS n,best.score AS score ORDER BY {order}"""
        else:
            ranking += f" RETURN {projection} AS n,score ORDER BY {order} LIMIT $top_k"
        rows = await self._query(ranking, **params)
        seen, result = set(), []
        for row in rows:
            n = properties(row["n"])
            key = group_key(n) if label == "Concept" else n["id"]
            if key in seen:
                continue
            seen.add(key)
            result.append({"id": n["id"], "name": n.get("name"), "summary": n.get("summary"),
                           "content": n.get("content"), "timestamp": n.get("create_time"),
                           "type": n.get("type"), "score": row["score"],
                           **({"equivalence_group": key} if label == "Concept" else {})})
            if len(result) >= top_k:
                break
        return result

    # ------------------------------------------------------------------
    # 图可视化（只读）
    # ------------------------------------------------------------------

    async def count_summary(self):
        rows = await self._query("MATCH (n:Event|Person|Concept|Location) RETURN label(n) AS label,count(*) AS c")
        relations = await self._query("""MATCH ()-[r:PARTICIPATED_IN|OCCURRED_AT|MENTIONS|HAS_ATTITUDE]->()
            RETURN count(r) AS c""")
        return {"nodes": {r["label"]: r["c"] for r in rows}, "relationships": relations[0]["c"]}

    async def fetch_graph(self, labels=None):
        selected = [label for label in labels or LABELS if label in LABELS]
        if not selected:
            selected = list(LABELS)
        projection = _node_projection("n", ("id", "name", "type", "summary", "content", "create_time", "equivalence_group"))
        rows = await self._query(f"MATCH (n:Event|Person|Concept|Location) WHERE label(n) IN $labels RETURN {projection} AS n",
                                 labels=selected)
        nodes = []
        for row in rows:
            n = row["n"]
            nodes.append({"id": self._graph_id(n), "label": n["_label"],
                          **{key: n.get(key) for key in ("name", "type", "summary", "content", "create_time")},
                          **({"equivalence_group": group_key(n)} if n["_label"] == "Concept" else {})})
        ids = {n["id"] for n in nodes}
        a, b = (_node_projection(var, ("id", "name")) for var in ("a", "b"))
        rows = await self._query(f"""MATCH (a)-[r:PARTICIPATED_IN|OCCURRED_AT|MENTIONS|HAS_ATTITUDE]->(b)
            RETURN {a} AS a,{b} AS b,r""")
        edges = [{"source": self._graph_id(r["a"]), "target": self._graph_id(r["b"]),
                  "type": r["r"]["_label"], "properties": self._relation_props(r["r"])} for r in rows
                 if self._graph_id(r["a"]) in ids and self._graph_id(r["b"]) in ids]
        nodes.sort(key=lambda n: n["id"])
        edges.sort(key=lambda e: (e["source"], e["target"], e["type"],
                                  json.dumps(e["properties"], sort_keys=True, ensure_ascii=False)))
        return {"nodes": nodes, "relationships": edges}

    async def _get_context(self, node_id):
        projection = _node_projection("n", ("id",))
        rows = await self._query(f"""MATCH (n:Event|Person|Concept|Location {{id:$node_id}})
            RETURN {projection} AS n""", node_id=node_id)
        context = {"persons": [], "locations": [], "concepts": [], "attitudes": []}
        if not rows:
            return context
        node = rows[0]["n"]
        members = await self._concept_member_ids(node_id) if node["_label"] == "Concept" else [node_id]
        projection = _node_projection("m", ("id", "name", "equivalence_group"))
        rows = await self._query(f"""MATCH (n:Event|Person|Concept|Location)-[r:PARTICIPATED_IN|OCCURRED_AT|MENTIONS|HAS_ATTITUDE]-(m:Person|Concept|Location)
            WHERE n.id IN $ids RETURN DISTINCT {projection} AS m,r""", ids=members)
        seen = set()
        for row in rows:
            m, r = row["m"], row["r"]
            kind = r["_label"]
            key = (group_key(m) if m["_label"] == "Concept" else m["id"], kind, r.get("properties_json"))
            if key in seen:
                continue
            seen.add(key)
            item = {"name": m["name"], "relation": kind}
            if kind == "HAS_ATTITUDE":
                item["sentiment"] = self._relation_props(r).get("sentiment", "")
                context["attitudes"].append(item)
            else:
                context[{"Person": "persons", "Concept": "concepts", "Location": "locations"}[m["_label"]]].append(item)
        return context

    async def _get_related_events(self, node_id, start_time, end_time, event_limit):
        members = await self._concept_member_ids(node_id)
        params = {"ids": members or [node_id], "fetch_limit": event_limit + 1}
        predicates = ["n.id IN $ids", *_date_predicates("e", start_time, end_time)]
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        rows = await self._query("""MATCH (n:Person|Concept|Location)-[r:PARTICIPATED_IN|OCCURRED_AT|MENTIONS|HAS_ATTITUDE]-(e:Event)
            WHERE """ + " AND ".join(predicates) + """ WITH e,collect(r) AS relations
            ORDER BY e.create_time DESC,e.id LIMIT $fetch_limit
            RETURN e.id AS id,e.create_time AS timestamp,e.type AS type,e.summary AS summary,relations""", **params)
        events = [{**r, "relations": [
                       {"relation": rel["_label"], "properties": self._relation_props(rel)} for rel in r["relations"]]}
                  for r in rows]
        return {"events": events[:event_limit], "events_has_more": len(events) > event_limit}

    async def _query(self, query, **params):
        async with self.get_read_driver().session() as session:
            return [dict(r) async for r in await session.run(query, params)]

    def _similarity(self, expression, parameter="vector"):
        dimensions = (getattr(self, "dimensions", None)
                      or getattr(getattr(self, "embedding_client", None), "dimensions", None)
                      or self.get_read_driver().dimensions)
        return similarity(expression, parameter, dimensions)

    async def _concept_member_ids(self, node_id):
        rows = await self._query("""MATCH (n:Concept {id:$id}),(m:Concept)
            WHERE coalesce(n.equivalence_group,n.id)=coalesce(m.equivalence_group,m.id)
            RETURN m.id AS id ORDER BY m.id""", id=node_id)
        return [r["id"] for r in rows]

    @staticmethod
    def _graph_id(node):
        label = node["_label"]
        return f"{label}:{node['id'] if label in {'Event', 'Concept'} else node['name']}"

    @staticmethod
    def _relation_props(rel):
        return json.loads(rel["properties_json"]) if rel.get("properties_json") else (
            {"sentiment": rel["sentiment"]} if rel.get("sentiment") is not None else {})

    @staticmethod
    async def _create_source(tx, event_id, row):
        await tx.run("""MATCH (e:Event {id:$event_id}) CREATE (s:EventSource {
            id:$id,start:$start,`end`:$end_value,text:$text,hash:$hash,embedding:$embedding,
            embedding_model:$embedding_model,embedding_dim:$embedding_dim}) CREATE (e)-[:HAS_SOURCE]->(s)""",
            {"event_id": event_id, **{k: v for k, v in row.items() if k not in {"end", "event_id"}},
             "end_value": row["end"]})
