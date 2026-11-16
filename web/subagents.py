"""Authenticated coding task presentation and DSH handoff."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from web.schemas import (
    DshAttachResponse,
    DshSessionsResponse,
    SubagentDetailResponse,
    SubagentListResponse,
)


class AttachBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


def subagent_router(manager, auth_dep):
    router = APIRouter(prefix="/api/subagents", dependencies=[auth_dep])

    async def read(task_id=None, before=None, limit=20):
        if manager is None:
            if task_id:
                raise HTTPException(404, "子代理未启用")
            return {"enabled": False, "tasks": []}
        try:
            return await manager.presentation(task_id, before=before, limit=limit)
        except LookupError:
            raise HTTPException(404, "子任务不存在") from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        except RuntimeError:
            raise HTTPException(503, "子代理暂不可用") from None

    @router.get(
        "",
        response_model=SubagentListResponse,
        response_model_exclude_unset=True,
    )
    async def tasks(limit: int = Query(default=20, ge=1, le=50)):
        return await read(limit=limit)

    @router.get(
        "/dsh/sessions",
        response_model=DshSessionsResponse,
        response_model_exclude_unset=True,
    )
    async def coding_sessions():
        if manager is None or not manager.coding_available:
            raise HTTPException(404, "DSH 未启用")
        try:
            return {"sessions": (await manager.coding_sessions())[:100]}
        except RuntimeError:
            raise HTTPException(503, "DSH 会话暂不可用") from None

    @router.post(
        "/dsh/attach",
        response_model=DshAttachResponse,
        response_model_exclude_unset=True,
    )
    async def attach(body: AttachBody):
        if manager is None or not manager.coding_available:
            raise HTTPException(404, "DSH 未启用")
        try:
            return await manager.attach(body.session_id)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        except RuntimeError:
            raise HTTPException(503, "DSH 会话暂不可用") from None

    @router.get(
        "/{task_id}",
        response_model=SubagentDetailResponse,
        response_model_exclude_unset=True,
    )
    async def detail(task_id: str, before: int | None = Query(default=None, ge=1),
                     limit: int = Query(default=20, ge=1, le=50)):
        return await read(task_id, before, limit)

    return router
