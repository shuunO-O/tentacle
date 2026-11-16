"""后台 coding 任务管理：逐任务串行、全局并发限制、持久化通知。"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from attachments.outgoing import check_file_access
from subagents.async_utils import settle
from subagents.config import SubagentConfig
from subagents.dsh import DshCleanupError, DshError, DshRunner, valid_session_id
from subagents.redaction import Redactor
from subagents.store import TaskStore

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "waiting_input", "failed", "cancelled", "interrupted"}


class SubagentManager:
    def __init__(self, config: SubagentConfig, *, runner=None):
        self.config = config
        self.store = TaskStore(config.db_path)
        self.runner = runner if runner is not None else DshRunner(config)
        self.redactor = Redactor()
        self._gate = asyncio.Lock()
        self._available = asyncio.Condition(self._gate)
        self._active_workspaces = set()
        self._workers = {}
        self._waiters = {}
        self._wake = asyncio.Event()
        self._notifier = None
        self._notify = None
        self._running = False
        self._execution_blocked = False
        self._observers = []

    def add_observer(self, observer):
        """注册同步状态通知回调 `(event: str, payload: dict) -> None`。"""
        if observer not in self._observers:
            self._observers.append(observer)

    def _emit(self, event: str, **payload):
        for observer in list(self._observers):
            try:
                observer(event, payload)
            except Exception:
                logger.debug("子代理观察者回调失败 event=%s", event, exc_info=True)

    async def _db(self, operation):
        # 事务完成前不释放管理锁，重复取消也不能留下迟到写回 running 的线程。
        return await settle(asyncio.to_thread(self.store.transaction, operation))

    async def start(self, notify=None):
        if self._running:
            return
        if not self.config.enabled and not self.config.db_path.exists():
            return
        await asyncio.to_thread(self.store.initialize)
        self._execution_blocked = bool(await self._db(lambda conn: conn.execute(
            "SELECT 1 FROM tasks WHERE workspace IS NOT NULL AND cleanup_error IS NOT NULL LIMIT 1").fetchone()))
        self._notify = notify
        self._running = True
        if notify is not None and self.config.enabled:
            self._notifier = asyncio.create_task(self._notifications(), name="subagent-notifications")
            self._wake.set()

    def _ensure_running(self):
        if not self._running:
            raise RuntimeError("子代理系统未启用或正在停止")

    def _ensure_execution(self):
        self._ensure_running()
        if not self.config.enabled:
            raise RuntimeError("请在主机启用子代理")
        if self._execution_blocked:
            raise RuntimeError("DSH 进程清理失败，已暂停新执行")

    def _text(self, text, name):
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{name} 必须为非空字符串")
        if len(text) > self.config.max_context_chars:
            raise ValueError(f"{name} 超过上下文长度上限")
        return self.redactor.text(text)

    def _check_depth(self, wake_depth):
        if not isinstance(wake_depth, int) or not 0 <= wake_depth < self.config.max_notification_depth:
            raise ValueError("子代理自动唤醒委派链已达上限；请汇总已有结果，等待新的输入")

    def resolve_workspace(self, cwd):
        if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
            raise ValueError("cwd 必须为非空路径")
        root = self.config.workspace_root
        try:
            workspace = (root / cwd).resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("cwd 必须指向 workspace 下的现有目录") from None
        if workspace == root or not workspace.is_relative_to(root) or not workspace.is_dir():
            raise ValueError("cwd 必须指向 workspace 下的现有目录")
        return workspace

    @property
    def coding_available(self):
        return self._running and self.config.enabled and isinstance(self.runner, DshRunner)

    def resolve_media_path(self, path):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("DSH 文件路径无效")
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("DSH 文件路径无效") from None
        if not resolved.is_relative_to(self.config.workspace_root) or not resolved.is_file():
            raise ValueError("DSH 文件不在工作区")
        check_file_access(Path(path))
        check_file_access(resolved)

    async def coding_sessions(self):
        self._ensure_execution()
        result = []
        for row in await self.runner.sessions():
            if row.get("blank"):
                continue
            try:
                workspace = self.resolve_workspace(row["cwd"])
            except ValueError:
                continue
            if str(workspace) != row["cwd"]:
                continue
            result.append({
                "session_id": row["sessionId"],
                "workspace": workspace.relative_to(self.config.workspace_root).as_posix(),
                "running": row["running"],
                "title": row.get("projections", {}).get("sessionTitle", {}).get("displayTitle") or row["sessionId"],
            })
        return self.redactor.value(result)

    async def attach(self, session_id, *, platform="web", parent_session=None):
        self._ensure_execution()
        if not valid_session_id(session_id):
            raise ValueError("DSH 会话 ID 无效")
        row = await self.runner.session(session_id)
        workspace = self.resolve_workspace(row["cwd"])
        if str(workspace) != row["cwd"]:
            raise ValueError("DSH 会话工作区已变更")
        objective = self.redactor.text(
            row.get("projections", {}).get("sessionTitle", {}).get("displayTitle") or f"DSH · {workspace.name}"
        )[:self.config.max_context_chars]
        async with self._gate:
            self._ensure_execution()

            def bind(conn):
                existing = conn.execute(
                    "SELECT id,status FROM tasks WHERE dsh_session_id=? AND archived=0", (session_id,),
                ).fetchone()
                if existing:
                    return existing["id"], existing["status"]
                task_id, now = uuid.uuid4().hex, time.time()
                conn.execute(
                    "INSERT INTO tasks (id,objective,background,platform,parent_session,status,created_at,"
                    "updated_at,workspace,dsh_session_id) VALUES (?,?,'',?,?,'attached',?,?,?,?)",
                    (task_id, objective, platform, parent_session, now, now, str(workspace), session_id),
                )
                return task_id, "attached"

            task_id, status = await self._db(bind)
        self._emit("task_attached", task_id=task_id, status=status)
        return {"task_id": task_id, "status": status, "dsh_session_id": session_id,
                "cwd": workspace.relative_to(self.config.workspace_root).as_posix(), "dsh_running": row["running"]}

    async def coding_history(self, task_id, limit):
        self._ensure_execution()
        task = await self.view(task_id, limit=1, compact=True)
        session_id = task.get("dsh_session_id")
        if not session_id:
            raise ValueError("任务尚未关联 DSH 会话")
        row = await self.runner.session(session_id)
        workspace = self.resolve_workspace(row["cwd"])
        if str(workspace) != row["cwd"]:
            raise ValueError("DSH 会话工作区已变更")
        messages = await self.runner.recent(session_id, min(limit, 20))
        return self.redactor.value({"dsh_running": row["running"],
                                    "cwd": workspace.relative_to(self.config.workspace_root).as_posix(),
                                    "dsh_history": messages})

    async def create(self, objective: str, *, cwd: str, background: str = "", wait: bool = False,
                     platform: str = "web", parent_session: str | None = None, wake_depth: int = 0):
        self._ensure_execution()
        self._check_depth(wake_depth)
        objective = self._text(objective, "objective")
        if not isinstance(background, str) or len(background) > self.config.max_context_chars:
            raise ValueError("background 必须为字符串且不超过上下文长度上限")
        if not isinstance(wait, bool):
            raise ValueError("wait 必须为布尔值")
        background = self.redactor.text(background)
        task_id = uuid.uuid4().hex
        workspace = self.resolve_workspace(cwd)
        now = time.time()
        async with self._gate:
            self._ensure_execution()

            def insert(conn):
                self._check_queue_limit(conn)
                conn.execute("INSERT INTO tasks (id,objective,background,platform,parent_session,status,created_at,updated_at,workspace) "
                             "VALUES (?,?,?,?,?,'queued',?,?,?)",
                             (task_id, objective, background, platform, parent_session, now, now, str(workspace)))
                cursor = conn.execute("INSERT INTO turns (task_id,message,status,delivery,wake_depth,created_at) "
                                      "VALUES (?,?,'queued',?,?,?)", (task_id, objective, "waiting" if wait else "background", wake_depth, now))
                return cursor.lastrowid

            turn_id = await self._db(insert)
            self._waiters[turn_id] = asyncio.get_running_loop().create_future()
            self._schedule(task_id, workspace)
        self._emit("task_created", task_id=task_id, turn_id=turn_id)
        return await self._response(task_id, turn_id, wait)

    def _check_queue_limit(self, conn):
        count = conn.execute("SELECT COUNT(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0]
        if count >= self.config.max_pending_turns:
            raise ValueError("子代理待执行轮次已达上限")

    def _schedule(self, task_id, workspace):
        if task_id not in self._workers:
            self._workers[task_id] = asyncio.create_task(self._drive(task_id, workspace), name=f"subagent-{task_id}")

    async def followup(self, task_id: str, message: str, *, wait: bool = False, wake_depth: int = 0):
        self._ensure_execution()
        self._check_depth(wake_depth)
        message = self._text(message, "message")
        if not isinstance(wait, bool):
            raise ValueError("wait 必须为布尔值")
        async with self._gate:
            self._ensure_execution()

            def insert(conn):
                task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if task is None:
                    raise ValueError("子任务不存在")
                if task["archived"]:
                    raise ValueError("已归档子任务不接受追问")
                if task["workspace"] is None:
                    raise ValueError("旧版只读子任务仅供查看，请创建新的 coding 任务")
                if task["cleanup_error"]:
                    raise ValueError("DSH 进程清理失败，需人工处理后才能继续该任务")
                self._check_queue_limit(conn)
                count = conn.execute("SELECT COUNT(*) FROM turns WHERE task_id=?", (task_id,)).fetchone()[0]
                if count >= self.config.max_turns_per_task:
                    raise ValueError("该子任务轮次数已达上限，请创建新任务并选择携带旧结果")
                now = time.time()
                cursor = conn.execute("INSERT INTO turns (task_id,message,status,delivery,wake_depth,created_at) "
                                      "VALUES (?,?,'queued',?,?,?)", (task_id, message, "waiting" if wait else "background", wake_depth, now))
                conn.execute("UPDATE tasks SET status=CASE WHEN status='running' THEN status ELSE 'queued' END, "
                             "updated_at=? WHERE id=?", (now, task_id))
                return cursor.lastrowid, Path(task["workspace"])

            turn_id, workspace = await self._db(insert)
            self._waiters[turn_id] = asyncio.get_running_loop().create_future()
            self._schedule(task_id, workspace)
        self._emit("task_followup", task_id=task_id, turn_id=turn_id)
        return await self._response(task_id, turn_id, wait)

    async def _response(self, task_id, turn_id, wait):
        if not wait:
            return {"task_id": task_id, "turn_id": turn_id, "status": "queued", "background": True}
        future = self._waiters[turn_id]
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=self.config.wait_timeout_seconds)
        except TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            # 取消主 agent 的等待不取消子任务。
            await asyncio.shield(self._release_waiter(turn_id))
            raise
        async with self._gate:
            def finish_wait(conn):
                row = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
                done = row["status"] in TERMINAL
                conn.execute("UPDATE turns SET delivery=? WHERE id=? AND delivery='waiting'",
                             ("delivered" if done else "background", turn_id))
                result = TaskStore.decode_turn(row)
                result.update(task_id=task_id, turn_id=turn_id, background=not done, wait_timed_out=timed_out and not done)
                return result
            result = await self._db(finish_wait)
            self._waiters.pop(turn_id, None)
        self._wake.set()
        return result

    async def _release_waiter(self, turn_id):
        async with self._gate:
            await self._db(lambda conn: conn.execute(
                "UPDATE turns SET delivery=CASE WHEN status IN ('queued','running') THEN 'background' ELSE 'pending' END "
                "WHERE id=? AND delivery='waiting'", (turn_id,)))
            self._waiters.pop(turn_id, None)
        self._wake.set()

    async def view(self, task_id=None, *, include_history=False, include_archived=False, limit=20,
                   compact=False, include_usage=False):
        self._ensure_running()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.config.max_query_rows:
            raise ValueError("limit 超出范围")
        if not all(isinstance(flag, bool) for flag in (include_history, include_archived, compact, include_usage)):
            raise ValueError("查询开关必须是布尔值")
        async with self._gate:
            def query(conn):
                if task_id:
                    fields = "id,objective,status,archived,cleanup_error,dsh_session_id" if compact else "*"
                    if compact and include_history:
                        fields += ",history"
                    row = conn.execute(f"SELECT {fields} FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if row is None:
                        raise ValueError("子任务不存在")
                    if compact:
                        task = {key: value for key, value in dict(row).items() if value not in (None, "")}
                        if task.get("archived"):
                            task["archived"] = True
                        else:
                            task.pop("archived", None)
                        if include_history:
                            task["history"] = json.loads(task["history"])
                        turn_fields = "id,status,result,error" + (",usage" if include_usage else "")
                        turns = [dict(row) for row in conn.execute(
                            f"SELECT {turn_fields} FROM turns WHERE task_id=? ORDER BY id DESC LIMIT ?",
                            (task_id, limit + 1))]
                        if len(turns) > limit:
                            task["has_more_turns"] = True
                        task["turns"] = []
                        for turn in reversed(turns[:limit]):
                            if not turn.get("error"):
                                turn.pop("error", None)
                            if include_usage:
                                turn["usage"] = json.loads(turn["usage"])
                            task["turns"].append(turn)
                    else:
                        task = TaskStore.decode_task(row)
                        for key in ("workspace", "workspace_ready", "acp_session_id", "progress"):
                            task.pop(key, None)
                        if not include_history:
                            task.pop("history")
                        task["turns"] = [TaskStore.decode_turn(row) for row in conn.execute(
                            "SELECT * FROM turns WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit))][::-1]
                    return task
                fields = "id,objective,status,archived"
                if not compact:
                    fields += ",created_at,updated_at"
                rows = conn.execute(f"SELECT {fields} FROM tasks "
                                    "WHERE (? OR archived=0) ORDER BY updated_at DESC LIMIT ?", (include_archived, limit))
                tasks = [dict(row) for row in rows]
                if compact:
                    tasks = [{key: value for key, value in task.items() if value not in (None, "", False)}
                             for task in tasks]
                return {"tasks": tasks, "execution_blocked": self._execution_blocked}
            return await self._db(query)

    async def presentation(self, task_id=None, *, before=None, limit=20):
        """Read-only UI projection; does not start work or claim result delivery."""
        if not self._running and not self.config.enabled:
            if task_id:
                raise LookupError("子代理未启用")
            return {"enabled": False, "tasks": []}
        self._ensure_running()
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("查询数量必须为 1..50")
        if task_id is not None and (not isinstance(task_id, str) or len(task_id) != 32
                                    or any(c not in "0123456789abcdef" for c in task_id)):
            raise ValueError("子任务标识无效")
        if before is not None and (type(before) is not int or before <= 0):
            raise ValueError("轮次游标无效")
        async with self._gate:
            def query(conn):
                if task_id:
                    row = conn.execute(
                        "SELECT id,objective,background,status,archived,cleanup_error,dsh_session_id,created_at,updated_at "
                        "FROM tasks WHERE id=?", (task_id,),
                    ).fetchone()
                    if row is None:
                        raise LookupError("子任务不存在")
                    turns = [TaskStore.decode_turn(row) for row in conn.execute(
                        "SELECT id,message,status,delivery,result,error,usage,created_at,finished_at "
                        "FROM turns WHERE task_id=? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
                        (task_id, before, before, limit + 1),
                    )]
                    more = len(turns) > limit
                    turns = turns[:limit]
                    return {"enabled": True, "task": dict(row), "turns": turns[::-1],
                            "has_more": more, "next_before": turns[-1]["id"] if more else None}
                rows = conn.execute(
                    "SELECT tasks.id,objective,tasks.status,archived,cleanup_error,"
                    "tasks.created_at,tasks.updated_at,turns.id AS turn_id,delivery,"
                    "substr(result,1,400) AS result,error,finished_at "
                    "FROM tasks LEFT JOIN turns ON turns.id=(SELECT id FROM turns WHERE task_id=tasks.id "
                    "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, id DESC LIMIT 1) "
                    "ORDER BY CASE tasks.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                    "tasks.updated_at DESC LIMIT ?", (limit + 1,),
                ).fetchall()
                return {"enabled": True, "tasks": [dict(row) for row in rows[:limit]],
                        "has_more": len(rows) > limit, "execution_blocked": self._execution_blocked,
                        "coding_enabled": self.coding_available}
            return self.redactor.value(await self._db(query))

    async def cancel(self, task_id: str, *, archive=False):
        self._ensure_running()
        async with self._gate:
            def cancel(conn):
                row = conn.execute("SELECT archived FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row is None:
                    raise ValueError("子任务不存在")
                if row["archived"]:
                    raise ValueError("子任务已归档")
                conn.execute("UPDATE tasks SET status=?,archived=?,updated_at=? WHERE id=?",
                             ("archived" if archive else "cancelled", int(archive), time.time(), task_id))
                conn.execute("UPDATE turns SET status='cancelled',finished_at=?,error='任务已取消' "
                             "WHERE task_id=? AND status IN ('queued','running')", (time.time(), task_id))
                conn.execute("UPDATE turns SET delivery='suppressed' WHERE task_id=? "
                             "AND delivery IN ('background','pending','waiting')", (task_id,))
                return [row[0] for row in conn.execute("SELECT id FROM turns WHERE task_id=?", (task_id,))]
            ids = await self._db(cancel)
            worker = self._workers.get(task_id)
            if worker is not None:
                worker.cancel()
            for turn_id in ids:
                future = self._waiters.pop(turn_id, None)
                if future is not None and not future.done():
                    future.set_result(None)
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        cleanup_error = await self._db(lambda conn: conn.execute("SELECT cleanup_error FROM tasks WHERE id=?", (task_id,)).fetchone()[0])
        result = {"task_id": task_id, "status": "archived" if archive else "cancelled"}
        if cleanup_error:
            result["cleanup_error"] = cleanup_error
        self._emit("task_cancelled", task_id=task_id, status=result["status"])
        return result

    async def _drive(self, task_id, workspace):
        try:
            while self._running:
                async with self._available:
                    await self._available.wait_for(lambda: self._execution_blocked or (
                        len(self._active_workspaces) < self.config.max_concurrency
                        and not any(workspace.is_relative_to(active) or active.is_relative_to(workspace)
                                    for active in self._active_workspaces)
                    ))

                    def claim(conn):
                        if self._execution_blocked:
                            return None
                        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                        if (task is None or task["archived"] or task["status"] == "cancelled"
                                or task["cleanup_error"] or task["workspace"] is None):
                            return None
                        row = conn.execute("SELECT * FROM turns WHERE task_id=? AND status='queued' ORDER BY id LIMIT 1",
                                           (task_id,)).fetchone()
                        if row is None:
                            return None
                        conn.execute("UPDATE turns SET status='running' WHERE id=?", (row["id"],))
                        conn.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?",
                                     (time.time(), task_id))
                        return dict(task), dict(row)

                    claimed = await self._db(claim)
                    if claimed is None:
                        self._workers.pop(task_id, None)
                        return
                    self._active_workspaces.add(workspace)
                self._emit("turn_running", task_id=task_id, turn_id=claimed[1]["id"])
                try:
                    await self._run_turn(*claimed)
                finally:
                    await settle(self._release_workspace(workspace))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("子代理任务管理异常 task_id=%s", task_id)
            # 管理器异常也不能留下永远 running 的任务。
            async with self._gate:
                def interrupt(conn):
                    conn.execute("UPDATE tasks SET status='interrupted',updated_at=? WHERE id=? AND status='running'",
                                 (time.time(), task_id))
                    ids = [row[0] for row in conn.execute("SELECT id FROM turns WHERE task_id=? AND status IN ('queued','running')",
                                                        (task_id,))]
                    conn.execute("UPDATE turns SET status='interrupted',error='任务管理异常',finished_at=?, "
                                 "delivery=CASE WHEN delivery='waiting' THEN 'waiting' ELSE 'pending' END "
                                 "WHERE task_id=? AND status IN ('queued','running')", (time.time(), task_id))
                    return ids
                for turn_id in await self._db(interrupt):
                    future = self._waiters.pop(turn_id, None)
                    if future is not None and not future.done():
                        future.set_result(None)
            self._wake.set()
        finally:
            async with self._gate:
                if self._workers.get(task_id) is asyncio.current_task():
                    self._workers.pop(task_id, None)
                    if self._running and not self._execution_blocked:
                        queued = await self._db(lambda conn: conn.execute(
                            "SELECT 1 FROM turns JOIN tasks ON tasks.id=turns.task_id "
                            "WHERE turns.task_id=? AND turns.status='queued' AND archived=0 "
                            "AND workspace IS NOT NULL AND cleanup_error IS NULL LIMIT 1",
                            (task_id,)).fetchone())
                        if queued:
                            self._schedule(task_id, workspace)

    async def _release_workspace(self, workspace):
        async with self._available:
            self._active_workspaces.remove(workspace)
            self._available.notify_all()

    async def _run_turn(self, task, turn):
        task_id, turn_id = task["id"], turn["id"]
        workspace = Path(task["workspace"])
        status, answer, error, usage = "failed", "", None, {}

        async def save_session(session_id):
            async with self._gate:
                await self._db(lambda conn: conn.execute(
                    "UPDATE tasks SET dsh_session_id=? WHERE id=?", (session_id, task_id)))

        try:
            async with asyncio.timeout(self.config.max_runtime_seconds):
                try:
                    resolved = self.resolve_workspace(task["workspace"])
                except ValueError:
                    resolved = None
                if resolved != workspace:
                    raise DshError("任务工作区已变更或缺失，请恢复原目录或创建新任务")

                message = turn["message"]
                if task["dsh_session_id"] is None:
                    if message != task["objective"]:
                        message = f"原始目标：{task['objective']}\n\n本轮要求：{message}"
                    if task["background"]:
                        message += f"\n\n相关背景：\n{task['background']}"
                reason, answer, usage = await self.runner.run(
                    workspace, task["dsh_session_id"], message, save_session=save_session)
                answer = self.redactor.text(answer)[:self.config.max_output_chars]
                if reason == "end_turn":
                    status = "completed"
                elif reason == "waiting_input":
                    status, error = "waiting_input", "DSH 正在编程页面等待处理"
                elif reason == "cancelled":
                    status, error = "cancelled", "DSH 已取消本轮"
                else:
                    error = {
                        "max_tokens": "DSH 达到输出上限，可追问继续",
                        "max_turn_requests": "DSH 达到本轮请求上限，可追问继续",
                        "refusal": "DSH 拒绝了本轮任务",
                    }.get(reason, "DSH 未能完成本轮")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            error = "达到本轮任务总时限；可显式追问继续"
        except DshCleanupError as exc:
            error = str(exc)
            await self._quarantine(task_id, error)
        except DshError as exc:
            error = str(exc)
        except Exception as exc:
            error = "DSH Web Host 请求失败，请检查执行器配置、模型认证和会话状态"
            logger.warning("DSH 执行失败 task_id=%s type=%s", task_id, type(exc).__name__)
        await self._finish(task_id, turn_id, status, answer, error, usage)

    async def _quarantine(self, task_id, error):
        async with self._gate:
            self._execution_blocked = True

            def block(conn):
                conn.execute("UPDATE tasks SET cleanup_error=? WHERE id=?", (error, task_id))
                ids = [row[0] for row in conn.execute("SELECT id FROM turns WHERE status='queued'")]
                conn.execute("UPDATE turns SET status='failed',error='DSH 进程未回收，暂停新执行',finished_at=?, "
                             "delivery=CASE WHEN delivery='waiting' THEN 'waiting' ELSE 'pending' END WHERE status='queued'",
                             (time.time(),))
                conn.execute("UPDATE tasks SET status='failed',updated_at=? WHERE status='queued'", (time.time(),))
                return ids

            for turn_id in await self._db(block):
                future = self._waiters.pop(turn_id, None)
                if future is not None and not future.done():
                    future.set_result(None)
        self._wake.set()
        self._emit("execution_blocked", task_id=task_id)

    async def _finish(self, task_id, turn_id, status, answer, error, usage):
        async with self._gate:
            def finish(conn):
                row = conn.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()
                if row is None or row["status"] != "running":
                    return False  # 取消或归档之后，迟到结果不能覆盖状态。
                conn.execute("UPDATE turns SET status=?, result=?,error=?,usage=?,finished_at=?, "
                             "delivery=CASE WHEN delivery='background' THEN 'pending' ELSE delivery END WHERE id=?",
                             (status, answer, self.redactor.text(error) if error else None,
                              json.dumps(usage), time.time(), turn_id))
                pending = conn.execute("SELECT 1 FROM turns WHERE task_id=? AND status='queued'", (task_id,)).fetchone()
                conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=? AND archived=0",
                             ("queued" if pending else status, time.time(), task_id))
                return True
            finished = await self._db(finish)
            future = self._waiters.get(turn_id)
            if future is not None and not future.done():
                future.set_result(None)
            if finished and future is not None:
                delivery = await self._db(lambda conn: conn.execute("SELECT delivery FROM turns WHERE id=?", (turn_id,)).fetchone()[0])
                if delivery != "waiting":
                    self._waiters.pop(turn_id, None)
        self._wake.set()
        if finished:
            self._emit("turn_finished", task_id=task_id, turn_id=turn_id, status=status)

    async def _notifications(self):
        while self._running:
            await self._wake.wait()
            self._wake.clear()
            while self._running:
                async with self._gate:
                    def next_event(conn):
                        row = conn.execute("SELECT turns.*,tasks.platform,tasks.objective FROM turns "
                                           "JOIN tasks ON tasks.id=turns.task_id WHERE delivery='pending' AND archived=0 "
                                           "AND tasks.status <> 'cancelled' ORDER BY turns.id LIMIT 1").fetchone()
                        return TaskStore.decode_turn(row) if row else None
                    event = await self._db(next_event)
                if event is None:
                    break

                async def claim():
                    async with self._gate:
                        def mark(conn):
                            cursor = conn.execute("UPDATE turns SET delivery='delivering' WHERE id=? AND delivery='pending' "
                                                  "AND task_id IN (SELECT id FROM tasks WHERE archived=0 AND status <> 'cancelled')",
                                                  (event["id"],))
                            return cursor.rowcount == 1
                        return await self._db(mark)
                try:
                    # claim 由 AgentSession 在叙事流锁内调用；排队期间可取消/归档。
                    delivered = await self._notify(event, claim)
                    async with self._gate:
                        await self._db(lambda conn: conn.execute(
                            "UPDATE turns SET delivery=? WHERE id=? AND delivery IN ('pending','delivering')",
                            ("delivered" if delivered else "suppressed", event["id"])))
                    self._emit("turn_delivery", task_id=event["task_id"], turn_id=event["id"],
                               delivery="delivered" if delivered else "suppressed")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("子代理结果唤醒失败 turn_id=%s", event["id"])
                    async with self._gate:
                        await self._db(lambda conn: conn.execute(
                            "UPDATE turns SET delivery='delivery_unknown' WHERE id=? AND delivery IN ('pending','delivering')",
                            (event["id"],)))

    async def stop(self):
        if not self._running:
            return
        self._running = False
        if self._notifier is not None:
            self._notifier.cancel()
            await asyncio.gather(self._notifier, return_exceptions=True)
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        async with self._gate:
            def interrupt(conn):
                conn.execute("UPDATE tasks SET status='interrupted',updated_at=? "
                             "WHERE status IN ('queued','running')", (time.time(),))
                conn.execute("UPDATE turns SET status='interrupted',error='运行时停止；未自动重放命令',finished_at=?, "
                             "delivery=CASE WHEN delivery='waiting' THEN 'waiting' ELSE 'pending' END "
                             "WHERE status IN ('queued','running')", (time.time(),))
            await self._db(interrupt)
            for future in self._waiters.values():
                if not future.done():
                    future.set_result(None)
            self._workers.clear()
            self._waiters.clear()
        if isinstance(self.runner, DshRunner):
            await self.runner.stop()
