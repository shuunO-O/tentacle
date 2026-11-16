"""
目标管理模块

封装 agent 目标系统（goal/task）的数据库操作，提供：
- 查看活跃目标及其未完成任务
- 批量创建、修改、删除目标与任务

设计原则：
1. 所有数据库操作都是同步 sqlite3 调用，通过 asyncio.to_thread 包装避免阻塞事件循环。
2. 不主动抛异常，所有错误转为可读字符串返回。
3. goal 不物理删除，只能改为 inactive；task 可物理删除。
4. inactive 的 goal 与 completed 的 task 不可修改。
"""

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from agent.chronicle import ChronicleManager

logger = logging.getLogger(__name__)

_PRIORITY_ORDER = {"high": 3, "medium": 2, "low": 1}
_TASK_STATUS_ORDER = {"in_progress": 2, "pending": 1, "completed": 0}
_VALID_GOAL_STATUSES = {"active", "inactive"}
_VALID_TASK_STATUSES = {"pending", "in_progress", "completed"}
_VALID_PRIORITIES = {"high", "medium", "low"}


class GoalManager:
    """
    目标与任务管理器。

    所有公开接口均为 async，内部通过 asyncio.to_thread 调用同步 sqlite3。
    """

    def __init__(self, db_path: str = "data/chronicle.db"):
        """
        初始化目标管理器。

        Args:
            db_path: SQLite 数据库路径。
        """
        self.db_path = Path(db_path)
        # 复用 ChronicleManager 初始化表结构，确保 goals/tasks 表已存在
        ChronicleManager(str(self.db_path))._init_db()

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _priority_rank(self, priority: str) -> int:
        return _PRIORITY_ORDER.get(priority, 0)

    def _urgency_rank(self, urgency: str) -> int:
        return _PRIORITY_ORDER.get(urgency, 0)

    def _task_status_rank(self, status: str) -> int:
        return _TASK_STATUS_ORDER.get(status, 0)

    def _list_active_goals_sync(self, goal_id: int | None = None) -> list[dict]:
        """
        同步查询活跃目标及未完成任务。

        返回的 goal 包含 result 字段，不包含 start_time/end_time。
        返回的 task 包含 start_time，不包含 end_time。
        """
        with self._connect() as conn:
            if goal_id is not None:
                cursor = conn.execute(
                    "SELECT id, description, priority, status, result FROM goals WHERE status = 'active' AND id = ?",
                    (goal_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, description, priority, status, result FROM goals WHERE status = 'active'"
                )
            goals = [
                {
                    "id": row[0],
                    "description": row[1],
                    "priority": row[2],
                    "status": row[3],
                    "result": row[4] or "",
                    "tasks": [],
                }
                for row in cursor.fetchall()
            ]

            if not goals:
                return []

            goal_ids = [g["id"] for g in goals]
            placeholders = ",".join("?" * len(goal_ids))
            cursor = conn.execute(
                f"""
                SELECT id, content, status, goal_id, urgency, start_time
                FROM tasks
                WHERE goal_id IN ({placeholders})
                  AND status IN ('pending', 'in_progress')
                """,
                tuple(goal_ids),
            )
            tasks = [
                {
                    "id": row[0],
                    "content": row[1],
                    "status": row[2],
                    "goal_id": row[3],
                    "urgency": row[4],
                    "start_time": row[5],
                }
                for row in cursor.fetchall()
            ]

        goal_map = {g["id"]: g for g in goals}
        for task in tasks:
            if task["goal_id"] in goal_map:
                goal_map[task["goal_id"]]["tasks"].append(task)

        # goal 按 priority 降序
        goals.sort(key=lambda g: self._priority_rank(g["priority"]), reverse=True)

        # 每个 goal 的 task 先 in_progress 后 pending，再按 urgency 降序
        for goal in goals:
            goal["tasks"].sort(
                key=lambda t: (
                    self._task_status_rank(t["status"]),
                    self._urgency_rank(t["urgency"]),
                ),
                reverse=True,
            )

        return goals

    async def list_active_goals(self, goal_id: int | None = None) -> list[dict]:
        """
        查看活跃目标及未完成任务。

        Args:
            goal_id: 可选，只查看指定 goal。

        Returns:
            排序后的 goal 列表。
        """
        return await asyncio.to_thread(self._list_active_goals_sync, goal_id)

    def _ensure_goal_active(self, conn: sqlite3.Connection, goal_id: int) -> dict | None:
        """确认 goal 存在且 active，返回 goal 数据，否则返回 None。"""
        cursor = conn.execute(
            "SELECT id, description, priority, status, result FROM goals WHERE id = ? AND status = 'active'",
            (goal_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "description": row[1],
            "priority": row[2],
            "status": row[3],
            "result": row[4],
        }

    def _ensure_task_editable(self, conn: sqlite3.Connection, task_id: int) -> dict | None:
        """确认 task 存在且不是 completed，返回 task 数据，否则返回 None。"""
        cursor = conn.execute(
            "SELECT id, content, status, goal_id, urgency FROM tasks WHERE id = ? AND status != 'completed'",
            (task_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "content": row[1],
            "status": row[2],
            "goal_id": row[3],
            "urgency": row[4],
        }

    def _create_goal_sync(self, conn: sqlite3.Connection, op: dict) -> dict:
        """同步创建 goal。"""
        description = op.get("description", "").strip()
        priority = op.get("priority", "")

        if not description:
            return {"status": "error", "error": "create_goal 必须提供 description"}
        if priority not in _VALID_PRIORITIES:
            return {"status": "error", "error": f"priority 必须是 {_VALID_PRIORITIES} 之一"}

        cursor = conn.execute(
            """
            INSERT INTO goals (description, priority, status, result, start_time)
            VALUES (?, ?, 'active', '', ?)
            """,
            (description, priority, time.time()),
        )
        goal_id = cursor.lastrowid
        return {"status": "ok", "goal_id": goal_id}

    def _create_task_sync(self, conn: sqlite3.Connection, op: dict) -> dict:
        """同步创建 task，goal 必须存在且 active。"""
        goal_id = op.get("goal_id")
        content = op.get("content", "").strip()
        urgency = op.get("urgency", "")

        if goal_id is None:
            return {"status": "error", "error": "create_task 必须提供 goal_id"}
        try:
            goal_id = int(goal_id)
        except (ValueError, TypeError):
            return {"status": "error", "error": "goal_id 必须是整数"}
        if not content:
            return {"status": "error", "error": "create_task 必须提供 content"}
        if urgency not in _VALID_PRIORITIES:
            return {"status": "error", "error": f"urgency 必须是 {_VALID_PRIORITIES} 之一"}

        goal = self._ensure_goal_active(conn, goal_id)
        if goal is None:
            return {"status": "error", "error": f"goal_id={goal_id} 不存在或不是 active 状态"}

        cursor = conn.execute(
            """
            INSERT INTO tasks (content, status, goal_id, urgency, start_time)
            VALUES (?, 'pending', ?, ?, ?)
            """,
            (content, goal_id, urgency, time.time()),
        )
        task_id = cursor.lastrowid
        return {"status": "ok", "task_id": task_id}

    def _update_goal_sync(self, conn: sqlite3.Connection, op: dict) -> dict:
        """同步修改 goal，只允许 active 的 goal。"""
        goal_id = op.get("goal_id")
        if goal_id is None:
            return {"status": "error", "error": "update_goal 必须提供 goal_id"}
        try:
            goal_id = int(goal_id)
        except (ValueError, TypeError):
            return {"status": "error", "error": "goal_id 必须是整数"}

        goal = self._ensure_goal_active(conn, goal_id)
        if goal is None:
            return {"status": "error", "error": f"goal_id={goal_id} 不存在或不是 active 状态，无法修改"}

        updates = []
        params = []
        description = op.get("description")
        if description is not None:
            description = description.strip()
            if not description:
                return {"status": "error", "error": "goal 的 description 不能为空"}
            updates.append("description = ?")
            params.append(description)

        priority = op.get("priority")
        if priority is not None:
            if priority not in _VALID_PRIORITIES:
                return {"status": "error", "error": f"priority 必须是 {_VALID_PRIORITIES} 之一"}
            updates.append("priority = ?")
            params.append(priority)

        result = op.get("result")
        if result is not None:
            updates.append("result = ?")
            params.append(result)

        status = op.get("status")
        if status is not None:
            if status not in _VALID_GOAL_STATUSES:
                return {"status": "error", "error": f"goal 的 status 必须是 {_VALID_GOAL_STATUSES} 之一"}
            updates.append("status = ?")
            params.append(status)
            if status == "inactive":
                updates.append("end_time = ?")
                params.append(time.time())

        if not updates:
            return {"status": "error", "error": "update_goal 没有提供任何可修改字段"}

        params.append(goal_id)
        set_clause = ", ".join(updates)
        conn.execute(f"UPDATE goals SET {set_clause} WHERE id = ?", params)
        return {"status": "ok", "goal_id": goal_id}

    def _update_task_sync(self, conn: sqlite3.Connection, op: dict) -> dict:
        """同步修改 task，只允许 pending 或 in_progress 的 task。"""
        task_id = op.get("task_id")
        if task_id is None:
            return {"status": "error", "error": "update_task 必须提供 task_id"}
        try:
            task_id = int(task_id)
        except (ValueError, TypeError):
            return {"status": "error", "error": "task_id 必须是整数"}

        task = self._ensure_task_editable(conn, task_id)
        if task is None:
            return {"status": "error", "error": f"task_id={task_id} 不存在或已完成，无法修改"}

        updates = []
        params = []
        content = op.get("content")
        if content is not None:
            content = content.strip()
            if not content:
                return {"status": "error", "error": "task 的 content 不能为空"}
            updates.append("content = ?")
            params.append(content)

        urgency = op.get("urgency")
        if urgency is not None:
            if urgency not in _VALID_PRIORITIES:
                return {"status": "error", "error": f"urgency 必须是 {_VALID_PRIORITIES} 之一"}
            updates.append("urgency = ?")
            params.append(urgency)

        status = op.get("status")
        if status is not None:
            if status not in _VALID_TASK_STATUSES:
                return {"status": "error", "error": f"task 的 status 必须是 {_VALID_TASK_STATUSES} 之一"}
            updates.append("status = ?")
            params.append(status)
            if status == "completed":
                updates.append("end_time = ?")
                params.append(time.time())

        goal_id = op.get("goal_id")
        if goal_id is not None:
            try:
                goal_id = int(goal_id)
            except (ValueError, TypeError):
                return {"status": "error", "error": "goal_id 必须是整数"}
            goal = self._ensure_goal_active(conn, goal_id)
            if goal is None:
                return {"status": "error", "error": f"goal_id={goal_id} 不存在或不是 active 状态，无法迁移 task"}
            updates.append("goal_id = ?")
            params.append(goal_id)

        if not updates:
            return {"status": "error", "error": "update_task 没有提供任何可修改字段"}

        params.append(task_id)
        set_clause = ", ".join(updates)
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", params)
        return {"status": "ok", "task_id": task_id}

    def _delete_task_sync(self, conn: sqlite3.Connection, op: dict) -> dict:
        """同步删除 task。"""
        task_id = op.get("task_id")
        if task_id is None:
            return {"status": "error", "error": "delete_task 必须提供 task_id"}
        try:
            task_id = int(task_id)
        except (ValueError, TypeError):
            return {"status": "error", "error": "task_id 必须是整数"}

        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cursor.rowcount == 0:
            return {"status": "error", "error": f"task_id={task_id} 不存在"}
        return {"status": "ok", "task_id": task_id}

    def _update_sync(self, operations: list[dict]) -> dict:
        """同步执行一批操作。"""
        results = []
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                for i, op in enumerate(operations):
                    op_type = op.get("op", "")
                    if op_type == "create_goal":
                        result = self._create_goal_sync(conn, op)
                    elif op_type == "create_task":
                        result = self._create_task_sync(conn, op)
                    elif op_type == "update_goal":
                        result = self._update_goal_sync(conn, op)
                    elif op_type == "update_task":
                        result = self._update_task_sync(conn, op)
                    elif op_type == "delete_task":
                        result = self._delete_task_sync(conn, op)
                    else:
                        result = {"status": "error", "error": f"未知操作类型: {op_type}"}
                    result["index"] = i
                    results.append(result)
                    if result["status"] != "ok":
                        # 遇到错误停止后续操作，回滚
                        conn.rollback()
                        for previous in results[:-1]:
                            previous.clear()
                            previous.update(status="rolled_back")
                        return {"status": "error", "error": result["error"], "rolled_back": True,
                                "results": results}
                else:
                    # 全部成功才提交
                    conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"目标系统批量操作异常: {e}", exc_info=True)
                return {"status": "error", "error": f"批量操作失败: {str(e)}"}

        return {"status": "ok", "results": results}

    async def update(self, operations: list[dict]) -> dict:
        """
        执行批量目标/任务操作。

        Args:
            operations: 操作列表，每个操作包含 op 字段。

        Returns:
            操作结果字典，任何一步失败都会回滚。
        """
        return await asyncio.to_thread(self._update_sync, operations)

    def _get_goal_summary_sync(self, goal_id: int) -> dict | None:
        """同步获取 goal 摘要，用于内部查询。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, description, priority, status, result FROM goals WHERE id = ?",
                (goal_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "description": row[1],
                "priority": row[2],
                "status": row[3],
                "result": row[4],
            }

    async def get_goal_summary(self, goal_id: int) -> dict | None:
        """获取指定 goal 摘要。"""
        return await asyncio.to_thread(self._get_goal_summary_sync, goal_id)

    def _get_task_sync(self, task_id: int) -> dict | None:
        """同步获取 task 数据（含 completed），用于内部查询。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, content, status, goal_id, urgency FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "content": row[1],
                "status": row[2],
                "goal_id": row[3],
                "urgency": row[4],
            }

    async def get_task(self, task_id: int) -> dict | None:
        """获取指定 task 数据（含 completed）。"""
        return await asyncio.to_thread(self._get_task_sync, task_id)


# 全局单例实例
_goal_manager_instance: GoalManager | None = None


def get_goal_manager(db_path: str = "data/chronicle.db") -> GoalManager:
    """获取 GoalManager 单例实例。"""
    global _goal_manager_instance
    if _goal_manager_instance is None:
        _goal_manager_instance = GoalManager(db_path)
    return _goal_manager_instance
