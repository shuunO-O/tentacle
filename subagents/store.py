"""子任务及逐轮结果的独立 SQLite 存储；不写主叙事流数据库。"""

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class TaskStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise ValueError("子代理任务库版本较新，拒绝用旧程序修改")
            # 保留旧版需要的 progress 列，便于回退。
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, objective TEXT NOT NULL, background TEXT NOT NULL,
                    platform TEXT NOT NULL, parent_session TEXT,
                    status TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                    history TEXT NOT NULL DEFAULT '[]', progress TEXT NOT NULL DEFAULT '',
                    cleanup_error TEXT, workspace TEXT, dsh_session_id TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                    message TEXT NOT NULL, status TEXT NOT NULL, delivery TEXT NOT NULL,
                    wake_depth INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '', error TEXT, usage TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS turns_task ON turns(task_id, id);
            """)
            # 兼容开发阶段已生成的早期任务库，保留历史/结果。
            task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            turn_columns = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
            if "cleanup_error" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN cleanup_error TEXT")
            for name in ("workspace", "dsh_session_id"):
                if name not in task_columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")
            if "acp_session_id" in task_columns and "dsh_session_id" not in task_columns:
                conn.execute("UPDATE tasks SET dsh_session_id=acp_session_id")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_dsh_session ON tasks(dsh_session_id) "
                         "WHERE dsh_session_id IS NOT NULL AND archived=0")
            if "wake_depth" not in turn_columns:
                conn.execute("ALTER TABLE turns ADD COLUMN wake_depth INTEGER NOT NULL DEFAULT 0")
            conn.execute("PRAGMA user_version=1")
            now = time.time()
            # 不自动补跑；上次没有观察到最终状态的任务均等待显式追问。
            conn.execute("UPDATE tasks SET status='interrupted', updated_at=? "
                         "WHERE status IN ('running', 'queued') AND archived=0", (now,))
            conn.execute("UPDATE turns SET status='interrupted', error='运行时已停止；未自动重放命令', "
                         "finished_at=?, delivery='pending' WHERE status IN ('running', 'queued')", (now,))
            # 主 agent 可能已收到但尚未确认；重启不重复主动触发同一轮。
            conn.execute("UPDATE turns SET delivery='delivery_unknown' WHERE delivery='delivering'")
            conn.execute("UPDATE turns SET delivery='suppressed' WHERE delivery='pending' "
                         "AND task_id IN (SELECT id FROM tasks WHERE workspace IS NULL)")
        self.path.chmod(0o600)

    def transaction(self, operation):
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return operation(conn)

    @staticmethod
    def decode_task(row):
        result = dict(row)
        result["history"] = json.loads(result["history"])
        result["archived"] = bool(result["archived"])
        return result

    @staticmethod
    def decode_turn(row):
        result = dict(row)
        result["usage"] = json.loads(result["usage"])
        return result
