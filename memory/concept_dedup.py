"""Web 手动触发的持久化去重队列。没有启动扫描、自动恢复或 agent 工具入口。"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from memory.concept_aligner import RULE_VERSION, AlignmentConfig, JevClient, JevError, _validate_answer
from memory.concept_audit import ConceptAuditLog
from memory.concept_maintenance import CandidateGone, ConceptMaintenance, ReviewRequired, encode
from config.manager import ConfigManager

logger = logging.getLogger(__name__)


class DedupJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, status TEXT, created REAL, updated REAL,
                    visited INTEGER DEFAULT 0, checked INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
                    message TEXT DEFAULT '', options TEXT
                );
                CREATE TABLE IF NOT EXISTS pairs (
                    id TEXT PRIMARY KEY, run_id TEXT, left_id TEXT, right_id TEXT, state TEXT,
                    data TEXT, operation TEXT, choice TEXT DEFAULT '', updated REAL,
                    UNIQUE(left_id, right_id)
                );
                CREATE TABLE IF NOT EXISTS independent (
                    left_id TEXT, right_id TEXT, PRIMARY KEY(left_id, right_id)
                );
            """)
            # 进程重启只标记中断，不重放模型调用或写操作。
            db.execute("UPDATE runs SET status='interrupted', message='服务重启，扫描已中断' WHERE status='running'")
            db.execute("UPDATE pairs SET state='pending' WHERE state='applying'")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def query(self, sql, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def write(self, sql, params=()):
        with self.connect() as db:
            db.execute(sql, params)

    def finalize(self, row_id, data, keep_id, drop_id, audit_id=None):
        with self.connect() as db:
            db.execute("UPDATE pairs SET state='merged',data=?,updated=? WHERE id=?", (encode(data), time.time(), row_id))
            if audit_id:
                ConceptAuditLog.finish_in_transaction(db, audit_id, "succeeded", {"result": data["result"]})

    def mark_independent(self, row_id, left_id, right_id, audit_id):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO independent VALUES(?,?)", (left_id, right_id))
            db.execute("UPDATE pairs SET state='independent',updated=? WHERE id=?", (time.time(), row_id))
            ConceptAuditLog.finish_in_transaction(db, audit_id, "succeeded", {"state": "independent"})


def pair_ids(a, b):
    return tuple(sorted((a, b)))


def canonical_id(a, b):
    def key(value):
        prefix, _, number = value.rpartition("_")
        return prefix, int(number) if number.isdigit() else float("inf"), value
    return min((a, b), key=key)


class ConceptDedupManager:
    def __init__(self, db_path="data/concept_dedup.db", graph=None, config=None, client=None):
        self.db_path = db_path
        self._journal = None
        self.graph = graph
        self.config = config
        self.client = client
        self._task = None
        self._cancel = asyncio.Event()
        self._lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self.audit = ConceptAuditLog(db_path)
        self._observers = []

    def add_observer(self, observer):
        """注册去重状态观察者 `(event: str, payload: dict) -> None`。"""
        if observer not in self._observers:
            self._observers.append(observer)

    def _emit(self, event: str, **payload):
        for observer in list(self._observers):
            try:
                observer(event, payload)
            except Exception:
                logger.debug("去重观察者回调失败 event=%s", event, exc_info=True)

    async def db(self, method, *args):
        def call():
            if self._journal is None:
                self._journal = DedupJournal(self.db_path)
            return getattr(self._journal, method)(*args)
        # 调用都在事件循环内串行提交；SQLite 本身每次独立事务。
        async with self._db_lock:
            return await asyncio.to_thread(call)

    def runtime(self):
        if self.graph is None:
            self.graph = ConceptMaintenance()
        if self.config is None:
            self.config = AlignmentConfig.from_app_config(ConfigManager().snapshot(), "concept_dedup")
        if self.client is None:
            self.client = JevClient(self.config, audit_log=self.audit, source="history")

    async def status(self, state="pending", offset=0):
        if state not in {"pending", "merged", "independent", "different", "all"}:
            raise ValueError("未知筛选状态")
        runs = await self.db("query", "SELECT * FROM runs ORDER BY created DESC LIMIT 1")
        counts = await self.db("query", "SELECT state, count(*) AS count FROM pairs GROUP BY state")
        where, params = ("", ()) if state == "all" else ("WHERE state=?", (state,))
        rows = await self.db("query", f"SELECT * FROM pairs {where} ORDER BY updated DESC LIMIT 30 OFFSET ?",
                             (*params, max(0, offset)))
        for row in rows:
            row["data"] = json.loads(row["data"])
            row["choice"] = json.loads(row["choice"]) if row["choice"] else None
        return {"run": runs[0] if runs else None, "counts": {r["state"]: r["count"] for r in counts},
                "pairs": rows, "offset": offset}

    async def detach(self, concept_id):
        """人工撤回单个概念的组归属；不初始化模型或改写历史配对结论。"""
        async with self._lock:
            if self.graph is None:
                self.graph = ConceptMaintenance()
            audit_id = await self.audit.begin(actor="manual", source="graph", action="detach",
                                             reference=concept_id, data={"concept_id": concept_id})
            try:
                result = await self.graph.detach(concept_id)
            except ValueError as exc:
                await self.audit.finish(audit_id, "failed", {"reason": str(exc)})
                raise
            except (Exception, asyncio.CancelledError) as exc:
                await self.audit.finish(audit_id, "unknown", {"error_type": type(exc).__name__})
                raise
            await self.audit.finish(audit_id, "succeeded", {"result": result})
            self._emit("detached", concept_id=concept_id, result=result)
            return result

    async def start(self, top_k=5, max_pairs=200):
        async with self._lock:
            if self._task and not self._task.done():
                raise ReviewRequired("已有扫描正在运行")
            self.runtime()
            if not os.getenv(self.config.api_key_env) and isinstance(self.client, JevClient):
                if not self.config.model:
                    raise ValueError("请先为历史概念去重角色绑定模型")
                raise ValueError(f"未配置 {self.config.api_key_env}")
            async with asyncio.timeout(self.config.total_timeout_seconds):
                await self.graph.validate()
            run_id = uuid.uuid4().hex
            now = time.time()
            audit_id = await self.audit.begin(actor="manual", source="history", action="scan_start", reference=run_id,
                                             data={"top_k": top_k, "max_pairs": max_pairs, "rule_version": RULE_VERSION})
            await self.db("write", "INSERT INTO runs(id,status,created,updated,options) VALUES(?,?,?,?,?)",
                          (run_id, "running", now, now, encode({"top_k": top_k, "max_pairs": max_pairs})))
            self._cancel.clear()
            self._task = asyncio.create_task(self._scan(run_id, top_k, max_pairs))
            await self.audit.finish(audit_id, "succeeded", {"run_id": run_id})
            self._emit("scan_started", run_id=run_id)
            return {"run_id": run_id}

    async def cancel(self):
        self._cancel.set()
        audit_id = await self.audit.begin(actor="manual", source="history", action="scan_stop", data={})
        await self.audit.finish(audit_id, "succeeded", {"stop_requested": True})
        self._emit("scan_cancel_requested")
        # 不取消正在提交的图事务；检查完当前候选后停止。
        return {"ok": True}

    async def stop(self):
        self._cancel.set()
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _progress(self, run_id, **changes):
        changes["updated"] = time.time()
        keys = list(changes)
        await self.db("write", "UPDATE runs SET " + ",".join(f"{k}=?" for k in keys) + " WHERE id=?",
                      (*[changes[k] for k in keys], run_id))
        status = changes.get("status")
        event_name = "scan_finished" if status in {"completed", "cancelled", "failed", "limited", "interrupted"} else "scan_progress"
        self._emit(event_name, run_id=run_id, run={"id": run_id, **changes})

    async def _scan(self, run_id, top_k, max_pairs):
        after, visited, checked, errors = "", 0, 0, 0
        deadline = time.monotonic() + 900
        try:
            while not self._cancel.is_set():
                if time.monotonic() >= deadline:
                    await self._progress(run_id, status="limited", message="达到 15 分钟扫描预算；可再次启动继续")
                    return
                async with asyncio.timeout(self.config.total_timeout_seconds):
                    nodes = await self.graph.page(after)
                if not nodes:
                    await self._progress(run_id, status="completed", message="扫描完成；召回范围之外的重复可能仍存在")
                    return
                for node in nodes:
                    if self._cancel.is_set():
                        break
                    if not node.get("id") or node["id"] <= after:
                        raise ReviewRequired("Concept ID 缺失或重复，扫描停止")
                    after = node["id"]
                    visited += 1
                    try:
                        async with asyncio.timeout(self.config.total_timeout_seconds):
                            candidates = await self.graph.candidates(node, top_k)
                    except Exception as exc:
                        errors += 1
                        await self._progress(run_id, errors=errors, message=f"部分节点召回失败：{type(exc).__name__}")
                        continue
                    for candidate in candidates:
                        if self._cancel.is_set():
                            break
                        if time.monotonic() >= deadline:
                            await self._progress(run_id, status="limited", message="达到 15 分钟扫描预算；可再次启动继续")
                            return
                        left, right = pair_ids(node["id"], candidate["id"])
                        previous = await self.db("query", "SELECT state FROM pairs WHERE left_id=? AND right_id=?", (left, right))
                        rejected = await self.db("query", "SELECT 1 FROM independent WHERE left_id=? AND right_id=?", (left, right))
                        if previous or rejected:
                            continue
                        if checked >= max_pairs:
                            await self._progress(run_id, status="limited", message="达到本轮候选上限；再次启动可继续检查未处理候选")
                            return
                        fatal = await self._inspect(run_id, left, right)
                        checked += 1
                        await self._progress(run_id, checked=checked, visited=visited, errors=errors)
                        if fatal:
                            await self._progress(run_id, status="failed", message="模型鉴权或余额异常，已停止扫描")
                            return
                await self._progress(run_id, visited=visited, errors=errors)
            await self._progress(run_id, status="cancelled", message="已停止；此前完成的分组和审核记录已保留")
        except asyncio.CancelledError:
            await self._progress(run_id, status="interrupted", message="扫描中断；不会自动重放")
            raise
        except ReviewRequired as exc:
            await self._progress(run_id, status="failed", message=str(exc))
        except Exception as exc:
            await self._progress(run_id, status="failed", message=f"扫描失败：{type(exc).__name__}")

    async def _inspect(self, run_id, left, right, *, row_id=None, locked=False, allow_auto=True):
        try:
            async with asyncio.timeout(self.config.total_timeout_seconds):
                preview = await self.graph.preview(left, right)
        except CandidateGone:
            if row_id:
                raise
            # 扫描页中的节点刚被本轮前面的分组处理，不再形成待审项。
            return
        data = {"preview": preview, "reason": "uncertain", "model": self.config.model, "rule_version": RULE_VERSION}
        state = "pending"
        try:
            async with asyncio.timeout(self.config.total_timeout_seconds):
                result = await self.client.evaluate(preview["left"], [preview["right"]])
            score = _validate_answer(result["answers"]["pair_0"])
            data.update(score=score, model=result["model"])
            if result.get("_audit_id"):
                data["evaluation_id"] = result["_audit_id"]
            if score["probabilities"]["0"] >= self.config.different_probability:
                state, data["reason"] = "different", "different"
            elif score["probabilities"]["2"] >= self.config.same_probability:
                data["reason"] = "same"
        except Exception as exc:
            data["reason"] = "jev_error"
            data["error_type"] = type(exc).__name__
            if isinstance(exc, JevError):
                data["error_code"] = exc.code
            if getattr(exc, "concept_audit_id", None):
                data["evaluation_id"] = exc.concept_audit_id
        existing_id = row_id
        row_id = row_id or uuid.uuid4().hex
        async with (nullcontext() if locked else self._lock):
            if await self.db("query", "SELECT 1 FROM independent WHERE left_id=? AND right_id=?", (left, right)):
                return
            audit_id = await self.audit.begin(actor="automatic", source="history", action="decision", reference=row_id,
                                             parent_id=data.get("evaluation_id"), data=data)
            if existing_id:
                await self.db("write", "UPDATE pairs SET state=?,data=?,updated=? WHERE id=?",
                              (state, encode(data), time.time(), row_id))
            else:
                await self.db("write", "INSERT OR IGNORE INTO pairs(id,run_id,left_id,right_id,state,data,updated) VALUES(?,?,?,?,?,?,?)",
                              (row_id, run_id, left, right, state, encode(data), time.time()))
            await self.audit.finish(audit_id, "failed" if data["reason"] == "jev_error" else "succeeded", {
                "state": state, "auto_merge_blocked_by_conflicts": bool(preview["conflicts"]),
            })
            if allow_auto and state == "pending" and data["reason"] == "same" and not preview["conflicts"] and not self._cancel.is_set():
                keep = canonical_id(left, right)
                await self._apply(row_id, keep, "reject", automatic=True)
        return data.get("error_code") in {"missing_api_key", "jev_http_401", "jev_http_402", "jev_http_403"}

    async def refresh(self, row_id):
        async with self._lock:
            self.runtime()
            row = await self._row(row_id)
            if row["state"] not in {"pending", "different"} or row["operation"]:
                raise ReviewRequired("该记录不能刷新，请先确认已有操作结果")
            audit_id = await self.audit.begin(actor="manual", source="history", action="refresh", reference=row_id,
                                             data={"previous": json.loads(row["data"])})
            try:
                async with asyncio.timeout(self.config.total_timeout_seconds):
                    preview = await self.graph.preview(row["left_id"], row["right_id"])
            except BaseException as exc:
                await self.audit.finish(audit_id, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                                        {"error_type": type(exc).__name__})
                raise
            data = json.loads(row["data"])
            if "score" in data:
                data["previous_score"] = data.pop("score")
            data.pop("error_code", None)
            data.update(preview=preview, reason="refreshed_review")
            await self.db("write", "UPDATE pairs SET state='pending', data=?, updated=? WHERE id=?",
                          (encode(data), time.time(), row_id))
            await self.audit.finish(audit_id, "succeeded", {"preview": preview})
            self._emit("pair_updated", row_id=row_id, state="pending")
            return preview

    async def reevaluate(self, row_id):
        """显式模型重判，仅更新待审核结果；旧判断及人工触发分别留痕。"""
        async with self._lock:
            self.runtime()
            row = await self._row(row_id)
            if row["state"] not in {"pending", "different"} or row["operation"]:
                raise ReviewRequired("该记录不能重新评估，请先确认已有操作结果")
            audit_id = await self.audit.begin(actor="manual", source="history", action="reevaluate", reference=row_id,
                                             data={"previous": json.loads(row["data"]), "rule_version": RULE_VERSION})
            try:
                await self._inspect(row["run_id"], row["left_id"], row["right_id"],
                                    row_id=row_id, locked=True, allow_auto=False)
                updated = await self._row(row_id)
                result = json.loads(updated["data"])
            except BaseException as exc:
                await self.audit.finish(audit_id, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                                        {"error_type": type(exc).__name__})
                raise
            await self.audit.finish(audit_id, "failed" if result["reason"] == "jev_error" else "succeeded",
                                    {"result": result, "state": updated["state"]})
            self._emit("pair_updated", row_id=row_id, state=updated["state"])
            return {"state": updated["state"]}

    async def _row(self, row_id):
        rows = await self.db("query", "SELECT * FROM pairs WHERE id=?", (row_id,))
        if not rows:
            raise ValueError("候选不存在")
        return rows[0]

    async def decide(self, row_id, action, keep_id=None, policy="reject"):
        async with self._lock:
            self.runtime()
            row = await self._row(row_id)
            if row["state"] not in {"pending", "different"}:
                raise ReviewRequired("该候选已处理，请刷新列表")
            if action == "independent":
                if row["operation"]:
                    raise ReviewRequired("存在可能已提交的分组操作，须先重试确认结果")
                audit_id = await self.audit.begin(actor="manual", source="history", action="independent", reference=row_id,
                                                 data={"previous": json.loads(row["data"])})
                try:
                    await self.db("mark_independent", row_id, row["left_id"], row["right_id"], audit_id)
                except Exception as exc:
                    await self.audit.finish(audit_id, "unknown", {"error_type": type(exc).__name__})
                    raise
                self._emit("pair_updated", row_id=row_id, state="independent")
                return {"state": "independent"}
            if action != "merge" or keep_id not in {row["left_id"], row["right_id"]}:
                raise ValueError("请选择分组参考概念")
            return await self._apply(row_id, keep_id, policy)

    async def _apply(self, row_id, keep_id, policy, automatic=False):
        if policy != "reject":
            raise ValueError("等价分组不覆盖关系属性")
        row = await self._row(row_id)
        data = json.loads(row["data"])
        drop_id = row["right_id"] if keep_id == row["left_id"] else row["left_id"]
        preview = data["preview"]
        left_members = {n["id"] for n in preview["left"].get("members", [preview["left"]])}
        right_members = {n["id"] for n in preview["right"].get("members", [preview["right"]])}
        independent = await self.db("query", "SELECT left_id,right_id FROM independent")
        if not row["operation"] and any(
            a["left_id"] in left_members and a["right_id"] in right_members
            or a["right_id"] in left_members and a["left_id"] in right_members for a in independent
        ):
            if automatic:
                return {"state": "pending"}
            raise ReviewRequired("两个组中存在已明确保持独立的概念，不能加入同一组")
        choice = encode({"keep_id": keep_id, "policy": policy})
        if row["operation"] and row["choice"] != choice:
            raise ReviewRequired("先用上次选择重试，确认是否已提交")
        operation = row["operation"] or uuid.uuid4().hex
        audit_id = await self.audit.begin(actor="automatic" if automatic else "manual", source="history", action="merge",
                                         reference=row_id, parent_id=data.get("evaluation_id"), data={
                                             "previous": data, "keep_id": keep_id, "drop_id": drop_id,
                                             "policy": policy, "operation_id": operation,
                                         })
        await self.db("write", "UPDATE pairs SET state='applying',operation=?,choice=? WHERE id=?", (operation, choice, row_id))
        try:
            result = await self.graph.merge(keep_id, drop_id, data["preview"]["fingerprint"], operation, policy)
        except ReviewRequired as exc:
            await self.audit.finish(audit_id, "failed", {"reason": str(exc)})
            data["reason"] = str(exc)
            # 确定事务拒绝的错误可重新审查；其它错误保留 operation 以幂等确认。
            await self.db("write", "UPDATE pairs SET state='pending',operation=NULL,choice='',data=? WHERE id=?", (encode(data), row_id))
            if not automatic:
                raise
            return {"state": "pending"}
        except Exception as exc:
            await self.audit.finish(audit_id, "unknown", {"error_type": type(exc).__name__})
            data.update(reason="merge_error", error_type=type(exc).__name__)
            await self.db("write", "UPDATE pairs SET state='pending',data=? WHERE id=?", (encode(data), row_id))
            if not automatic:
                raise ReviewRequired("分组结果待确认，请按相同选择重试") from exc
            return {"state": "pending"}
        data.update(result=result, automatic=automatic)
        # 图事务已提交但 SQLite 最终提交失败时保留 applying/operation，重启后按原选择确认。
        await self.db("finalize", row_id, data, keep_id, drop_id, audit_id)
        self._emit("pair_updated", row_id=row_id, state="merged")
        return {"state": "merged", "result": result}
