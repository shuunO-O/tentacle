"""等价概念分组维护；原始节点与关系保持不变。"""

import asyncio
import hashlib
import json
import uuid

from memory.concept_groups import group_key
from memory.ladybug_driver import properties
from memory.ladybug_store import LadybugStore
from runtime.timezone import local_datetime


class ReviewRequired(ValueError):
    pass


class CandidateGone(ReviewRequired):
    """候选已删除或已在同一组，可以跳过。"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def fingerprint(snapshot):
    return hashlib.sha256(encode(snapshot).encode()).hexdigest()


class ConceptMaintenance:
    def __init__(self, store=None):
        self.store = store or LadybugStore()

    @property
    def driver(self):
        return self.store.get_read_driver()

    async def validate(self):
        await self.driver.ready()

    async def page(self, after="", limit=50):
        return await self.store._query("""MATCH (n:Concept) WHERE n.id > $after
            RETURN n.id AS id,n.name AS name,n.embedding AS embedding,
            coalesce(n.equivalence_group,n.id) AS group_id ORDER BY n.id LIMIT $limit""",
            after=after, limit=limit)

    async def candidates(self, node, top_k):
        if not node.get("embedding"):
            raise ReviewRequired("节点缺少向量，未完成召回")
        groups = await self.store._recall_concepts(node["name"], node["embedding"], top_k + 1,
                                                  allow_truncated_exact=True)
        return [n for n in groups if n["group_id"] != node.get("group_id", node["id"])][:top_k]

    @staticmethod
    async def _snapshot(tx, ids):
        nodes = [properties(r["n"]) async for r in await tx.run(
            "MATCH (n:Concept) WHERE n.id IN $ids RETURN n ORDER BY n.id", ids=ids)]
        if len(nodes) != 2:
            raise CandidateGone("概念已删除，请重新扫描")
        groups = sorted({group_key(n) for n in nodes})
        if len(groups) != 2:
            raise CandidateGone("概念已在同一等价组")
        members = [properties(r["n"]) async for r in await tx.run("""MATCH (n:Concept)
            WHERE coalesce(n.equivalence_group,n.id) IN $groups RETURN n ORDER BY n.id""", groups=groups)]
        edges = [dict(r) async for r in await tx.run("""MATCH (a)-[r:PARTICIPATED_IN|OCCURRED_AT|MENTIONS|HAS_ATTITUDE]->(b)
            WHERE a.id IN $members OR b.id IN $members RETURN a.id AS source,b.id AS target,r
            ORDER BY source,target,label(r)""", members=[n["id"] for n in members])]
        edges = [{"source": r["source"], "target": r["target"], "type": r["r"]["_label"],
                  "props": LadybugStore._relation_props(r["r"])} for r in edges]
        edges.sort(key=encode)
        return {"nodes": nodes, "members": members, "edges": edges}

    async def preview(self, left_id, right_id):
        async def read(tx):
            snapshot = await self._snapshot(tx, [left_id, right_id])
            evidence = {}
            for n in snapshot["nodes"]:
                group = group_key(n)
                members = [m for m in snapshot["members"] if group_key(m) == group]
                events = [r["summary"] async for r in await tx.run("""MATCH (e:Event)-[:MENTIONS]->(c:Concept)
                    WHERE c.id IN $ids WITH DISTINCT e ORDER BY e.create_time DESC,e.id LIMIT 3
                    RETURN e.summary AS summary""", ids=[m["id"] for m in members])]
                evidence[n["id"]] = {"id": n["id"], "name": n["name"], "group_id": group,
                    "aliases": list(dict.fromkeys(value for m in members
                        for value in [m["name"], *m.get("aliases", [])] if value != n["name"]))[:20],
                    "members": [{"id": m["id"], "name": m["name"]} for m in members],
                    "first_context": n.get("first_context", "")[:500],
                    "events": [(value or "")[:300] for value in events]}
            return {"fingerprint": fingerprint(snapshot), "left": evidence[left_id], "right": evidence[right_id],
                    "conflicts": [], "relationships": len(snapshot["edges"])}
        async with self.driver.session() as session:
            return await session.execute_read(read)

    async def merge(self, keep_id, drop_id, expected, operation_id, policy="reject"):
        # merge 是审核协议的动作名；实际只更新整个等价组的归属。
        if policy != "reject":
            raise ValueError("等价分组不覆盖关系属性")
        if not self.store._initialized:
            self.store._init()
        async with self.store._save_lock:
            async with self.driver.session() as session:
                result = await session.execute_write(self._merge_tx, keep_id, drop_id, expected, operation_id, policy)
            self.store._evidence_generation = getattr(self.store, "_evidence_generation", 0) + 1
            return result

    @staticmethod
    async def _merge_tx(tx, keep_id, drop_id, expected, operation_id, policy="reject"):
        if keep_id == drop_id or policy != "reject":
            raise ValueError("无效的概念分组选择")
        committed = [dict(r) async for r in await tx.run(
            "MATCH (a:ConceptGroupAudit {id:$id}) RETURN a.result AS result", id=operation_id)]
        if committed:
            previous = json.loads(committed[0]["result"])
            if previous["keep_id"] != keep_id or previous["drop_id"] != drop_id:
                raise ReviewRequired("此操作已按其它选择提交，请刷新")
            return previous
        snapshot = await ConceptMaintenance._snapshot(tx, [keep_id, drop_id])
        if fingerprint(snapshot) != expected:
            raise ReviewRequired("概念组成员或关系已变化，请刷新证据后再决定")
        nodes = {n["id"]: n for n in snapshot["nodes"]}
        group = group_key(nodes[keep_id])
        members = [n["id"] for n in snapshot["members"]]
        await tx.run("""MATCH (n:Concept) WHERE n.id IN $ids
            SET n.equivalence_group=$group_id,n.group_joined_at=$at""",
            ids=members, group_id=group, at=local_datetime().isoformat())
        outcome = {"keep_id": keep_id, "drop_id": drop_id, "group_id": group,
                   "members": members, "policy": policy, "operation_id": operation_id,
                   "relationships": len(snapshot["edges"]), "conflicts": 0}
        await tx.run("""CREATE (:ConceptGroupAudit {id:$id,snapshot:$snapshot,result:$result,created_at:$at})""",
            id=operation_id, snapshot=encode(snapshot), result=encode(outcome), at=local_datetime().isoformat())
        return outcome

    async def detach(self, node_id):
        """将一个概念移出当前组，不改任何事件和关系。"""
        if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > 500:
            raise ValueError("concept_id 必须为非空概念 ID，最长 500 字符")
        node_id = node_id.removeprefix("Concept:")
        if not node_id.strip():
            raise ValueError("concept_id 不能为空")
        # 分组维护不依赖模型配置或 ID 序列；后续保存复用此锁与游标版本。
        if not hasattr(self.store, "_save_lock"):
            self.store._save_lock = asyncio.Lock()
        async with self.store._save_lock:
            async with self.driver.session() as session:
                async def write(tx):
                    nodes = [properties(r["n"]) async for r in await tx.run(
                        "MATCH (n:Concept {id:$id}) RETURN n", id=node_id)]
                    if not nodes:
                        raise ValueError("概念不存在")
                    group = "concept_group_" + uuid.uuid4().hex
                    await tx.run("""MATCH (n:Concept {id:$id})
                        SET n.equivalence_group=$group_id,n.group_joined_at=NULL""", id=node_id, group_id=group)
                    outcome = {"concept_id": node_id, "group_id": group,
                               "previous_group_id": group_key(nodes[0])}
                    await tx.run("""CREATE (:ConceptGroupAudit {id:$id,snapshot:$snapshot,result:$result,created_at:$at})""",
                        id=uuid.uuid4().hex, snapshot=encode(nodes), result=encode(outcome), at=local_datetime().isoformat())
                    return outcome
                result = await session.execute_write(write)
            self.store._evidence_generation = getattr(self.store, "_evidence_generation", 0) + 1
            return result
