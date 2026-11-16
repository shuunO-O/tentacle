"""Ladybug 记忆图谱只读查询与 Concept 去重调用仓储。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from memory.concept_maintenance import ReviewRequired

logger = logging.getLogger(__name__)


async def run_dedup_call(awaitable) -> Any:
    try:
        return await awaitable
    except ReviewRequired as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Concept 维护失败: %s", type(exc).__name__)
        raise HTTPException(503, detail="记忆库维护暂不可用，请检查连接或稍后重试") from exc


async def fetch_memory_summary(new_store_fn) -> dict[str, Any]:
    try:
        store = new_store_fn()
        summary = await store.count_summary()
        return {"ok": True, "data": summary}
    except Exception as exc:
        logger.warning("Ladybug 图谱概览失败: %s", type(exc).__name__)
        return {"ok": False, "data": {"nodes": {}, "relationships": 0}, "error": "记忆库暂不可用"}


async def fetch_memory_graph(new_store_fn, labels: str = "") -> dict[str, Any]:
    label_list = [s.strip() for s in labels.split(",") if s.strip()] if labels else None
    try:
        store = new_store_fn()
        data = await store.fetch_graph(label_list)
        return {"ok": True, "data": data}
    except Exception as exc:
        logger.warning("Ladybug 图谱数据失败: %s", type(exc).__name__)
        return {"ok": False, "data": {"nodes": [], "relationships": []}, "error": "记忆库暂不可用"}
