"""
全局 ID 生成器

为记忆库各类节点（event/person/concept/location）生成持久化自增 ID。
在 data/chronicle.db 中维护 memory_id_seq 表，与 chronicle 共用同一数据库。
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class MemoryIdGenerator:
    """
    基于 SQLite 的自增 ID 生成器。

    使用 SQLite 的 RETURNING 子句保证并发安全，外层 asyncio.Lock 保证
    异步环境下的串行访问。
    """

    def __init__(self, db_path: str = "data/chronicle.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self):
        """初始化自增序列表"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_id_seq (
                        prefix TEXT PRIMARY KEY,
                        seq INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
        except Exception as e:
            logger.error(f"初始化 memory_id_seq 表失败: {e}", exc_info=True)
            raise

    async def next_id(self, prefix: str) -> str:
        """
        获取下一个 ID，格式为 {prefix}_{seq}。

        Args:
            prefix: ID 前缀，如 event、person、concept、location

        Returns:
            生成的唯一 ID 字符串
        """
        async with self._lock:

            def _get_seq() -> int:
                with sqlite3.connect(self.db_path) as conn:
                    # SQLite 3.35+ 支持 RETURNING
                    cur = conn.execute(
                        """
                        INSERT INTO memory_id_seq(prefix, seq)
                        VALUES (?, 1)
                        ON CONFLICT(prefix) DO UPDATE SET seq = seq + 1
                        RETURNING seq
                        """,
                        (prefix,),
                    )
                    row = cur.fetchone()
                    conn.commit()
                    return row[0]

            try:
                seq = await asyncio.to_thread(_get_seq)
                return f"{prefix}_{seq}"
            except Exception as e:
                logger.error(f"生成 ID 失败 [{prefix}]: {e}", exc_info=True)
                raise
