"""状态监控、用量余额、终端回放与 Chronicle 数据管理路由。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException

import runtime.self_backup as self_backup
from runtime.usage import get_usage_tracker
from web.deps import AppContext
from web.repositories.chronicle_repo import ChronicleRepository
from web.schemas import (
    AdminBackupRestoreResponse,
    AdminBackupsResponse,
    AdminTableRowsResponse,
    AdminTablesResponse,
    BackupRestoreBody,
    BalancesResponse,
    GoalCreateBody,
    GoalManagerOpResponse,
    GoalUpdateBody,
    OkResponse,
    StatusSnapshotResponse,
    TaskCreateBody,
    TaskUpdateBody,
    TerminalHistoryResponse,
    UsageStatsResponse,
)


def build_status_snapshot(ctx: AppContext, repo: ChronicleRepository) -> dict[str, Any]:
    session = ctx.chronicle.get_active_session() if ctx.chronicle is not None else None
    acc_tokens = getattr(ctx.session_manager, "_context_tokens", 0)
    next_beat = getattr(ctx.heartbeat, "_next_beat_time", None) if ctx.heartbeat else None
    interval = getattr(ctx.heartbeat, "interval_seconds", None) if ctx.heartbeat else None
    usage = ctx.web_adapter.get_usage_stats() if ctx.web_adapter is not None else {}
    connections = ctx.conn_manager.count() if ctx.conn_manager is not None else 0

    return {
        "agent_name": ctx.settings.agent_name,
        "session": session,
        "tokens": {"accumulated": acc_tokens},
        "usage": usage,
        "heartbeat": {
            "interval_seconds": interval,
            "next_beat_at": next_beat,
        },
        "db": repo.table_counts_map(),
        "connections": connections,
        "time": time.time(),
    }


async def _read_usage_config(ctx: AppContext) -> dict[str, Any]:
    from config.manager import ConfigManager

    if ctx.configuration_service:
        return (await asyncio.to_thread(ctx.configuration_service.store.read))[2]
    return ConfigManager().snapshot()


def admin_router(ctx: AppContext, repo: ChronicleRepository, auth_dep) -> APIRouter:
    router = APIRouter(dependencies=[auth_dep])

    # ---------- 状态 ----------

    @router.get(
        "/api/status",
        response_model=StatusSnapshotResponse,
        response_model_exclude_unset=True,
    )
    async def get_status():
        return build_status_snapshot(ctx, repo)

    # ---------- 用量与余额统计 ----------

    @router.get(
        "/api/usage",
        response_model=UsageStatsResponse,
        response_model_exclude_unset=True,
    )
    async def usage_stats(
        granularity: str = "day",
        window: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        role: str | None = None,
    ):
        """用量聚合数据（统计页图表）。"""
        window = max(1, min(int(window), 365)) if window else None
        config = await _read_usage_config(ctx)
        try:
            return await get_usage_tracker().query_usage(
                granularity=granularity,
                window=window,
                start_date=start_date,
                end_date=end_date,
                provider_id=provider_id,
                model_id=model_id,
                role=role,
                config=config,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get(
        "/api/balance",
        response_model=BalancesResponse,
        response_model_exclude_unset=True,
    )
    async def deepseek_balance(force: bool = False, provider_id: str | None = None):
        """按供应商连接查询余额；模型和角色不会改变账户余额。"""
        config = await _read_usage_config(ctx)
        return await get_usage_tracker().get_provider_balances(config, provider_id, force_refresh=force)

    # ---------- 终端历史（只读回放） ----------

    @router.get(
        "/api/terminal/history",
        response_model=TerminalHistoryResponse,
        response_model_exclude_unset=True,
    )
    async def terminal_history(since: float = 0):
        """返回终端输出历史（带时间戳），供独立终端面板回放。"""
        return {"ok": True, "entries": ctx.web_adapter.get_terminal_history(since)}

    # ---------- 管理页：通用表浏览 ----------

    @router.get(
        "/api/admin/tables",
        response_model=AdminTablesResponse,
        response_model_exclude_unset=True,
    )
    async def admin_tables():
        return {"tables": repo.list_tables()}

    @router.get(
        "/api/admin/messages",
        response_model=AdminTableRowsResponse,
        response_model_exclude_unset=True,
    )
    async def admin_messages(
        offset: int = 0,
        limit: int = 50,
        role: str = "",
        platform: str = "",
        session_id: str = "",
        q: str = "",
    ):
        return repo.query_messages(
            offset=offset,
            limit=limit,
            role=role,
            platform=platform,
            session_id=session_id,
            q=q,
        )

    @router.delete(
        "/api/admin/messages/{msg_id}",
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    async def admin_delete_message(msg_id: int):
        repo.delete_message(msg_id)
        return {"ok": True}

    @router.get(
        "/api/admin/sessions",
        response_model=AdminTableRowsResponse,
        response_model_exclude_unset=True,
    )
    async def admin_sessions(offset: int = 0, limit: int = 50):
        return repo.query_sessions(offset=offset, limit=limit)

    @router.get(
        "/api/admin/summaries",
        response_model=AdminTableRowsResponse,
        response_model_exclude_unset=True,
    )
    async def admin_summaries(offset: int = 0, limit: int = 50):
        return repo.query_summaries(offset=offset, limit=limit)

    @router.delete(
        "/api/admin/summaries/{summary_id}",
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    async def admin_delete_summary(summary_id: int):
        repo.delete_summary(summary_id)
        return {"ok": True}

    # ---------- 管理页：goals / tasks ----------

    @router.get(
        "/api/admin/goals",
        response_model=AdminTableRowsResponse,
        response_model_exclude_unset=True,
    )
    async def admin_goals():
        return repo.query_goals()

    @router.post(
        "/api/admin/goals",
        response_model=GoalManagerOpResponse,
        response_model_exclude_unset=True,
    )
    async def admin_create_goal(body: GoalCreateBody):
        return await repo.apply_goal_ops(
            [{"op": "create_goal", "description": body.description, "priority": body.priority}]
        )

    @router.put(
        "/api/admin/goals/{goal_id}",
        response_model=GoalManagerOpResponse,
        response_model_exclude_unset=True,
    )
    async def admin_update_goal(goal_id: int, body: GoalUpdateBody):
        op: dict[str, Any] = {"op": "update_goal", "goal_id": goal_id}
        if body.description is not None:
            op["description"] = body.description
        if body.priority is not None:
            op["priority"] = body.priority
        if body.result is not None:
            op["result"] = body.result
        if body.status is not None:
            op["status"] = body.status
        return await repo.apply_goal_ops([op])

    @router.delete(
        "/api/admin/goals/{goal_id}",
        response_model=GoalManagerOpResponse,
        response_model_exclude_unset=True,
    )
    async def admin_archive_goal(goal_id: int):
        """goal 不物理删除，置为 inactive 归档。"""
        return await repo.apply_goal_ops([{"op": "update_goal", "goal_id": goal_id, "status": "inactive"}])

    @router.get(
        "/api/admin/tasks",
        response_model=AdminTableRowsResponse,
        response_model_exclude_unset=True,
    )
    async def admin_tasks():
        return repo.query_tasks()

    @router.post(
        "/api/admin/tasks",
        response_model=GoalManagerOpResponse,
        response_model_exclude_unset=True,
    )
    async def admin_create_task(body: TaskCreateBody):
        return await repo.apply_goal_ops(
            [{"op": "create_task", "goal_id": body.goal_id, "content": body.content, "urgency": body.urgency}]
        )

    @router.put(
        "/api/admin/tasks/{task_id}",
        response_model=GoalManagerOpResponse,
        response_model_exclude_unset=True,
    )
    async def admin_update_task(task_id: int, body: TaskUpdateBody):
        op: dict[str, Any] = {"op": "update_task", "task_id": task_id}
        if body.content is not None:
            op["content"] = body.content
        if body.urgency is not None:
            op["urgency"] = body.urgency
        if body.status is not None:
            op["status"] = body.status
        if body.goal_id is not None:
            op["goal_id"] = body.goal_id
        return await repo.apply_goal_ops([op])

    @router.delete(
        "/api/admin/tasks/{task_id}",
        response_model=GoalManagerOpResponse,
        response_model_exclude_unset=True,
    )
    async def admin_delete_task(task_id: int):
        return await repo.apply_goal_ops([{"op": "delete_task", "task_id": task_id}])

    # ---------- 设置页：自我修改备份回滚 ----------

    @router.get(
        "/api/admin/backups",
        response_model=AdminBackupsResponse,
        response_model_exclude_unset=True,
    )
    async def admin_backups(path: str | None = None):
        try:
            return {"items": await asyncio.to_thread(self_backup.list_backups, path)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/api/admin/backups/restore",
        response_model=AdminBackupRestoreResponse,
        response_model_exclude_unset=True,
    )
    async def admin_restore_backup(body: BackupRestoreBody):
        result = await asyncio.to_thread(self_backup.restore, body.path, body.timestamp, True)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "恢复失败")
        return result

    return router
