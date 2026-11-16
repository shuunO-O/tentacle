"""Chronicle SQLite 管理查询与 GoalManager 写入仓储。"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import HTTPException

ADMIN_TABLES = frozenset({"raw_messages", "sessions", "summaries", "goals", "tasks", "files", "usage_logs"})


def _rows(db_path: str, sql: str, params: tuple[Any, ...] = ()) -> tuple[list[str], list[list[Any]]]:
    """执行只读/管理 SQL 查询，返回 (columns, rows)。"""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()]
    return cols, rows


def table_count(db_path: str, table: str) -> int:
    if table not in ADMIN_TABLES:
        return 0
    try:
        _, rows = _rows(db_path, f"SELECT COUNT(*) FROM {table}")
        return int(rows[0][0]) if rows else 0
    except Exception:
        return 0


class ChronicleRepository:
    def __init__(self, db_path: str, goal_manager: Any = None):
        self.db_path = db_path
        self.goal_manager = goal_manager

    def table_counts_map(self) -> dict[str, int]:
        return {t: table_count(self.db_path, t) for t in sorted(ADMIN_TABLES)}

    def list_tables(self) -> list[dict[str, Any]]:
        return [{"name": t, "count": table_count(self.db_path, t)} for t in sorted(ADMIN_TABLES)]

    def query_messages(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        role: str = "",
        platform: str = "",
        session_id: str = "",
        q: str = "",
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        where: list[str] = []
        params: list[Any] = []
        if role:
            where.append("role = ?")
            params.append(role)
        if platform:
            where.append("platform = ?")
            params.append(platform)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if q:
            where.append("content LIKE ?")
            params.append(f"%{q}%")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        _, total_rows = _rows(self.db_path, f"SELECT COUNT(*) FROM raw_messages{where_sql}", tuple(params))
        cols, rows = _rows(
            self.db_path,
            f"SELECT id, session_id, role, content, platform, created_time FROM raw_messages"
            f"{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        return {"columns": cols, "rows": rows, "total": total_rows[0][0] if total_rows else 0}

    def delete_message(self, msg_id: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM raw_messages WHERE id = ?", (msg_id,))
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="消息不存在")

    def query_sessions(self, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        cols, rows = _rows(
            self.db_path,
            "SELECT session_id, start_time, end_time, status FROM sessions ORDER BY start_time DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        _, total = _rows(self.db_path, "SELECT COUNT(*) FROM sessions")
        return {"columns": cols, "rows": rows, "total": total[0][0] if total else 0}

    def query_summaries(self, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        cols, rows = _rows(
            self.db_path,
            "SELECT id, summary, start_time, end_time FROM summaries ORDER BY start_time DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        _, total = _rows(self.db_path, "SELECT COUNT(*) FROM summaries")
        return {"columns": cols, "rows": rows, "total": total[0][0] if total else 0}

    def delete_summary(self, summary_id: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM summaries WHERE id = ?", (summary_id,))
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="摘要不存在")

    def query_goals(self) -> dict[str, Any]:
        cols, rows = _rows(
            self.db_path,
            "SELECT id, description, priority, status, result, start_time, end_time FROM goals ORDER BY id DESC",
        )
        return {"columns": cols, "rows": rows}

    def query_tasks(self) -> dict[str, Any]:
        cols, rows = _rows(
            self.db_path,
            "SELECT id, content, status, goal_id, urgency, start_time, end_time FROM tasks ORDER BY id DESC",
        )
        return {"columns": cols, "rows": rows}

    @staticmethod
    def _check_goal_result(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("status") != "ok":
            raise HTTPException(status_code=400, detail=result.get("error") or "操作失败")
        for item in result.get("results") or []:
            if item.get("status") != "ok":
                raise HTTPException(status_code=400, detail=item.get("error") or "操作失败")
        return result

    async def apply_goal_ops(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        result = await self.goal_manager.update(ops)
        return self._check_goal_result(result)
