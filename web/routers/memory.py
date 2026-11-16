"""Ladybug 图谱只读查询与人工 Concept 去重审核路由。"""

from __future__ import annotations

from fastapi import APIRouter

from web.deps import AppContext
from web.repositories.memory_repo import fetch_memory_graph, fetch_memory_summary, run_dedup_call
from web.schemas import (
    ConceptDetachBody,
    ConceptDetachResponse,
    DedupAuditResponse,
    DedupDecisionBody,
    DedupDecisionResponse,
    DedupStartBody,
    DedupStartResponse,
    DedupStatusResponse,
    MemoryGraphResponse,
    MemorySummaryResponse,
    OkResponse,
)


def memory_router(ctx: AppContext, auth_dep) -> APIRouter:
    router = APIRouter(dependencies=[auth_dep])

    # ---------- Ladybug 图谱可视化（含旧 /api/neo4j/* 兼容别名） ----------

    @router.get(
        "/api/neo4j/summary",
        include_in_schema=False,
        response_model=MemorySummaryResponse,
        response_model_exclude_unset=True,
    )
    @router.get(
        "/api/memory/summary",
        response_model=MemorySummaryResponse,
        response_model_exclude_unset=True,
    )
    async def memory_summary():
        """图谱概览：各标签节点数与关系总数。数据库不可用时返回空并标记不可用。"""
        return await fetch_memory_summary(ctx.new_ladybug_store)

    @router.get(
        "/api/neo4j/graph",
        include_in_schema=False,
        response_model=MemoryGraphResponse,
        response_model_exclude_unset=True,
    )
    @router.get(
        "/api/memory/graph",
        response_model=MemoryGraphResponse,
        response_model_exclude_unset=True,
    )
    async def memory_graph(labels: str = ""):
        """图谱数据：节点与关系。labels 为逗号分隔的标签白名单，空 = 全部。"""
        return await fetch_memory_graph(ctx.new_ladybug_store, labels)

    # ---------- Concept 维护（仅人工 REST，不注册给 agent / scheduler） ----------

    @router.post(
        "/api/neo4j/concepts/detach",
        include_in_schema=False,
        response_model=ConceptDetachResponse,
        response_model_exclude_unset=True,
    )
    @router.post(
        "/api/memory/concepts/detach",
        response_model=ConceptDetachResponse,
        response_model_exclude_unset=True,
    )
    async def concept_detach(body: ConceptDetachBody):
        return await run_dedup_call(ctx.dedup_manager().detach(body.concept_id))

    @router.get(
        "/api/neo4j/dedup",
        include_in_schema=False,
        response_model=DedupStatusResponse,
        response_model_exclude_unset=True,
    )
    @router.get(
        "/api/memory/dedup",
        response_model=DedupStatusResponse,
        response_model_exclude_unset=True,
    )
    async def concept_dedup_status(state: str = "pending", offset: int = 0):
        return await run_dedup_call(ctx.dedup_manager().status(state, offset))

    @router.get(
        "/api/neo4j/dedup/audit",
        include_in_schema=False,
        response_model=DedupAuditResponse,
        response_model_exclude_unset=True,
    )
    @router.get(
        "/api/memory/dedup/audit",
        response_model=DedupAuditResponse,
        response_model_exclude_unset=True,
    )
    async def concept_audit(actor: str = "all", offset: int = 0):
        return await run_dedup_call(ctx.dedup_manager().audit.list(actor, offset))

    @router.post(
        "/api/neo4j/dedup/start",
        include_in_schema=False,
        response_model=DedupStartResponse,
        response_model_exclude_unset=True,
    )
    @router.post(
        "/api/memory/dedup/start",
        response_model=DedupStartResponse,
        response_model_exclude_unset=True,
    )
    async def concept_dedup_start(body: DedupStartBody):
        return await run_dedup_call(ctx.dedup_manager().start(body.top_k, body.max_pairs))

    @router.post(
        "/api/neo4j/dedup/cancel",
        include_in_schema=False,
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    @router.post(
        "/api/memory/dedup/cancel",
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    async def concept_dedup_cancel():
        return await run_dedup_call(ctx.dedup_manager().cancel())

    @router.post("/api/neo4j/dedup/{pair_id}/refresh", include_in_schema=False)
    @router.post("/api/memory/dedup/{pair_id}/refresh")
    async def concept_dedup_refresh(pair_id: str):
        return await run_dedup_call(ctx.dedup_manager().refresh(pair_id))

    @router.post("/api/neo4j/dedup/{pair_id}/reevaluate", include_in_schema=False)
    @router.post("/api/memory/dedup/{pair_id}/reevaluate")
    async def concept_dedup_reevaluate(pair_id: str):
        return await run_dedup_call(ctx.dedup_manager().reevaluate(pair_id))

    @router.post(
        "/api/memory/dedup/{pair_id}/decide",
        response_model=DedupDecisionResponse,
        response_model_exclude_unset=True,
    )
    async def concept_dedup_decide(pair_id: str, body: DedupDecisionBody):
        return await run_dedup_call(ctx.dedup_manager().decide(pair_id, body.action, body.keep_id, body.policy))

    @router.post(
        "/api/neo4j/dedup/{pair_id}/decide",
        include_in_schema=False,
        response_model=DedupDecisionResponse,
        response_model_exclude_unset=True,
    )
    async def legacy_concept_dedup_decide(pair_id: str, body: DedupDecisionBody):
        # 旧客户端可提交原冲突选项；等价分组保留双方的关系属性。
        if body.policy in {"keep_target", "keep_source"}:
            body = body.model_copy(update={"policy": "reject"})
        return await concept_dedup_decide(pair_id, body)

    return router
