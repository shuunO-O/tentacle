"""
用量统计模块

职责：
1. 持久化每次 API 调用的 token 用量（usage_logs 表，chronicle.db），
   重启不丢；AgentLoop 在派发 USAGE 事件时写入，覆盖 web/qq 两个平台
2. 按天/按小时聚合查询（供统计页图表）
3. DeepSeek 余额查询代理（GET /user/balance，带内存缓存避免频繁请求）

设计要点：
- 单例模式；DB 操作 asyncio.to_thread 包装，写库失败仅记日志不影响主循环
- 聚合在 Python 侧完成（避免 SQLite strftime 的 UTC 时区问题）
- 后台模型（compress/memory_extractor）的调用同样记录，
  以 platform 字段区分主对话与后台任务
"""

import asyncio
import logging
import hashlib
import json
import os
import sqlite3
import time
from datetime import date, timedelta
from runtime.timezone import day_boundaries, local_datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _aiohttp():
    mod = globals().get("aiohttp")
    if mod is None:
        import aiohttp as mod

        globals()["aiohttp"] = mod
    return mod


def __getattr__(name: str):
    if name == "aiohttp":
        return _aiohttp()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 余额查询缓存时长（秒）
BALANCE_CACHE_SECONDS = 300


class UsageTracker:
    """
    用量统计器（单例）

    usage_logs 表结构：
        id, created_time, platform, model,
        prompt_tokens, completion_tokens, total_tokens,
        cached_tokens, reasoning_tokens, cache_write_tokens
    （旧库缺新列时启动自动 ALTER TABLE 迁移）
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path: str = "data/chronicle.db"):
        if self._initialized:
            return
        self.db_path = Path(db_path)
        self._init_db()
        # 余额查询配置（configure 注入，与 files 段共用 API Key）
        self._balance_base_url = ""
        self._balance_api_key = ""
        self._balance_cache: dict | None = None
        self._balance_cache_time = 0.0
        self._balance_accounts = {}
        self._balance_slots = asyncio.Semaphore(3)
        self._initialized = True

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_time REAL NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'unknown',
                    model TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_logs_time ON usage_logs(created_time)"
            )
            # 旧库迁移：补缺失列（幂等，ALTER 后默认值填充旧行）
            cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_logs)")}
            if "reasoning_tokens" not in cols:
                conn.execute(
                    "ALTER TABLE usage_logs ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "cache_write_tokens" not in cols:
                conn.execute(
                    "ALTER TABLE usage_logs ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0"
                )

            for column in ("provider_id", "model_id", "role"):
                if column not in cols:
                    conn.execute(f"ALTER TABLE usage_logs ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    def configure(self, config: dict):
        """
        注入明确选择的供应商连接；不与 files 存储配置共用。

        Args:
            config: {"base_url": ..., "api_key_env": ...}
        """
        import os

        config = config or {}
        self._balance_base_url = (config.get("base_url") or "https://api.deepseek.com").rstrip("/")
        api_key_env = config.get("api_key_env", "")
        self._balance_api_key = os.getenv(api_key_env, "") if api_key_env else ""
        self._balance_proxy = config.get("proxy")
        self._balance_url = config.get("balance_url") or f"{self._balance_base_url}/user/balance"
        self._balance_cache = None
        self._balance_cache_time = 0.0

    # ==================== 记录 ====================

    async def record_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        cache_write_tokens: int = 0,
        platform: str = "unknown",
        model: str = "",
        provider_id: str = "",
        model_id: str = "",
        role: str = "",
    ) -> None:
        """记录一次 API 调用的用量（失败不影响主循环）"""
        try:
            await asyncio.to_thread(
                self._db_insert,
                time.time(),
                platform,
                model,
                prompt_tokens or 0,
                completion_tokens or 0,
                total_tokens or 0,
                cached_tokens or 0,
                reasoning_tokens or 0,
                cache_write_tokens or 0,
                provider_id, model_id, role,
            )
        except Exception as e:
            logger.error(f"用量记录写入失败: {e}", exc_info=True)

    def _db_insert(self, created_time, platform, model, pt, ct, tt, kct, rt, cwt,
                   provider_id="", model_id="", role=""):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO usage_logs
                    (created_time, platform, model, prompt_tokens, completion_tokens,
                     total_tokens, cached_tokens, reasoning_tokens, cache_write_tokens,
                     provider_id, model_id, role)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (created_time, platform, model, pt, ct, tt, kct, rt, cwt, provider_id, model_id, role),
            )

    # ==================== 聚合查询 ====================

    def _db_rows(self, start_time: float, end_time: float, filters=None) -> list[tuple]:
        with sqlite3.connect(self.db_path) as conn:
            clauses, params = [], [start_time, end_time]
            for key, value in (filters or {}).items():
                if value is not None:
                    expression = "COALESCE(NULLIF(model_id, ''), 'legacy:' || model)" if key == "model_id" else key
                    clauses.append(f" AND {expression} = ?")
                    params.append("" if value == ":unknown" else value)
            cur = conn.execute(
                """
                SELECT created_time, prompt_tokens, completion_tokens, total_tokens,
                       cached_tokens, reasoning_tokens, cache_write_tokens
                FROM usage_logs
                WHERE created_time >= ? AND created_time < ?
                """ + "".join(clauses) + " ORDER BY created_time ASC",
                params,
            )
            return cur.fetchall()

    def _aggregate(self, rows: list[tuple], granularity: str) -> list[dict]:
        """
        Python 侧按程序时区聚合

        Args:
            granularity: "day" 或 "hour"
        """
        fmt = "%Y-%m-%d" if granularity == "day" else "%Y-%m-%d %H:00"
        buckets: dict[str, dict] = {}
        for created_time, pt, ct, tt, kct, rt, cwt in rows:
            key = local_datetime(created_time).strftime(fmt)
            b = buckets.setdefault(key, {
                "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cached_tokens": 0,
                "reasoning_tokens": 0, "cache_write_tokens": 0,
                "requests": 0,
            })
            b["prompt_tokens"] += pt
            b["completion_tokens"] += ct
            b["total_tokens"] += tt
            b["cached_tokens"] += kct
            b["reasoning_tokens"] += rt
            b["cache_write_tokens"] += cwt
            b["requests"] += 1

        points = []
        for key in sorted(buckets.keys()):
            b = buckets[key]
            # 命中率 = 缓存命中 / 输入（无输入的桶记 None，前端折线断开）
            b["cache_hit_rate"] = round(b["cached_tokens"] / b["prompt_tokens"] * 100, 1) \
                if b["prompt_tokens"] > 0 else None
            points.append({"bucket": key, **b})
        return points

    async def query_usage(self, granularity: str = "day", window: int | None = None,
                          *, start_date: str | None = None, end_date: str | None = None,
                          provider_id: str | None = None, model_id: str | None = None,
                          role: str | None = None, config: dict | None = None) -> dict:
        """
        聚合查询

        Args:
            granularity: "day"（默认窗口 30 天）或 "hour"（默认窗口 48 小时）
            window: 自定义窗口数（天数/小时数）；给出日期区间时忽略
            start_date/end_date: 程序时区下的 "YYYY-MM-DD"；给出后图表与汇总都限制在该区间
                （只给 start 表示到今天，只给 end 表示从最早记录到该日）

        Returns:
            {"granularity", "points": [...], "summary": {...}, "range": {...}|None}
        """
        if granularity not in ("day", "hour"):
            granularity = "day"
        window = window or (30 if granularity == "day" else 48)

        now = time.time()
        start_day = self._parse_date(start_date, "起始日期")
        end_day = self._parse_date(end_date, "结束日期")
        if start_day and end_day:
            if start_day > end_day:
                raise ValueError("起始日期不能晚于结束日期")
            limit = 92 if granularity == "hour" else 366
            span = (end_day - start_day).days + 1
            if span > limit:
                raise ValueError(f"按{'小时' if granularity == 'hour' else '天'}统计的日期范围最多 {limit} 天")
            range_start = day_boundaries(start_day)[0]
            range_end = day_boundaries(end_day + timedelta(days=1))[0]
        elif start_day:
            range_start = day_boundaries(start_day)[0]
            range_end = now + 1
        elif end_day:
            range_start = 0.0  # 未给起始日：从最早记录算起
            range_end = day_boundaries(end_day + timedelta(days=1))[0]
        else:
            range_start = range_end = None

        if range_start is None:
            chart_start = self._default_window_start(granularity, window, now)
            fetch_start = min(chart_start, now - 30 * 86400)
            fetch_end = now + 1
        else:
            # 日期过滤：汇总也只统计区间内数据，故无需额外回溯 30 天
            chart_start, fetch_start, fetch_end = range_start, range_start, range_end

        filters = {"provider_id": provider_id, "model_id": model_id, "role": role}
        rows = await asyncio.to_thread(self._db_rows, fetch_start, fetch_end, filters)
        points = self._aggregate([r for r in rows if r[0] >= chart_start], granularity)

        # 汇总：今日 / 最近 7 天 / 最近 30 天（日期过滤时与区间取交集）
        today_start = local_datetime().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        week_start = now - 7 * 86400
        month_start = now - 30 * 86400

        summary = {}
        for name, period_start in (("today", today_start), ("week", week_start), ("month", month_start)):
            lower = max(period_start, fetch_start)
            summary[name] = self._summarize_rows([r for r in rows if lower <= r[0] < fetch_end])
        if range_start is None:
            summary["all"] = await asyncio.to_thread(self._db_total, filters, now + 1)
        else:
            summary["all"] = self._summarize_rows(rows)

        options = await asyncio.to_thread(self._filter_options, config or {})
        return {"granularity": granularity, "points": points, "summary": summary,
                "options": options, "filters": filters,
                "range": self._range_info(start_day, end_day, fetch_start, fetch_end)}

    @staticmethod
    def _parse_date(value: str | None, field: str) -> date | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{field}格式应为 YYYY-MM-DD") from None

    @staticmethod
    def _default_window_start(granularity: str, window: int, now: float) -> float:
        if granularity == "day":
            return (local_datetime(now) - timedelta(days=window - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
        return now - window * 3600

    @staticmethod
    def _range_info(start_day: date | None, end_day: date | None,
                    fetch_start: float, fetch_end: float) -> dict | None:
        if start_day is None and end_day is None:
            return None
        if start_day and end_day:
            days = (end_day - start_day).days + 1
        elif start_day:
            days = (local_datetime(fetch_end - 1).date() - start_day).days + 1
        else:
            days = None  # 起始日不设限，日均没有确定口径
        return {
            "start_date": start_day.isoformat() if start_day else None,
            "end_date": end_day.isoformat() if end_day else None,
            "days": days,
        }

    def _summarize_rows(self, rows: list[tuple]) -> dict:
        summary = {
            "prompt_tokens": sum(r[1] for r in rows),
            "completion_tokens": sum(r[2] for r in rows),
            "total_tokens": sum(r[3] for r in rows),
            "cached_tokens": sum(r[4] for r in rows),
            "reasoning_tokens": sum(r[5] for r in rows),
            "cache_write_tokens": sum(r[6] for r in rows),
            "requests": len(rows),
        }
        summary["cache_hit_rate"] = (
            round(summary["cached_tokens"] / summary["prompt_tokens"] * 100, 1)
            if summary["prompt_tokens"] else None
        )
        return summary

    def _db_total(self, filters, end_time):
        columns = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
                   "reasoning_tokens", "cache_write_tokens")
        clauses, params = ["created_time < ?"], [end_time]
        for key, value in filters.items():
            if value is not None:
                expression = "COALESCE(NULLIF(model_id, ''), 'legacy:' || model)" if key == "model_id" else key
                clauses.append(f"{expression} = ?")
                params.append("" if value == ":unknown" else value)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT " + ", ".join(f"COALESCE(SUM({column}), 0)" for column in columns)
                + ", COUNT(*) FROM usage_logs WHERE " + " AND ".join(clauses), params,
            ).fetchone()
        result = dict(zip((*columns, "requests"), row))
        result["cache_hit_rate"] = round(result["cached_tokens"] / result["prompt_tokens"] * 100, 1) if result["prompt_tokens"] else None
        return result

    def _filter_options(self, config):
        # Read actual recorded identities, never infer historical ownership from today's roles.
        with sqlite3.connect(self.db_path) as conn:
            identities = conn.execute(
                "SELECT DISTINCT provider_id, model_id, role, model FROM usage_logs"
            ).fetchall()
        options = {"providers": {}, "models": {}, "roles": {}}

        def add(kind, key):
            key = key or ":unknown"
            label = (config.get(kind, {}).get(key) or {}).get("label") or key
            options[kind][key] = {"id": key, "label": "未记录" if key == ":unknown" else label}

        def add_model(model_id, name, provider):
            key = model_id or "legacy:" + name
            label = (config.get("models", {}).get(model_id) or {}).get("label") or model_id
            item = options["models"].setdefault(key, {
                "id": key, "label": label or ((name or "未记录") + "（历史记录）"), "provider_ids": [],
            })
            provider = provider or ":unknown"
            if provider not in item["provider_ids"]:
                item["provider_ids"].append(provider)

        # Include unused configuration entries so they can be selected even before the first call.
        for kind in ("providers", "roles"):
            for key in config.get(kind, {}):
                add(kind, key)
        for key, model in config.get("models", {}).items():
            add_model(key, model.get("name", ""), model.get("provider", ""))
        for provider, model_id, role, name in identities:
            add("providers", provider)
            add("roles", role)
            add_model(model_id, name, provider)
        return {kind: sorted(items.values(), key=lambda item: item["label"])
                for kind, items in options.items()}


    # ==================== 余额 ====================

    async def get_provider_balances(self, config, provider_id=None, force_refresh=False):
        """Balances belong to connections, independent of the selected model and role."""
        from config.providers import PROVIDERS, connection

        providers = config.get("providers", {})
        keys = [provider_id] if provider_id else list(providers)

        async def fetch(key):
            value = providers.get(key)
            identity = {"provider_id": key, "label": (value or {}).get("label") or ("未记录" if key == ":unknown" else key)}
            if value is None:
                return {**identity, "ok": False, "error": "该供应商未配置，无法查询余额"}
            mode = value.get("balance_mode", "auto")
            if mode == "none":
                return {**identity, "ok": False, "error": "该供应商已关闭余额查询"}
            if mode != "deepseek" and not PROVIDERS[value["type"]].balance:
                return {**identity, "ok": False, "error": "该供应商暂不支持余额查询"}
            settings = connection(config, key)
            signature = hashlib.sha256(json.dumps({
                "config": settings, "key": os.getenv(settings.get("api_key_env", ""), ""),
            }, sort_keys=True).encode()).hexdigest()
            if signature not in self._balance_accounts:
                if len(self._balance_accounts) >= 64:
                    self._balance_accounts.clear()
                account = object.__new__(UsageTracker)
                account.configure(settings)
                self._balance_accounts[signature] = (account, asyncio.Lock())
            account, lock = self._balance_accounts[signature]
            async with lock:
                async with self._balance_slots:
                    result = await account.get_balance(force_refresh)
            return {**identity, **result}

        return {"items": await asyncio.gather(*(fetch(key) for key in keys))}

    async def get_balance(self, force_refresh: bool = False) -> dict:
        """
        查询 DeepSeek 账户余额（带 5 分钟缓存）

        Returns:
            {"ok": True, "is_available", "balances": [...], "fetched_at"}
            或 {"ok": False, "error": ...}
        """
        if not self._balance_api_key:
            return {"ok": False, "error": "余额查询未配置（缺少 API Key）"}

        if not force_refresh and self._balance_cache is not None:
            if time.time() - self._balance_cache_time < BALANCE_CACHE_SECONDS:
                return self._balance_cache

        aiohttp = _aiohttp()
        try:
            async with aiohttp.ClientSession(
                trust_env=getattr(self, "_balance_proxy", None) is None,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                async with session.get(
                    getattr(self, "_balance_url", f"{self._balance_base_url}/user/balance"),
                    headers={"Authorization": f"Bearer {self._balance_api_key}"},
                    proxy=getattr(self, "_balance_proxy", None) or None,
                    allow_redirects=False,
                ) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status != 200:
                        return {"ok": False, "error": f"余额查询失败（HTTP {resp.status}）"}
        except Exception:
            return {"ok": False, "error": "余额查询失败，请检查连接与凭据配置"}

        if not isinstance(payload, dict) or not isinstance(payload.get("balance_infos"), list):
            return {"ok": False, "error": "余额接口返回格式不受支持"}
        balances = []
        for item in payload["balance_infos"]:
            if not isinstance(item, dict) or not isinstance(item.get("currency"), str):
                return {"ok": False, "error": "余额接口返回格式不受支持"}
            if not isinstance(item.get("total_balance"), (str, int, float)):
                return {"ok": False, "error": "余额接口返回格式不受支持"}
            balances.append({key: item.get(key) for key in (
                "currency", "total_balance", "topped_up_balance", "granted_balance",
            )})
        result = {
            "ok": True,
            "is_available": payload.get("is_available"),
            "balances": balances,
            "fetched_at": time.time(),
        }
        self._balance_cache = result
        self._balance_cache_time = time.time()
        return result


# 全局单例访问入口
def get_usage_tracker() -> UsageTracker:
    return UsageTracker()
