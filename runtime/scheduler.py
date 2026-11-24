"""持久化任务计划及执行实例。

tool/command 在后台执行，外部消息发送复用 tool:send_message；heartbeat 经共享心跳
服务唤醒主叙事流，只允许 once/cron 且必须是唯一动作，不重置固定心跳计时。
三种触发来源共用事务内准入；running/unknown 不自动重放，结果未知须显式确认重试。
默认列表只查 active，按 ID 可读取归档任务及运行记录。旧 QQ notify 迁移为工具动作，
旧 Web notify 任务归档。生产运行前应备份 scheduler.db。
"""

import asyncio
import json
import logging
import math
import os
import re
import signal
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from runtime.timezone import local_datetime, localize
from runtime.tool_registry import registry

logger = logging.getLogger(__name__)

DEFAULT_MAX_RUNTIME = 600.0  # 单次运行总超时（秒）
DEFAULT_CATCH_UP_GRACE_HOURS = 24.0
DEFAULT_INTERVAL_CATCH_UP_GRACE_HOURS = 6.0
MAX_CATCH_UP_RUNS = 20  # interval 停机补跑单任务上限（防极短间隔 × 长停机狂跑）
RUN_HISTORY_KEEP = 50  # 每任务保留最近运行记录条数
RESULT_SUMMARY_MAX = 2000  # 运行结果摘要截断字符数
VALID_TRIGGER_TYPES = {"cron", "interval", "once"}
MAX_ACTIVE_SCHEDULES = 50  # 活跃任务数上限（防无节制堆积，超限拒绝 create）
MAX_CONCURRENT_JOBS = 2
FAILURE_NOTIFY_TIMEOUT = 10.0
# 定时任务的 tool 动作禁用名单：任务建任务会指数自我复制，且无人值守
_FORBIDDEN_TOOL_ACTIONS = {
    "schedule_update", "subagent_create", "subagent_followup",
    "terminal_run", "terminal_interact",
}

_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)([smh]?)$")
_PLATFORM_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_RESERVED_NOTIFY_PLATFORMS = {"web", "system"}


def _valid_notify_platform(platform: object) -> str | None:
    if not isinstance(platform, str):
        return None
    cleaned = platform.strip().lower()
    if cleaned in _RESERVED_NOTIFY_PLATFORMS or not _PLATFORM_NAME_RE.fullmatch(cleaned):
        return None
    return cleaned


def _validate_on_failure(on_failure: object) -> tuple[dict | None, str | None]:
    """校验并规范化 on_failure 配置；返回 `(normalized, error)`。"""
    if on_failure is None:
        return None, None
    if not isinstance(on_failure, dict):
        return None, "on_failure 必须是对象"
    if not on_failure:
        return {}, None
    extra = set(on_failure) - {"notify_platform"}
    platform = _valid_notify_platform(on_failure.get("notify_platform"))
    if extra or platform is None:
        return None, "失败通知仅支持外部通道 {notify_platform: <platform>}，经 send_message 发送"
    return {"notify_platform": platform}, None
_INTERVAL_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "": 60.0}

_SCHEDULE_COLUMNS = (
    "id, name, status, trigger_type, trigger_expr, actions, max_runtime, "
    "on_failure, next_run_at, last_run_at, last_status, last_error, "
    "created_at, updated_at"
)


def _fmt_time(t: float | None) -> str:
    """时间戳转 [MM.DD-HH:MM] 展示格式，空值返回空串"""
    if not t:
        return ""
    return local_datetime(t).strftime("%m.%d %H:%M")


def _parse_interval_seconds(trigger_expr: str) -> float:
    """解析 interval 表达式为秒数；非法抛 ValueError。"""
    m = _INTERVAL_RE.match(str(trigger_expr).strip())
    if not m:
        raise ValueError(
            f"非法 interval 表达式: {trigger_expr}（支持 90s / 30m / 2h / 裸数字=分钟）"
        )
    seconds = float(m.group(1)) * _INTERVAL_UNITS[m.group(2)]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("interval 必须为正数")
    return seconds


def compute_next_run(trigger_type: str, trigger_expr: str, after: float) -> float | None:
    """
    计算 after 之后的一次触发时间戳。

    once 已过期返回 None；表达式非法抛 ValueError。
    """
    if trigger_type == "cron":
        from croniter import croniter

        if len(trigger_expr.split()) != 5 or not croniter.is_valid(trigger_expr):
            raise ValueError(f"非法 cron 表达式: {trigger_expr}（标准 5 字段，如 0 9 * * 1）")
        itr = croniter(trigger_expr, local_datetime(after))
        return itr.get_next(datetime).timestamp()
    if trigger_type == "interval":
        seconds = _parse_interval_seconds(trigger_expr)
        return after + seconds
    if trigger_type == "once":
        try:
            dt = datetime.fromisoformat(str(trigger_expr).strip())
        except ValueError:
            raise ValueError(
                f"非法 once 时间: {trigger_expr}（ISO 本地时间，如 2026-08-31 09:00）"
            )
        ts = localize(dt).timestamp()
        return ts if ts > after else None
    raise ValueError(f"未知触发类型: {trigger_type}（支持 cron / interval / once）")


def validate_actions(actions, trigger_type=None) -> str | None:
    """校验 actions 结构，返回错误信息或 None。"""
    if not isinstance(actions, list) or not actions:
        return "actions 必须是非空数组"
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            return f"actions[{i}] 必须是对象"
        a_type = action.get("type", "")
        if a_type == "tool":
            name = action.get("name", "")
            if not isinstance(name, str) or not name.strip():
                return f"actions[{i}] tool 动作必须提供 name"
            if not registry.has(str(name)):
                return f"actions[{i}] 引用了未注册的工具: {name}"
            if str(name) in _FORBIDDEN_TOOL_ACTIONS:
                if name in {"terminal_run", "terminal_interact"}:
                    return f"actions[{i}] 不允许直接调用 {name}；命令用 command，需要主 Agent 处理用 heartbeat"
                if str(name).startswith("subagent_"):
                    return f"actions[{i}] 不允许调用 {name}：子任务委派/追问须由主 agent 在叙事流中发起"
                return (
                    f"actions[{i}] 不允许调用 {name}：定时任务不能创建/触发定时任务"
                    "（防自我复制），请在对话中直接操作"
                )
            args_val = action.get("args")
            if args_val is not None and not isinstance(args_val, dict):
                return f"actions[{i}] tool 动作的 args 必须是对象"
        elif a_type == "command":
            if not isinstance(action.get("command"), str) or not action["command"].strip():
                return f"actions[{i}] command 动作必须提供 command"
            timeout = action.get("timeout")
            if timeout is not None and (
                not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0
            ):
                return f"actions[{i}] command 的 timeout 必须是正数"
        elif a_type == "heartbeat":
            if len(actions) != 1:
                return "heartbeat 必须是任务的唯一动作"
            if trigger_type == "interval":
                return "heartbeat 只允许 once / cron，不能使用 interval"
            if not isinstance(action.get("content"), str) or not action["content"].strip():
                return "heartbeat 必须提供非空 content"
        else:
            return f"actions[{i}] 未知动作类型: {a_type}（支持 tool / command / heartbeat；发送消息用 tool:send_message）"
    return None


class Scheduler:
    """
    定时任务引擎。

    所有 DB 操作通过 asyncio.to_thread 调用同步 sqlite3（照 GoalManager 模式）；
    任务执行为独立 asyncio task，per-job 防重入（上一轮未结束跳过本轮）。
    """

    def __init__(
        self,
        db_path: str = "data/scheduler.db",
        catch_up_grace_hours: float = DEFAULT_CATCH_UP_GRACE_HOURS,
        interval_catch_up_grace_hours: float = DEFAULT_INTERVAL_CATCH_UP_GRACE_HOURS,
    ):
        self.db_path = Path(db_path)
        self.catch_up_grace_seconds = float(catch_up_grace_hours) * 3600
        self.interval_catch_up_grace_seconds = float(interval_catch_up_grace_hours) * 3600
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._job_tasks: set[asyncio.Task] = set()
        self._active_jobs: set[int] = set()  # 正在执行的任务 id（防重入）
        self._heartbeat = None
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        self._catch_up_until: dict[int, float] = {}
        # 轮询间隔暴露为实例属性，测试可调小
        self._poll_interval = 30.0
        self._init_db()
        self._recover_runs_sync()

    # ------------------------------------------------------------------
    # 数据库基础
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    trigger_type TEXT NOT NULL,
                    trigger_expr TEXT NOT NULL,
                    actions TEXT NOT NULL,
                    max_runtime REAL,
                    on_failure TEXT,
                    next_run_at REAL,
                    last_run_at REAL,
                    last_status TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules (status, next_run_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT,
                    result_summary TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedule_runs ON schedule_runs (schedule_id, id)"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(schedule_runs)")}
            if "scheduled_for" not in columns:
                conn.execute("ALTER TABLE schedule_runs ADD COLUMN scheduled_for REAL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_runs_live ON schedule_runs (schedule_id, status)")
            # 一次性迁移旧 notify；不注册任何旧发送路径，也不读取/展示任务正文。
            for sid, raw, raw_failure in conn.execute(
                    "SELECT id, actions, on_failure FROM schedules WHERE status='active'").fetchall():
                try:
                    actions = json.loads(raw)
                    if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
                        raise ValueError('invalid actions')
                    failure = json.loads(raw_failure) if raw_failure else None
                    if failure is not None and not isinstance(failure, dict):
                        raise ValueError('invalid failure action')
                except (ValueError, TypeError):
                    conn.execute("UPDATE schedules SET status='inactive',next_run_at=NULL,last_error=? WHERE id=?",
                                 ('旧任务结构无效，已归档，未执行', sid))
                    continue
                migrated = []
                unsupported = False
                for action in actions:
                    if action.get("type") != "notify":
                        migrated.append(action)
                    elif action.get("platform") == "qq":
                        migrated.append({"type": "tool", "name": "send_message", "args": {
                            "platform": "qq", "text": action.get("text", ""),
                        }})
                    else:
                        unsupported = True
                if failure and _valid_notify_platform(failure.get("notify_platform")) is None:
                    failure = None
                if unsupported:
                    conn.execute("UPDATE schedules SET status='inactive', next_run_at=NULL, "
                                 "last_error='旧 notify 平台已移除，任务已归档' WHERE id=?", (sid,))
                elif migrated != actions:
                    conn.execute("UPDATE schedules SET actions=? WHERE id=?",
                                 (json.dumps(migrated, ensure_ascii=False), sid))
                if failure != (json.loads(raw_failure) if raw_failure else None):
                    conn.execute("UPDATE schedules SET on_failure=? WHERE id=?",
                                 (json.dumps(failure) if failure else None, sid))

    def _row_to_schedule(self, row: tuple) -> dict:
        """行元组转任务字典，actions / on_failure 反序列化为对象。"""
        keys = _SCHEDULE_COLUMNS.split(", ")
        data = dict(zip(keys, row))
        try:
            data["actions"] = json.loads(data["actions"])
        except (json.JSONDecodeError, TypeError):
            data["actions"] = []
        try:
            data["on_failure"] = json.loads(data["on_failure"]) if data["on_failure"] else None
        except (ValueError, TypeError):
            data["on_failure"] = None
        return data

    # ------------------------------------------------------------------
    # 写操作（批量、整体回滚，照 GoalManager.update 模式）
    # ------------------------------------------------------------------

    def _ensure_schedule_active(self, conn: sqlite3.Connection, schedule_id: int) -> dict | None:
        """确认任务存在且 active，返回任务数据，否则返回 None。"""
        cursor = conn.execute(
            f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE id = ? AND status = 'active'",
            (schedule_id,),
        )
        row = cursor.fetchone()
        return self._row_to_schedule(row) if row else None

    def _create_schedule_sync(self, conn: sqlite3.Connection, op: dict) -> dict:
        """同步创建任务。"""
        name = str(op.get("name", "")).strip()
        trigger = op.get("trigger")
        actions = op.get("actions")

        if not name:
            return {"status": "error", "error": "create_schedule 必须提供 name"}
        if not isinstance(trigger, dict):
            return {"status": "error", "error": "create_schedule 必须提供 trigger（{type, expr}）"}
        trigger_type = str(trigger.get("type", "")).strip()
        trigger_expr = str(trigger.get("expr", "")).strip()
        if trigger_type not in VALID_TRIGGER_TYPES:
            return {
                "status": "error",
                "error": f"trigger.type 必须是 {VALID_TRIGGER_TYPES} 之一",
            }

        now = time.time()
        try:
            next_run = compute_next_run(trigger_type, trigger_expr, now)
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        if next_run is None:
            return {"status": "error", "error": "once 的触发时间已过期"}

        err = validate_actions(actions, trigger_type)
        if err:
            return {"status": "error", "error": err}

        # 数量上限：同一事务内计数，批量 create 的前序插入也计入
        active_count = conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE status = 'active'"
        ).fetchone()[0]
        if active_count >= MAX_ACTIVE_SCHEDULES:
            return {
                "status": "error",
                "error": f"活跃任务已达上限 {MAX_ACTIVE_SCHEDULES}，请先归档不需要的任务",
            }

        max_runtime = op.get("max_runtime")
        if max_runtime is not None:
            if (not isinstance(max_runtime, (int, float)) or isinstance(max_runtime, bool)
                    or not math.isfinite(max_runtime) or max_runtime <= 0):
                return {"status": "error", "error": "max_runtime 必须是正数（秒）"}
        on_failure, err = _validate_on_failure(op.get("on_failure"))
        if err:
            return {"status": "error", "error": err}
        if actions[0]["type"] == "heartbeat" and max_runtime is not None:
            return {"status": "error", "error": "heartbeat 使用现有心跳预算，不支持 max_runtime"}

        cursor = conn.execute(
            """
            INSERT INTO schedules
                (name, status, trigger_type, trigger_expr, actions, max_runtime,
                 on_failure, next_run_at, created_at, updated_at)
            VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                trigger_type,
                trigger_expr,
                json.dumps(actions, ensure_ascii=False),
                max_runtime,
                json.dumps(on_failure, ensure_ascii=False) if on_failure is not None else None,
                next_run,
                now,
                now,
            ),
        )
        return {"status": "ok", "schedule_id": cursor.lastrowid, "next_run_at": next_run}

    def _update_schedule_sync(self, conn: sqlite3.Connection, op: dict) -> dict:
        """同步修改任务，只允许 active 的任务。"""
        schedule_id = op.get("schedule_id")
        if schedule_id is None:
            return {"status": "error", "error": "update_schedule 必须提供 schedule_id"}
        if type(schedule_id) is not int or schedule_id <= 0:
            return {"status": "error", "error": "schedule_id 必须是正整数"}

        schedule = self._ensure_schedule_active(conn, schedule_id)
        if schedule is None:
            return {
                "status": "error",
                "error": f"schedule_id={schedule_id} 不存在或不是 active 状态，无法修改",
            }
        candidate_trigger = op.get('trigger')
        if candidate_trigger is not None and not isinstance(candidate_trigger, dict):
            return {"status": "error", "error": "trigger 必须是对象"}
        combined_type = str((candidate_trigger or {}).get('type', schedule['trigger_type'])).strip()
        err = validate_actions(op.get("actions", schedule["actions"]), combined_type)
        if err:
            return {"status": "error", "error": err}
        if op.get("actions", schedule["actions"])[0]["type"] == "heartbeat" and op.get("max_runtime") is not None:
            return {"status": "error", "error": "heartbeat 使用现有心跳预算，不支持 max_runtime"}

        updates = []
        if op.get('actions', schedule['actions'])[0]['type'] == 'heartbeat' and schedule['max_runtime'] is not None:
            updates.append('max_runtime = NULL')
        params = []
        now = time.time()

        name = op.get("name")
        if name is not None:
            name = str(name).strip()
            if not name:
                return {"status": "error", "error": "任务的 name 不能为空"}
            updates.append("name = ?")
            params.append(name)

        trigger = op.get("trigger")
        if trigger is not None:
            if not isinstance(trigger, dict):
                return {"status": "error", "error": "trigger 必须是 {type, expr} 对象"}
            trigger_type = str(trigger.get("type", "")).strip()
            trigger_expr = str(trigger.get("expr", "")).strip()
            if trigger_type not in VALID_TRIGGER_TYPES:
                return {
                    "status": "error",
                    "error": f"trigger.type 必须是 {VALID_TRIGGER_TYPES} 之一",
                }
            try:
                next_run = compute_next_run(trigger_type, trigger_expr, now)
            except ValueError as e:
                return {"status": "error", "error": str(e)}
            if next_run is None:
                return {"status": "error", "error": "once 的触发时间已过期"}
            updates.append("trigger_type = ?")
            params.append(trigger_type)
            updates.append("trigger_expr = ?")
            params.append(trigger_expr)
            updates.append("next_run_at = ?")
            params.append(next_run)

        actions = op.get("actions")
        if actions is not None:
            err = validate_actions(actions, trigger_type if trigger is not None else schedule["trigger_type"])
            if err:
                return {"status": "error", "error": err}
            updates.append("actions = ?")
            params.append(json.dumps(actions, ensure_ascii=False))

        max_runtime = op.get("max_runtime")
        if max_runtime is not None:
            if (not isinstance(max_runtime, (int, float)) or isinstance(max_runtime, bool)
                    or not math.isfinite(max_runtime) or max_runtime <= 0):
                return {"status": "error", "error": "max_runtime 必须是正数（秒）"}
            updates.append("max_runtime = ?")
            params.append(max_runtime)

        raw_on_failure = op.get("on_failure")
        if raw_on_failure is not None:
            on_failure, err = _validate_on_failure(raw_on_failure)
            if err:
                return {"status": "error", "error": err}
            updates.append("on_failure = ?")
            params.append(json.dumps(on_failure, ensure_ascii=False))

        status = op.get("status")
        if status is not None:
            if status != "inactive":
                return {
                    "status": "error",
                    "error": "任务的 status 只支持 inactive（归档，不可逆）",
                }
            updates.append("status = ?")
            params.append(status)

        if not updates:
            return {"status": "error", "error": "update_schedule 没有提供任何可修改字段"}

        updates.append("updated_at = ?")
        params.append(now)
        params.append(schedule_id)
        set_clause = ", ".join(updates)
        conn.execute(f"UPDATE schedules SET {set_clause} WHERE id = ?", params)
        return {"status": "ok", "schedule_id": schedule_id}

    def _claim_run(self, conn, schedule_id, trigger, scheduled_for=None, acknowledge_unknown=False):
        """在写事务内登记执行；所有触发来源共用，尚不执行动作。"""
        schedule = self._ensure_schedule_active(conn, schedule_id)
        if schedule is None:
            return {"status": "error", "error": f"schedule_id={schedule_id} 不存在或不是 active 状态"}, None
        live = conn.execute("SELECT id FROM schedule_runs WHERE schedule_id=? AND status='running'",
                            (schedule_id,)).fetchone()
        if live or schedule_id in self._active_jobs:
            return {"status": "error", "error": "任务正在运行，未接收新的执行", "run_status": "busy"}, None
        if schedule['last_status'] == 'unknown':
            if trigger != 'manual' or not acknowledge_unknown:
                return {"status": "error", "error": "上次执行结果未知；确认可能重复后，用 run_schedule 的 acknowledge_unknown=true 明确重试"}, None
            previous = conn.execute("SELECT scheduled_for FROM schedule_runs WHERE schedule_id=? "
                                    "AND status='unknown' ORDER BY id DESC LIMIT 1", (schedule_id,)).fetchone()
            scheduled_for = previous[0] if previous else None
        elif trigger != 'manual':
            if scheduled_for is not None and schedule['next_run_at'] != scheduled_for:
                return {"status": "error", "error": "触发时间已更新，跳过旧触发", "run_status": "skipped"}, None
            scheduled_for = schedule['next_run_at']
            if scheduled_for is None:
                return {"status": "error", "error": "没有待执行的触发点", "run_status": "skipped"}, None
        err = validate_actions(schedule['actions'], schedule['trigger_type'])
        if err:
            return {"status": "error", "error": err}, None
        cursor = conn.execute(
            "INSERT INTO schedule_runs(schedule_id,trigger,started_at,status,result_summary,scheduled_for) "
            "VALUES(?,?,?,'running','等待执行',?)", (schedule_id, trigger, time.time(), scheduled_for))
        run_id = cursor.lastrowid
        claim = {'run_id': run_id, 'schedule': schedule, 'trigger': trigger, 'scheduled_for': scheduled_for}
        return {"status": "ok", "schedule_id": schedule_id, "run_id": run_id,
                "run_status": "accepted", "message": "已接收执行，结果用 schedule_list 查看"}, claim

    def _claim_run_sync(self, schedule, trigger):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return self._claim_run(conn, schedule['id'], trigger,
                                   schedule['next_run_at'] if trigger != 'manual' else None)

    def _prepare_manual_run_sync(self, conn, op):
        schedule_id = op.get('schedule_id')
        if type(schedule_id) is not int or schedule_id <= 0:
            return {"status": "error", "error": "schedule_id 必须是正整数"}, None
        acknowledge = op.get('acknowledge_unknown', False)
        if not isinstance(acknowledge, bool):
            return {"status": "error", "error": "acknowledge_unknown 必须是布尔值"}, None
        return self._claim_run(conn, schedule_id, 'manual', acknowledge_unknown=acknowledge)

    def _update_operations_sync(
        self, operations: list[dict]
    ) -> tuple[dict, list[dict]]:
        """同步执行一批写操作，返回 (结果, 待手动触发的任务列表)。"""
        results = []
        manual_schedules: list[dict] = []
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not isinstance(operations, list) or not operations:
                    return {"status": "error", "error": "operations 必须是非空数组"}, []
                for i, op in enumerate(operations):
                    if not isinstance(op, dict):
                        raise ValueError("operations 的每一项必须是对象")
                    op_type = op.get("op", "")
                    if op_type == "create_schedule":
                        result = self._create_schedule_sync(conn, op)
                    elif op_type == "update_schedule":
                        result = self._update_schedule_sync(conn, op)
                    elif op_type == "run_schedule":
                        result, schedule = self._prepare_manual_run_sync(conn, op)
                        if result["status"] == "ok" and schedule is not None:
                            manual_schedules.append(schedule)
                    else:
                        result = {"status": "error", "error": f"未知操作类型: {op_type}"}
                    result["index"] = i
                    results.append(result)
                    if result["status"] != "ok":
                        # 遇到错误停止后续操作，回滚
                        conn.rollback()
                        manual_schedules.clear()
                        for previous in results[:-1]:
                            previous.clear()
                            previous.update(status="rolled_back")
                        return {"status": "error", "error": result["error"], "rolled_back": True,
                                "results": results}, []
                else:
                    # 全部成功才提交
                    conn.commit()
            except Exception as e:
                conn.rollback()
                manual_schedules.clear()
                logger.error(f"定时任务批量操作异常: {e}", exc_info=True)
                return {"status": "error", "error": f"批量操作失败: {str(e)}"}, []

        return {"status": "ok", "results": results}, manual_schedules

    async def update(self, operations: list[dict]) -> dict:
        """
        执行批量定时任务操作（create_schedule / update_schedule / run_schedule）。

        任何一步失败都会整体回滚；run_schedule 在批量成功提交后才实际触发。
        """
        result, manual_schedules = await asyncio.to_thread(
            self._update_operations_sync, operations
        )
        if manual_schedules and all(r.get("status") == "ok" for r in result.get("results", [])):
            for schedule in manual_schedules:
                self._launch_run(schedule)
        return result

    # ------------------------------------------------------------------
    # 读操作（schedule_list 工具）
    # ------------------------------------------------------------------

    def _list_active_schedules_sync(self) -> list[dict]:
        with self._connect() as conn:
            cursor = conn.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE status = 'active' "
                "ORDER BY next_run_at IS NULL, next_run_at"
            )
            rows = cursor.fetchall()

        schedules = []
        for row in rows:
            s = self._row_to_schedule(row)
            schedules.append(
                {
                    "id": s["id"],
                    "name": s["name"],
                    "trigger": f"{s['trigger_type']}: {s['trigger_expr']}",
                    "next_run": _fmt_time(s["next_run_at"]),
                    "last_run": _fmt_time(s["last_run_at"]),
                    "last_status": s["last_status"],
                    "last_error": (s["last_error"] or "")[:200],
                }
            )
        return schedules

    async def list_active_schedules(self) -> list[dict]:
        """查看所有活跃任务概览。"""
        return await asyncio.to_thread(self._list_active_schedules_sync)

    def _get_schedule_sync(self, schedule_id: int) -> dict | None:
        """同步获取任务完整数据（含 inactive，供执行前复查）。"""
        with self._connect() as conn:
            cursor = conn.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE id = ?",
                (schedule_id,),
            )
            row = cursor.fetchone()
        return self._row_to_schedule(row) if row else None

    def _get_schedule_detail_sync(self, schedule_id: int, run_limit: int = 10) -> dict | None:
        """同步获取任务详情及运行记录（包含归档）。"""
        with self._connect() as conn:
            cursor = conn.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE id = ?",
                (schedule_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            run_rows = []
            if run_limit:
                cursor = conn.execute(
                    """
                    SELECT trigger, started_at, finished_at, status, result_summary, id, scheduled_for
                    FROM schedule_runs WHERE schedule_id = ? ORDER BY id DESC LIMIT ?
                    """,
                    (schedule_id, run_limit),
                )
                run_rows = cursor.fetchall()

        s = self._row_to_schedule(row)
        runs = [
            {
                "run_id": r[5],
                "scheduled_for": r[6],
                "trigger": r[0],
                "started": _fmt_time(r[1]),
                "finished": _fmt_time(r[2]),
                "status": r[3],
                "result_summary": (r[4] or "")[:500],
            }
            for r in run_rows
        ]
        return {
            "id": s["id"],
            "name": s["name"],
            "status": s["status"],
            "trigger": {"type": s["trigger_type"], "expr": s["trigger_expr"]},
            "actions": s["actions"],
            "max_runtime": s["max_runtime"],
            "on_failure": s["on_failure"],
            "next_run": _fmt_time(s["next_run_at"]),
            "last_run": _fmt_time(s["last_run_at"]),
            "last_status": s["last_status"],
            "last_error": s["last_error"],
            "recent_runs": runs,
        }

    async def get_schedule_detail(self, schedule_id: int, *, run_limit: int = 10) -> dict | None:
        """按 ID 查看任务详情（含归档）；run_limit=0 不查询运行历史。"""
        if type(run_limit) is not int or not 0 <= run_limit <= 10:
            raise ValueError("run_limit 必须是 0..10 的整数")
        return await asyncio.to_thread(self._get_schedule_detail_sync, schedule_id, run_limit)

    def _count_active_sync(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM schedules WHERE status = 'active'")
            return cursor.fetchone()[0]

    def set_heartbeat(self, heartbeat):
        """装配共享心跳服务；普通发送只通过已注册工具。"""
        self._heartbeat = heartbeat

    def _recover_runs_sync(self):
        """进程退出时未写完的执行不自动重放，保留为结果未知。"""
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            ids = [r[0] for r in conn.execute("SELECT DISTINCT schedule_id FROM schedule_runs WHERE status='running'")]
            for sid in ids:
                conn.execute("UPDATE schedules SET next_run_at=NULL,last_status='unknown',last_error=? WHERE id=?",
                             ("上次执行中断，部分动作可能已完成；不会自动重试", sid))
            conn.execute("UPDATE schedule_runs SET status='unknown',finished_at=?,result_summary=? WHERE status='running'",
                         (time.time(), "上次执行中断，结果未知；确认后可显式重试"))

    def _launch_run(self, claim):
        sid = claim['schedule']['id']
        self._active_jobs.add(sid)
        task = asyncio.create_task(self._execute_run(claim))
        self._job_tasks.add(task)

        def done(completed):
            self._job_tasks.discard(completed)
            self._active_jobs.discard(sid)
            if not completed.cancelled() and completed.exception() is not None:
                error = completed.exception()
                logger.error("Scheduler 运行异常 schedule_id=%s", sid,
                             exc_info=(type(error), error, error.__traceback__))
        task.add_done_callback(done)
        return task

    async def _run_schedule(self, schedule, trigger, *, wait=True):
        """手动、定时和补跑共用执行准入；先登记，再派发。"""
        receipt, claim = await asyncio.to_thread(self._claim_run_sync, schedule, trigger)
        if claim is None:
            return receipt
        task = self._launch_run(claim)
        return await task if wait else receipt

    async def _stop_process(self, proc):
        if os.name == 'posix':
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.returncode is None:
            # Windows 开发环境；生产使用上面的独立进程组。
            killer = await asyncio.create_subprocess_exec(
                'taskkill', '/PID', str(proc.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        await proc.wait()

    async def _run_command(self, action):
        command = action['command']
        try:
            proc = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                **({'start_new_session': True} if os.name == 'posix' else {}))
        except Exception as exc:
            return f"命令启动失败: {exc}", False
        # 持续排空管道，摘要有界，避免 communicate 为大量输出占满内存。
        output = bytearray()

        async def read_output():
            while chunk := await proc.stdout.read(8192):
                remaining = 65536 - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
            await proc.wait()

        reader = asyncio.create_task(read_output())
        try:
            await asyncio.wait_for(asyncio.shield(reader), float(action.get('timeout') or DEFAULT_MAX_RUNTIME))
        except TimeoutError:
            await self._stop_process(proc)
            await reader
            return "命令超时，进程组已终止", False
        except asyncio.CancelledError:
            await self._stop_process(proc)
            await reader
            raise
        text = output.decode('utf-8', errors='replace').strip() or '(无输出)'
        return (text, True) if proc.returncode == 0 else (f"命令退出码 {proc.returncode}: {text}", False)

    async def _run_actions(self, schedule, parts=None):
        from runtime.tool_registry import tool_result_error
        parts = [] if parts is None else parts
        for i, action in enumerate(schedule['actions']):
            if action['type'] == 'tool':
                name = action['name']
                if name in _FORBIDDEN_TOOL_ACTIONS:
                    result = json.dumps({'error': f'定时任务不允许调用 {name}'}, ensure_ascii=False)
                else:
                    result = await registry.execute(name, action.get('args') or {})
                error = tool_result_error(result)
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            elif action['type'] == 'command':
                text, ok = await self._run_command(action)
                error = None if ok else text
            else:
                return parts, '不支持的后台动作'
            parts.append(f"[{i}][{action.get('name', action['type'])}] {text[:500]}")
            if error:
                return parts, error
        return parts, ''

    async def _execute_run(self, claim):
        sid, run_id = claim['schedule']['id'], claim['run_id']
        status, error, parts = 'ok', '', []
        effects_started = False
        cancelled = False
        try:
            current = await asyncio.to_thread(self._get_schedule_sync, sid)
            if current is None or current['status'] != 'active':
                status = 'cancelled'
            elif claim['trigger'] != 'manual' and current['next_run_at'] != claim['scheduled_for']:
                status = 'cancelled'
            else:
                claim['schedule'] = current
                err = validate_actions(current['actions'], current['trigger_type'])
                if err:
                    raise ValueError(err)
                if current['actions'][0]['type'] == 'heartbeat':
                    if self._heartbeat is None:
                        raise RuntimeError('心跳服务未装配')

                    async def still_active():
                        row = await asyncio.to_thread(self._get_schedule_sync, sid)
                        return (row is not None and row['status'] == 'active'
                                and row['updated_at'] == current['updated_at'])

                    # 让路/模型请求预算由心跳入口处理；不可用通用 timeout 取消工具批次。
                    effects_started = True
                    result = await self._heartbeat.run_scheduled(
                        current['actions'][0]['content'], request_id=f'schedule-run-{run_id}',
                        before_run=still_active)
                    if result.stop_reason == 'skipped':
                        status = 'cancelled'
                    elif result.stop_reason not in {'complete', 'waiting_input'}:
                        status, error = 'error', result.error or '预约心跳执行失败'
                    elif result.stop_reason == 'waiting_input':
                        status = 'waiting_input'
                    parts.append(f"heartbeat: {result.stop_reason}")
                else:
                    async with self._slots:
                        # 排队期间归档的任务不再执行。
                        row = await asyncio.to_thread(self._get_schedule_sync, sid)
                        if row is None or row['status'] != 'active':
                            status = 'cancelled'
                        elif claim['trigger'] != 'manual' and row['next_run_at'] != claim['scheduled_for']:
                            status = 'cancelled'
                        elif row['actions'] != current['actions']:
                            status = 'cancelled'
                        else:
                            effects_started = True
                            async with asyncio.timeout(current.get('max_runtime') or DEFAULT_MAX_RUNTIME):
                                _, error = await self._run_actions(current, parts)
                            if error:
                                status = 'error'
        except TimeoutError:
            status, error = 'timeout', '动作执行超时，已中断'
        except asyncio.CancelledError:
            cancelled = True
            status = 'unknown' if effects_started else 'cancelled'
            error = '执行被中断，部分动作可能已完成' if effects_started else '执行前取消'
        except Exception as exc:
            status, error = 'error', str(exc)
            logger.exception('定时任务执行失败 schedule_id=%s run_id=%s', sid, run_id)
        summary = ('\n'.join(parts) or '(无输出)')[:RESULT_SUMMARY_MAX]
        if error:
            summary = f'{error}\n{summary}'[:RESULT_SUMMARY_MAX]
        try:
            await asyncio.to_thread(self._record_run_sync, claim, status, summary, error)
        except Exception:
            # 持久化的 running 仍占用该任务，下次轮询/重启均不能自动重放。
            status, error = 'unknown', '运行结果保存失败，部分动作可能已完成，不会自动重试'
            logger.exception('定时任务结果保存失败 schedule_id=%s run_id=%s', sid, run_id)
        if status in {'error', 'timeout'}:
            await self._notify_failure(claim['schedule'], status, error)
        if cancelled:
            raise asyncio.CancelledError
        return {'status': status, 'run_id': run_id, 'error': error}

    def _record_run_sync(self, claim, status, summary, error):
        now = time.time()
        sid = claim['schedule']['id']
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE schedule_runs SET finished_at=?,status=?,result_summary=? WHERE id=?",
                         (now, status, summary, claim['run_id']))
            current = self._ensure_schedule_active(conn, sid)
            if status == 'unknown':
                conn.execute("UPDATE schedules SET last_status=?,last_error=?,next_run_at=NULL WHERE id=?",
                             (status, error, sid))
            else:
                conn.execute("UPDATE schedules SET last_run_at=?,last_status=?,last_error=? WHERE id=?",
                             (now, status, error, sid))
                # 人工修改后的新触发时间不被旧执行覆盖。
                if (current and status != 'cancelled'
                        and (claim['scheduled_for'] is not None or claim['schedule']['last_status'] == 'unknown')
                        and current['updated_at'] == claim['schedule']['updated_at']):
                    if current['trigger_type'] == 'once' and claim['scheduled_for'] is not None:
                        next_run = None
                    elif current['trigger_type'] == 'interval' and claim['trigger'] == 'catch_up':
                        candidate = claim['scheduled_for'] + _parse_interval_seconds(current['trigger_expr'])
                        next_run = (candidate if candidate <= self._catch_up_until.get(sid, 0)
                                    else compute_next_run('interval', current['trigger_expr'], now))
                    else:
                        next_run = compute_next_run(current['trigger_type'], current['trigger_expr'], now)
                    conn.execute("UPDATE schedules SET next_run_at=?,status=?,updated_at=? WHERE id=?",
                                 (next_run, 'inactive' if next_run is None else 'active', now, sid))
            conn.execute("DELETE FROM schedule_runs WHERE schedule_id=? AND status!='running' AND id NOT IN "
                         "(SELECT id FROM schedule_runs WHERE schedule_id=? ORDER BY id DESC LIMIT ?)",
                         (sid, sid, RUN_HISTORY_KEEP))

    async def _notify_failure(self, schedule, status, detail):
        notify_platform = _valid_notify_platform((schedule.get('on_failure') or {}).get('notify_platform'))
        if not notify_platform:
            return
        from runtime.tool_registry import tool_result_error
        text = f"[定时任务{status}] {schedule['name']}（#{schedule['id']}）\n{detail[:800]}"
        try:
            async with asyncio.timeout(FAILURE_NOTIFY_TIMEOUT):
                result = await registry.execute('send_message', {'platform': notify_platform, 'text': text})
                if tool_result_error(result):
                    logger.warning('定时任务失败通知未送达 schedule_id=%s platform=%s', schedule['id'], notify_platform)
        except Exception:
            logger.warning('定时任务失败通知异常 schedule_id=%s platform=%s', schedule['id'], notify_platform, exc_info=True)

    def _due_schedules_sync(self, now):
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE status='active' AND next_run_at<=? "
                "AND (last_status IS NULL OR last_status!='unknown') "
                "AND NOT EXISTS (SELECT 1 FROM schedule_runs WHERE schedule_id=schedules.id AND status='running') "
                "ORDER BY next_run_at,id", (now,)).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    def _reschedule_sync(self, schedule_id, trigger_type, trigger_expr, now):
        next_run = compute_next_run(trigger_type, trigger_expr, now)
        with self._connect() as conn:
            conn.execute("UPDATE schedules SET next_run_at=?,updated_at=? WHERE id=? AND status='active' "
                         "AND trigger_type=? AND trigger_expr=?",
                         (next_run, now, schedule_id, trigger_type, trigger_expr))

    def _archive_sync(self, schedule_id, reason, expected_updated_at=None):
        with self._connect() as conn:
            conn.execute("UPDATE schedules SET status='inactive',last_error=?,next_run_at=NULL,updated_at=? "
                         "WHERE id=? AND (? IS NULL OR updated_at=?)",
                         (reason, time.time(), schedule_id, expected_updated_at, expected_updated_at))

    async def _catch_up_interval(self, row, now):
        """只规划补跑游标，不等待动作；逐次执行通过统一准入。"""
        try:
            interval = _parse_interval_seconds(row['trigger_expr'])
        except ValueError as exc:
            await asyncio.to_thread(self._archive_sync, row['id'], str(exc), row['updated_at'])
            return
        first = row['next_run_at']
        last_k = math.floor((now - first) / interval)
        first_k = max(0, math.ceil((now - self.interval_catch_up_grace_seconds - first) / interval),
                      last_k - MAX_CATCH_UP_RUNS + 1)
        if first_k > last_k:
            await asyncio.to_thread(self._reschedule_sync, row['id'], 'interval', row['trigger_expr'], now)
            return
        earliest = first + first_k * interval
        self._catch_up_until[row['id']] = now
        await asyncio.to_thread(self._set_catch_up_cursor_sync, row['id'], first, earliest)

    def _set_catch_up_cursor_sync(self, sid, previous, earliest):
        with self._connect() as conn:
            conn.execute("UPDATE schedules SET next_run_at=? WHERE id=? AND next_run_at=? AND status='active'",
                         (earliest, sid, previous))

    async def _startup_catch_up(self):
        now = time.time()
        for row in await asyncio.to_thread(self._due_schedules_sync, now):
            if row['trigger_type'] == 'interval':
                await self._catch_up_interval(row, now)
            elif row['trigger_type'] == 'cron':
                await asyncio.to_thread(self._reschedule_sync, row['id'], 'cron', row['trigger_expr'], now)
            elif now - row['next_run_at'] > self.catch_up_grace_seconds:
                await asyncio.to_thread(self._archive_sync, row['id'], 'once 已超过补跑宽限期，自动归档', row['updated_at'])
        # 正式轮询会公平派发所有符合条件的任务，不在启动阶段逐任务等待。

    async def _loop(self):
        initialized = False
        while self._running:
            try:
                if not initialized:
                    await self._startup_catch_up()
                    initialized = True
                for row in await asyncio.to_thread(self._due_schedules_sync, time.time()):
                    trigger = 'catch_up' if row['next_run_at'] <= self._catch_up_until.get(row['id'], 0) else 'scheduled'
                    await self._run_schedule(row, trigger, wait=False)
            except Exception:
                logger.exception('Scheduler 轮询失败')
            await asyncio.sleep(self._poll_interval)

    async def run(self):
        if not self._running:
            self._running = True
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for task in tuple(self._job_tasks):
            task.cancel()
        await asyncio.gather(*self._job_tasks, return_exceptions=True)
        self._job_tasks.clear()
        self._active_jobs.clear()


_scheduler_instance: Scheduler | None = None


def get_scheduler(db_path='data/scheduler.db', catch_up_grace_hours=DEFAULT_CATCH_UP_GRACE_HOURS,
                  interval_catch_up_grace_hours=DEFAULT_INTERVAL_CATCH_UP_GRACE_HOURS):
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = Scheduler(db_path, catch_up_grace_hours, interval_catch_up_grace_hours)
    return _scheduler_instance
