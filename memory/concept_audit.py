"""Concept 判定与操作审计。惰性建表，不扫描图谱，也不重放未完成操作。"""

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class ConceptAuditLog:
    def __init__(self, path="data/concept_dedup.db"):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS concept_audit (
                id TEXT PRIMARY KEY, created REAL NOT NULL, finished REAL,
                actor TEXT NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL,
                status TEXT NOT NULL, reference TEXT, parent_id TEXT, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS concept_audit_actor_created ON concept_audit(actor, created DESC);
            CREATE INDEX IF NOT EXISTS concept_audit_created ON concept_audit(created DESC);
        """)
        try:
            with db:
                yield db
        finally:
            db.close()

    async def begin(self, *, actor, source, action, data, reference=None, parent_id=None):
        if actor not in {"automatic", "manual"}:
            raise ValueError("未知审计分类")
        entry_id = uuid.uuid4().hex
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False)

        def write():
            with self._connect() as db:
                db.execute("INSERT INTO concept_audit VALUES(?,?,NULL,?,?,?,'running',?,?,?)",
                           (entry_id, time.time(), actor, source, action, reference, parent_id, payload))
        await asyncio.to_thread(write)
        return entry_id

    async def finish(self, entry_id, status, data):
        if status not in {"succeeded", "failed", "cancelled", "unknown"}:
            raise ValueError("未知审计结果")

        def write():
            with self._connect() as db:
                self.finish_in_transaction(db, entry_id, status, data)
        await asyncio.to_thread(write)

    @staticmethod
    def finish_in_transaction(db, entry_id, status, data):
        """与审核状态共用 SQLite 事务，避免图提交后分开确认审计与审核状态。"""
        row = db.execute("SELECT data,status FROM concept_audit WHERE id=?", (entry_id,)).fetchone()
        if row is None or row["status"] != "running":
            raise ValueError("审计记录不存在或已经完成")
        payload = json.loads(row["data"])
        payload.update(data)
        db.execute("UPDATE concept_audit SET finished=?,status=?,data=? WHERE id=?",
                   (time.time(), status, json.dumps(payload, ensure_ascii=False, allow_nan=False), entry_id))

    async def list(self, actor="all", offset=0):
        if actor not in {"all", "automatic", "manual"} or type(offset) is not int or offset < 0:
            raise ValueError("无效的审计筛选或分页")

        def read():
            with self._connect() as db:
                counts = {row["actor"]: row["n"] for row in db.execute(
                    "SELECT actor,count(*) AS n FROM concept_audit GROUP BY actor")}
                where, params = ("", ()) if actor == "all" else ("WHERE actor=?", (actor,))
                rows = db.execute(f"SELECT * FROM concept_audit {where} ORDER BY created DESC,id DESC LIMIT 30 OFFSET ?",
                                  (*params, offset)).fetchall()
                total = db.execute(f"SELECT count(*) FROM concept_audit {where}", params).fetchone()[0]
                entries = [dict(row) for row in rows]
                for row in entries:
                    row["data"] = json.loads(row["data"])
                return {"entries": entries, "counts": counts, "total": total, "offset": offset}
        return await asyncio.to_thread(read)
