"""Durable, device-scoped phone actions and round tracking for HarmonyOS companion."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


current_phone: ContextVar[tuple | None] = ContextVar("current_phone", default=None)


class PhoneService:
    """持久化设备注册、闹钟动作队列与实况窗回合状态。"""

    def __init__(self, path, broadcast=None, push=None):
        self.path = Path(path)
        self.broadcast = broadcast
        self.push = push
        self.guards = {}
        self.waiters = {}
        self._ready = False

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            if not self._ready:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS phone_devices (
                    id TEXT PRIMARY KEY, secret TEXT NOT NULL, owner TEXT NOT NULL,
                    token TEXT NOT NULL, timezone TEXT NOT NULL, alarms INTEGER NOT NULL,
                    live INTEGER NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS phone_actions (
                    id TEXT PRIMARY KEY, device TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS phone_action_device ON phone_actions(device, status);
                CREATE TABLE IF NOT EXISTS phone_rounds (
                    request TEXT PRIMARY KEY, device TEXT NOT NULL, owner TEXT NOT NULL, activity INTEGER NOT NULL,
                    event TEXT NOT NULL, status TEXT NOT NULL, push_status TEXT NOT NULL,
                    updated REAL NOT NULL);
                """)
                conn.execute("UPDATE phone_rounds SET status='unknown', push_status='interrupted' WHERE status='thinking'")
                conn.commit()
                self._ready = True
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def register(self, device, secret, owner, token, timezone, alarms, live, guard):
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
        with self.db() as db:
            existing = db.execute("SELECT secret, owner FROM phone_devices WHERE id=?", (device,)).fetchone()
            if existing and existing["secret"] != self.digest(secret):
                raise PermissionError("设备凭证不匹配")
            if existing and existing["owner"] != owner:
                db.execute("UPDATE phone_actions SET status='cancelled' WHERE device=? AND status='pending'", (device,))
            db.execute(
                "INSERT OR REPLACE INTO phone_devices VALUES (?,?,?,?,?,?,?,?)",
                (device, self.digest(secret), owner, token, timezone, alarms, live, time.time()),
            )
        self.guards[device] = guard

    def authorize(self, device, secret, owner):
        with self.db() as db:
            row = db.execute("SELECT * FROM phone_devices WHERE id=?", (device,)).fetchone()
        if not row or row["secret"] != self.digest(secret) or row["owner"] != owner:
            raise PermissionError("手机未注册或登录已变化，请重新连接")
        return dict(row)

    def unregister(self, device):
        with self.db() as db:
            db.execute("UPDATE phone_devices SET token='', alarms=0, live=0 WHERE id=?", (device,))
            db.execute("UPDATE phone_actions SET status='cancelled' WHERE device=? AND status='pending'", (device,))
        self.guards.pop(device, None)

    def available(self, device):
        guard = self.guards.get(device)
        if guard is None or not guard():
            raise PermissionError("手机授权已失效，请打开客户端重新连接")

    def state(self, device):
        self.available(device)
        with self.db() as db:
            db.execute(
                "UPDATE phone_actions SET status='expired' WHERE device=? AND status='pending' AND expires<=?",
                (device, time.time()),
            )
            actions = db.execute(
                "SELECT * FROM phone_actions WHERE device=? AND status='pending' ORDER BY rowid LIMIT 32",
                (device,),
            ).fetchall()
            rounds = db.execute(
                "SELECT request AS request_id, activity AS activity_id, event, status, push_status "
                "FROM phone_rounds WHERE device=? AND owner=(SELECT owner FROM phone_devices WHERE id=?) "
                "ORDER BY updated DESC LIMIT 32",
                (device, device),
            ).fetchall()
        return {
            "actions": [{**json.loads(r["payload"]), "id": r["id"], "expires_at": r["expires"] * 1000} for r in actions],
            "rounds": [dict(r) for r in rounds],
        }

    def acknowledge(self, device, action_id, status, result):
        self.available(device)
        with self.db() as db:
            row = db.execute(
                "SELECT status, result FROM phone_actions WHERE id=? AND device=?",
                (action_id, device),
            ).fetchone()
            if not row:
                raise LookupError("操作不存在")
            if row["status"] == "pending":
                db.execute("UPDATE phone_actions SET status=?, result=? WHERE id=?", (status, result, action_id))
            elif row["status"] != status or row["result"] != result:
                raise ValueError("操作已有终态，不可覆盖")
        waiter = self.waiters.get(action_id)
        if waiter:
            waiter.set()

    def action(self, device, action_id):
        with self.db() as db:
            db.execute(
                "UPDATE phone_actions SET status='expired' WHERE id=? AND device=? AND status='pending' AND expires<=?",
                (action_id, device, time.time()),
            )
            row = db.execute(
                "SELECT status, result FROM phone_actions WHERE id=? AND device=?",
                (action_id, device),
            ).fetchone()
        if not row:
            raise LookupError("操作不存在")
        return {"action_id": action_id, "status": row["status"], "result": row["result"]}

    async def alarm(self, device, args, *, wait_seconds=12, expected_owner=None):
        self.available(device)
        if expected_owner is not None:
            with self.db() as db:
                row = db.execute("SELECT owner FROM phone_devices WHERE id=?", (device,)).fetchone()
            if row is None or row["owner"] != expected_owner:
                raise PermissionError("本轮手机登录已变化，不能向新的登录会话下发闹钟")
        if args.get("operation") == "status":
            return self.action(device, args.get("action_id", ""))
        if args.get("operation", "create") != "create":
            raise ValueError("仅支持 create 和 status")
        with self.db() as db:
            phone = db.execute("SELECT alarms FROM phone_devices WHERE id=?", (device,)).fetchone()
        if not phone or not phone["alarms"]:
            raise ValueError("手机尚未开启 Agent 闹钟，请先在客户端设置中开启")
        title = args.get("title", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError("闹钟标题应为 1 至 100 字")
        value = args.get("at", "")
        try:
            target = datetime.fromisoformat(value)
            if target.utcoffset() is None:
                raise ValueError()
            due = target.timestamp()
        except (ValueError, TypeError):
            raise ValueError("at 必须是带时区偏移的 ISO 时间，如 2026-11-04T09:00:00+08:00") from None
        if not time.time() + 10 < due < time.time() + 366 * 86400:
            raise ValueError("闹钟时间须在未来 10 秒至一年之间")
        action_id = uuid.uuid4().hex
        payload = {"kind": "alarm.create", "title": title.strip(), "at": due * 1000}
        with self.db() as db:
            db.execute(
                "UPDATE phone_actions SET status='expired' WHERE device=? AND status='pending' AND expires<=?",
                (device, time.time()),
            )
            pending = db.execute(
                "SELECT COUNT(*) FROM phone_actions WHERE device=? AND status='pending'",
                (device,),
            ).fetchone()[0]
            if pending >= 32:
                raise ValueError("待执行闹钟过多，请先打开手机处理")
            db.execute(
                "INSERT INTO phone_actions VALUES (?,?,?,'pending','',?)",
                (action_id, device, json.dumps(payload, ensure_ascii=False), min(due - 5, time.time() + 86400)),
            )
        waiter = asyncio.Event()
        self.waiters[action_id] = waiter
        try:
            if self.broadcast:
                await self.broadcast({"type": "phone_action", "data": {"device_id": device}})
            try:
                await asyncio.wait_for(waiter.wait(), wait_seconds)
            except TimeoutError:
                pass
            return self.action(device, action_id)
        finally:
            self.waiters.pop(action_id, None)

    def begin_round(self, device, request, activity, event):
        self.available(device)
        with self.db() as db:
            existing = db.execute("SELECT 1 FROM phone_rounds WHERE request=?", (request,)).fetchone()
            if existing:
                raise ValueError("本次请求已登记，请勿重复发送")
            owner = db.execute("SELECT owner FROM phone_devices WHERE id=?", (device,)).fetchone()[0]
            db.execute(
                "INSERT INTO phone_rounds VALUES (?,?,?,?,?,?,'pending',?)",
                (request, device, owner, activity, event, "thinking", time.time()),
            )

    def finish_round(self, request, status):
        if not self._ready:
            return
        with self.db() as db:
            db.execute(
                "UPDATE phone_rounds SET status=?, updated=? WHERE request=?",
                (status, time.time(), request),
            )

    async def push_round(self, request):
        with self.db() as db:
            row = db.execute(
                "SELECT r.*, d.token, d.live, d.owner AS current_owner "
                "FROM phone_rounds r JOIN phone_devices d ON d.id=r.device WHERE r.request=?",
                (request,),
            ).fetchone()
        if not row or not row["live"] or not row["token"] or row["push_status"] == "sent" or row["owner"] != row["current_owner"]:
            return
        status = "unconfigured"
        if self.push and self.push.configured:
            try:
                self.available(row["device"])
                await self.push.finish(dict(row))
                status = "sent"  # Push platform accepted; not proof of device delivery.
            except Exception:
                status = "failed"
        with self.db() as db:
            db.execute("UPDATE phone_rounds SET push_status=? WHERE request=?", (status, request))
