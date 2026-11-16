"""
对话归档模块

将用户与 Agent 的原始交互记录持久化到 SQLite，为 session 管理、压缩摘要提供数据支持。

设计原则：
1. 只记录 role 为 'user' 或 'assistant' 的实际对话内容。
2. 不记录 system、tool、tool_calls 消息；心跳 user 消息由 AgentSession 显式归档。
3. 归档及关键状态写入失败向调用方报告，避免把失败当作保存成功。
4. session_id 随 session 系统接入后真实写入。
"""

import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


class ChronicleManager:
    """
    原始对话归档管理器

    负责维护 chronicle.db 数据库，提供按 session 写入原始消息、查询历史、读写压缩摘要的能力。
    """

    def __init__(self, db_path: str = "data/chronicle.db"):
        """
        初始化归档管理器

        Args:
            db_path: SQLite 数据库路径，默认放在 data/chronicle.db
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            # 原始对话消息表
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'unknown',
                    created_time REAL NOT NULL
                )
                """
            )
            # 迁移：为旧表增加 platform 字段
            try:
                conn.execute("ALTER TABLE raw_messages ADD COLUMN platform TEXT NOT NULL DEFAULT 'unknown'")
            except sqlite3.OperationalError:
                # 字段已存在，忽略
                pass
            # 迁移：为旧表增加 images 字段（JSON 数组，存图片本地引用）
            try:
                conn.execute("ALTER TABLE raw_messages ADD COLUMN images TEXT")
            except sqlite3.OperationalError:
                # 字段已存在，忽略
                pass
            # 迁移：同一轮对话共享 round_id，供严格回退整轮使用
            try:
                conn.execute("ALTER TABLE raw_messages ADD COLUMN round_id TEXT")
            except sqlite3.OperationalError:
                # 字段已存在，忽略
                pass
            # session 元数据表
            try:
                conn.execute("ALTER TABLE raw_messages ADD COLUMN delivery TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE raw_messages ADD COLUMN interrupted INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    status TEXT NOT NULL
                )
                """
            )
            # 压缩摘要表：按天/按事件存储
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL NOT NULL
                )
                """
            )
            # 目标表：长期目标
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    description TEXT NOT NULL,
                    priority TEXT NOT NULL CHECK(priority IN ('high', 'medium', 'low')),
                    status TEXT NOT NULL CHECK(status IN ('active', 'inactive')),
                    result TEXT NOT NULL DEFAULT '',
                    start_time REAL NOT NULL,
                    end_time REAL
                )
                """
            )
            # 任务表：隶属于 goal 的具体任务
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'completed')),
                    goal_id INTEGER NOT NULL,
                    urgency TEXT NOT NULL CHECK(urgency IN ('high', 'medium', 'low')),
                    start_time REAL NOT NULL,
                    end_time REAL,
                    FOREIGN KEY (goal_id) REFERENCES goals(id)
                )
                """
            )

    def append_messages(
        self,
        session_id: str,
        messages: list[dict],
        default_platform: str = "unknown",
        round_id: str | None = None,
    ):
        """
        批量写入原始对话消息

        会自动过滤：
        - role 不是 'user' 或 'assistant' 的消息
        - content 不是非空字符串的消息（数组 content 会提取文本部分）

        Args:
            session_id: 会话标识
            messages: 待写入的消息列表，每个消息为 {"role": ..., "content": ...,
                "platform": ..., "images": [{local_id, path, mime, filename}]} 字典，
                images 可选，图片的本地引用（与远端 file_id 解耦）
            default_platform: 消息未指定 platform 时的默认值
            round_id: 可选的轮次标识；同一轮消息共享，用于整轮回退
        """
        round_id = round_id or uuid.uuid4().hex
        records = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            platform = msg.get("platform") or default_platform

            if role not in ("user", "assistant"):
                continue

            # 多模态 content（数组）提取文本部分归档
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") in ("text", "input_text")
                )

            if not isinstance(content, str) or not content.strip():
                continue

            images = msg.get("images")
            images_json = json.dumps(images, ensure_ascii=False) if images else None

            delivery = json.dumps(msg['delivery'], ensure_ascii=False) if msg.get('delivery') else None
            records.append((session_id, role, content, platform, time.time(), images_json, round_id, delivery,
                            int(bool(msg.get("interrupted")))))

        if not records:
            return

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT INTO raw_messages "
                    "(session_id, role, content, platform, created_time, images, round_id, delivery, interrupted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    records,
                )
                conn.commit()
        except Exception as e:
            logger.error(f"归档原始对话失败: {e}", exc_info=True)
            raise

    def rollback_round(
        self,
        round_id: str,
        before_session_id: str,
        after_session_id: str,
        before_start_time: float,
    ) -> int:
        """原子删除一轮归档；必要时撤销该轮触发的自动 session 切换。"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM raw_messages WHERE round_id = ?",
                (round_id,),
            )
            deleted = cursor.rowcount
            if after_session_id != before_session_id:
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (after_session_id,))
                conn.execute(
                    """
                    INSERT INTO sessions (session_id, start_time, end_time, status)
                    VALUES (?, ?, NULL, 'active')
                    ON CONFLICT(session_id) DO UPDATE SET
                        start_time = excluded.start_time,
                        end_time = NULL,
                        status = 'active'
                    """,
                    (before_session_id, before_start_time),
                )
            conn.commit()
        return deleted

    def get_recent_rounds(self, session_id: str = "", limit: int = 10, *,
                          conversation_only: bool = False, strict: bool = False) -> list[dict]:
        """按自增 ID 返回最近 N 轮的全部消息；同轮可以有多条助手输出。"""
        if limit <= 0:
            return []
        try:
            where = "role IN ('user', 'assistant')"
            params = []
            if session_id:
                where += " AND session_id = ?"
                params.append(session_id)
            with sqlite3.connect(self.db_path) as conn:
                if conversation_only:
                    # 用真实用户输入选择整轮，不能只过滤 system 消息而留下心跳的 assistant 输出。
                    # 无 round_id 的旧记录按同 session 内最近的 user 输入归组。
                    rows = conn.execute(
                        f"""
                        WITH grouped AS (
                            SELECT *, CASE WHEN round_id IS NOT NULL THEN 'round:' || round_id
                                ELSE 'legacy:' || MAX(CASE WHEN role = 'user' THEN id END)
                                    OVER (PARTITION BY session_id ORDER BY id)
                                END AS round_key
                            FROM raw_messages WHERE {where}
                        ), recent AS (
                            SELECT round_key FROM grouped
                            WHERE role = 'user' AND platform != 'system'
                            GROUP BY round_key ORDER BY MIN(id) DESC LIMIT ?
                        )
                        SELECT id, role, content, platform, created_time, images, round_id, delivery, interrupted
                        FROM grouped WHERE round_key IN (SELECT round_key FROM recent)
                        ORDER BY id ASC
                        """, (*params, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        WITH recent AS (
                            SELECT COALESCE(round_id, id) AS round_key
                            FROM raw_messages WHERE {where} GROUP BY COALESCE(round_id, id)
                            ORDER BY MIN(id) DESC LIMIT ?
                        )
                        SELECT id, role, content, platform, created_time, images, round_id, delivery, interrupted FROM raw_messages
                        WHERE {where} AND COALESCE(round_id, id) IN (SELECT round_key FROM recent)
                        ORDER BY id ASC
                        """,
                        (*params, limit, *params),
                    ).fetchall()
            return [
                {
                    "id": row[0], "role": row[1], "content": row[2], "platform": row[3],
                    "created_time": row[4], "images": self._parse_images(row[5]), "round_id": row[6],
                    **({'delivery': json.loads(row[7])} if row[7] else {}),
                    **({"interrupted": True} if row[8] else {}),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"查询最近对话失败: {e}", exc_info=True)
            if strict:
                raise
            return []

    @staticmethod
    def _parse_images(images_json) -> list[dict] | None:
        """解析归档的 images JSON 字段，损坏时返回 None（不影响消息本体）"""
        if not images_json:
            return None
        try:
            parsed = json.loads(images_json)
            return parsed if isinstance(parsed, list) else None
        except (json.JSONDecodeError, TypeError):
            return None

    def get_summaries(self, start_time: float, end_time: float, *, strict: bool = False) -> list[dict]:
        """
        获取某段时间内的压缩摘要

        Args:
            start_time: 开始时间戳
            end_time: 结束时间戳

        Returns:
            摘要字典列表，每个字典包含 id、summary、start_time、end_time
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT id, summary, start_time, end_time FROM summaries
                    WHERE start_time >= ? AND start_time <= ?
                    ORDER BY start_time ASC
                    """,
                    (start_time, end_time),
                )
                rows = cursor.fetchall()
            return [
                {"id": row[0], "summary": row[1], "start_time": row[2], "end_time": row[3]}
                for row in rows
            ]
        except Exception as e:
            logger.error(f"查询摘要失败: {e}", exc_info=True)
            if strict:
                raise
            return []

    def get_messages_by_id_range(self, start_id: int, end_id: int) -> list[dict]:
        """
        根据 id 范围获取 user 和 assistant 消息，用于压缩摘要。

        Args:
            start_id: 起始消息 id（包含）
            end_id: 结束消息 id（包含）

        Returns:
            消息字典列表，每个字典包含 id、role、content、platform、created_time、images
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT id, role, content, platform, created_time, images FROM raw_messages
                    WHERE id >= ? AND id <= ? AND role IN ('user', 'assistant')
                    ORDER BY id ASC
                    """,
                    (start_id, end_id),
                )
                rows = cursor.fetchall()
            return [
                {
                    "id": row[0],
                    "role": row[1],
                    "content": row[2],
                    "platform": row[3],
                    "created_time": row[4],
                    "images": self._parse_images(row[5]),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"按 id 范围查询消息失败: {e}", exc_info=True)
            return []

    def get_messages_after_id(self, message_id: int, limit: int = 100) -> list[dict]:
        """
        获取某条消息之后的 user/assistant 消息

        Args:
            message_id: 起始消息 id（不包含）
            limit: 最大返回条数

        Returns:
            消息字典列表，每个字典包含 id、role、content、platform、created_time、images
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT id, role, content, platform, created_time, images FROM raw_messages
                    WHERE id > ? AND role IN ('user', 'assistant')
                    ORDER BY id ASC LIMIT ?
                    """,
                    (message_id, limit),
                )
                rows = cursor.fetchall()
            return [
                {
                    "id": row[0],
                    "role": row[1],
                    "content": row[2],
                    "platform": row[3],
                    "created_time": row[4],
                    "images": self._parse_images(row[5]),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"按 id 查询消息失败: {e}", exc_info=True)
            return []

    def get_latest_message_id(self) -> int:
        """
        获取当前最新的 raw_message id

        Returns:
            最新 id，没有数据时返回 0
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT MAX(id) FROM raw_messages")
                row = cursor.fetchone()
            return row[0] or 0
        except Exception as e:
            logger.error(f"查询最新消息 id 失败: {e}", exc_info=True)
            return 0

    def get_messages_by_time_range(
        self, start_time: float, end_time: float
    ) -> list[dict]:
        """
        按时间范围获取 user 和 assistant 消息，用于按天压缩摘要

        Args:
            start_time: 开始时间戳（包含）
            end_time: 结束时间戳（包含）

        Returns:
            消息字典列表，每个字典包含 id、role、content、platform、created_time、images
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT id, role, content, platform, created_time, images FROM raw_messages
                    WHERE created_time >= ? AND created_time <= ?
                      AND role IN ('user', 'assistant')
                    ORDER BY id ASC
                    """,
                    (start_time, end_time),
                )
                rows = cursor.fetchall()
            return [
                {
                    "id": row[0],
                    "role": row[1],
                    "content": row[2],
                    "platform": row[3],
                    "created_time": row[4],
                    "images": self._parse_images(row[5]),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"按时间范围查询消息失败: {e}", exc_info=True)
            raise

    def get_earliest_message_time(self) -> float | None:
        """
        获取最早的 raw_message 时间戳

        Returns:
            时间戳，没有数据时返回 None
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT MIN(created_time) FROM raw_messages WHERE role IN ('user', 'assistant')"
                )
                row = cursor.fetchone()
            return row[0]
        except Exception as e:
            logger.error(f"查询最早消息时间失败: {e}", exc_info=True)
            raise

    def get_latest_user_platform(self) -> str:
        """
        获取最近一条 user 消息的平台标识

        Returns:
            platform 字符串，没有数据时返回 'unknown'
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT platform FROM raw_messages
                    WHERE role = 'user'
                    ORDER BY created_time DESC LIMIT 1
                    """
                )
                row = cursor.fetchone()
            return row[0] if row else "unknown"
        except Exception as e:
            logger.error(f"查询最近用户平台失败: {e}", exc_info=True)
            return "unknown"

    def get_session_message_range(self, session_id: str) -> tuple[int, int]:
        """
        获取某个 session 的 raw_message id 范围

        Args:
            session_id: 会话标识

        Returns:
            (min_id, max_id)，没有数据时返回 (0, 0)
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT MIN(id), MAX(id) FROM raw_messages
                    WHERE session_id = ? AND role = 'user'
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
            min_id, max_id = row[0] or 0, row[1] or 0
            return min_id, max_id
        except Exception as e:
            logger.error(f"查询 session 消息范围失败: {e}", exc_info=True)
            return 0, 0

    def append_summaries(self, summaries: list[dict]):
        """
        批量写入压缩摘要

        Args:
            summaries: 摘要字典列表，每个字典包含 summary、start_time、end_time
        """
        records = []
        for s in summaries:
            summary = s.get("summary", "")
            start_time = s.get("start_time", 0.0)
            end_time = s.get("end_time", 0.0)
            if not summary:
                continue
            records.append((summary, start_time, end_time))

        if not records:
            return

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT INTO summaries (summary, start_time, end_time) VALUES (?, ?, ?)",
                    records,
                )
                conn.commit()
        except Exception as e:
            logger.error(f"归档摘要失败: {e}", exc_info=True)
            raise

    @contextmanager
    def session_transition(self, session_id: str, start_time: float):
        """在单个事务内结束旧 session 并插入新记录；调用方在提交前发布缓存。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE sessions SET status = 'inactive', end_time = ? WHERE status = 'active'",
                         (start_time,))
            # 新 session 必须插入，ID 碰撞不能覆盖旧记录。
            conn.execute("INSERT INTO sessions (session_id, start_time, end_time, status) VALUES (?, ?, NULL, 'active')",
                         (session_id, start_time))
            yield

    def upsert_session(self, session_id: str, start_time: float, end_time: float | None, status: str):
        """
        插入或更新 session 元数据

        Args:
            session_id: 会话标识
            start_time: 开始时间戳
            end_time: 结束时间戳，未结束时传 None
            status: 会话状态
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (session_id, start_time, end_time, status)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        end_time = excluded.end_time,
                        status = excluded.status
                    """,
                    (session_id, start_time, end_time, status),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"保存 session 元数据失败: {e}", exc_info=True)

    def get_active_session(self) -> dict | None:
        """
        获取当前 active 的 session 元数据（按开始时间取最新一条）

        Returns:
            session 字典，如果没有 active session 则返回 None
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT session_id, start_time, end_time, status FROM sessions
                    WHERE status = 'active'
                    ORDER BY start_time DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
            if row:
                return {
                    "session_id": row[0],
                    "start_time": row[1],
                    "end_time": row[2],
                    "status": row[3],
                }
            return None
        except Exception as e:
            logger.error(f"查询 active session 失败: {e}", exc_info=True)
            return None

    def get_active_sessions(self) -> list[dict]:
        """
        获取所有 active 的 session 元数据

        Returns:
            session 字典列表，按开始时间倒序
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT session_id, start_time, end_time, status FROM sessions
                    WHERE status = 'active'
                    ORDER BY start_time DESC
                    """
                )
                rows = cursor.fetchall()
            return [
                {
                    "session_id": row[0],
                    "start_time": row[1],
                    "end_time": row[2],
                    "status": row[3],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"查询 active sessions 失败: {e}", exc_info=True)
            return []

    def get_session(self, session_id: str) -> dict | None:
        """
        根据 session_id 获取 session 元数据

        Args:
            session_id: 会话标识

        Returns:
            session 字典，不存在则返回 None
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT session_id, start_time, end_time, status FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
            if row:
                return {
                    "session_id": row[0],
                    "start_time": row[1],
                    "end_time": row[2],
                    "status": row[3],
                }
            return None
        except Exception as e:
            logger.error(f"查询 session 失败: {e}", exc_info=True)
            return None
