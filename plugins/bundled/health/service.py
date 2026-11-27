"""Intervals.icu 只读客户端、本地 SQLite 全量归档与读取期空值过滤服务。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


DEFAULT_BASE_URL = "https://intervals.icu/api/v1"
ATHLETE_ID_RE = re.compile(r"^(0|[A-Za-z0-9_-]{1,64})$")
ACTIVITY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONTROL_WS_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")

MAX_LIVE_RANGE_DAYS = 366
MAX_ARCHIVE_RANGE_DAYS = 3650
MAX_ACTIVITIES_LIMIT = 100
DEFAULT_ACTIVITIES_LIMIT = 20

# Intervals.icu /wellness 默认自带的元信息或无值占位布尔字段
_WELLNESS_META_KEYS = frozenset({"id", "updated"})
_WELLNESS_DEFAULT_FALSE_FLAGS = frozenset({"tempWeight", "tempRestingHR", "locked"})
_ADDITIVE_METRICS = frozenset({"steps", "floorsClimbed", "kcalConsumed", "sleepSecs", "hydrationVolume"})

# 运动记录摘要保留的高价值字段（仍会自动剔除 null/空值）
_ACTIVITY_SUMMARY_KEYS = (
    "id",
    "start_date_local",
    "type",
    "name",
    "description",
    "moving_time",
    "elapsed_time",
    "distance",
    "total_elevation_gain",
    "average_heartrate",
    "max_heartrate",
    "average_speed",
    "calories",
    "icu_training_load",
    "icu_intensity",
    "perceived_exertion",
    "feel",
    "source",
    "device_name",
)


class HealthServiceError(RuntimeError):
    """运动健康同步或查询错误（错误信息均已脱敏，不含 API Key）。"""


def sanitize_secret(text: str, secret: str) -> str:
    """确保任何日志或错误信息绝不回显 API Key。"""
    cleaned = str(text or "")
    if secret and len(secret) >= 4:
        cleaned = cleaned.replace(secret, "***")
    return cleaned


def clean_inline_text(value: Any, max_len: int = 300) -> str:
    text = CONTROL_WS_RE.sub(" ", str(value or "")).strip()
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def format_duration_seconds(seconds: Any) -> str | None:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    total = int(round(seconds))
    if total <= 0:
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分" if minutes > 0 else f"{hours}小时"
    if minutes > 0:
        return f"{minutes}分{secs}秒" if secs > 0 else f"{minutes}分"
    return f"{secs}秒"


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_iso_date(raw: Any, field_name: str = "date") -> date:
    text = str(raw or "").strip()
    if not DATE_RE.fullmatch(text):
        raise ValueError(f"{field_name} 必须为 YYYY-MM-DD 格式日期")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field_name} 不是合法日期: {text}") from None


def validate_timezone(tz_name: str) -> ZoneInfo:
    cleaned = (tz_name or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(cleaned)
    except ZoneInfoNotFoundError:
        raise ValueError(f"无效时区: {cleaned}") from None


def validate_athlete_id(athlete_id: str) -> str:
    cleaned = (athlete_id or "0").strip() or "0"
    if not ATHLETE_ID_RE.fullmatch(cleaned):
        raise ValueError("athlete_id 仅允许 0 或 1-64 位字母/数字/_/-")
    return cleaned


def clean_wellness_record(
    raw: dict[str, Any],
    *,
    fields: list[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """从原始全量 wellness 记录中过滤空值与无意义默认占位字段。

    返回 `(cleaned_record, has_metrics)`；`has_metrics` 表示除日期/更新时间外是否存在至少一项有效健康数据。
    """
    date_str = str(raw.get("id") or raw.get("date") or "").strip()
    allowed = {f.strip() for f in fields if isinstance(f, str) and f.strip()} if fields else None

    metrics: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _WELLNESS_META_KEYS:
            continue
        if allowed is not None and key not in allowed:
            continue
        if _is_empty_value(value):
            continue
        if key in _WELLNESS_DEFAULT_FALSE_FLAGS and value is False:
            continue
        if isinstance(value, str):
            metrics[key] = clean_inline_text(value, 500)
        else:
            metrics[key] = value

    if "sleepSecs" in metrics and _is_number(metrics["sleepSecs"]) and metrics["sleepSecs"] > 0:
        metrics["sleep_hours"] = round(float(metrics["sleepSecs"]) / 3600.0, 2)
        sleep_text = format_duration_seconds(metrics["sleepSecs"])
        if sleep_text:
            metrics["sleep_text"] = sleep_text

    result: dict[str, Any] = {"date": date_str}
    if raw.get("updated"):
        result["updated"] = str(raw["updated"])
    result.update(metrics)
    return result, bool(metrics)


def summarize_wellness_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """自动对所有出现过的数值指标生成区间统计，未来新增睡眠/心率/体重等指标自动生效。"""
    buckets: dict[str, list[float]] = {}
    for rec in records:
        for key, value in rec.items():
            if key in {"date", "updated", "sleep_hours", "sleep_text"}:
                continue
            if _is_number(value):
                buckets.setdefault(key, []).append(float(value))

    stats: dict[str, Any] = {}
    for key in sorted(buckets):
        vals = buckets[key]
        if not vals:
            continue
        all_int = all(v.is_integer() for v in vals)
        avg_val = round(sum(vals) / len(vals), 2)
        min_val: int | float = int(min(vals)) if all_int else round(min(vals), 2)
        max_val: int | float = int(max(vals)) if all_int else round(max(vals), 2)
        entry: dict[str, Any] = {
            "days": len(vals),
            "avg": avg_val,
            "min": min_val,
            "max": max_val,
        }
        if key in _ADDITIVE_METRICS:
            total = sum(vals)
            entry["total"] = int(total) if all_int else round(total, 2)
        if key == "sleepSecs":
            avg_text = format_duration_seconds(avg_val)
            if avg_text:
                entry["avg_text"] = avg_text
                entry["avg_hours"] = round(avg_val / 3600.0, 2)
        stats[key] = entry
    return stats


def clean_activity_record(raw: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
    """从原始全量运动记录中过滤空值；默认输出精简摘要，detailed=True 输出完整非空标量字段。"""
    cleaned: dict[str, Any] = {}
    if not detailed:
        for key in _ACTIVITY_SUMMARY_KEYS:
            value = raw.get(key)
            if _is_empty_value(value):
                continue
            if isinstance(value, str):
                cleaned[key] = clean_inline_text(value, 300)
            else:
                cleaned[key] = value
    else:
        for key, value in raw.items():
            if _is_empty_value(value):
                continue
            # 跳过超大嵌套数组以免撑爆模型上下文；保留标量与小型摘要字典/短列表
            if isinstance(value, list) and len(value) > 20:
                continue
            if isinstance(value, dict) and len(value) > 30:
                continue
            if isinstance(value, str):
                cleaned[key] = clean_inline_text(value, 500)
            else:
                cleaned[key] = value

    if _is_number(cleaned.get("moving_time")) and cleaned["moving_time"] > 0:
        dur = format_duration_seconds(cleaned["moving_time"])
        if dur:
            cleaned["moving_time_text"] = dur
    if _is_number(cleaned.get("distance")) and cleaned["distance"] > 0:
        cleaned["distance_km"] = round(float(cleaned["distance"]) / 1000.0, 2)
    return cleaned


class IntervalsClient:
    """Intervals.icu 只读 HTTP 客户端（仅发起 GET 请求，支持显式代理与凭据脱敏）。"""

    def __init__(
        self,
        *,
        api_key: str,
        athlete_id: str = "0",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        proxy: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api_key = (api_key or "").strip()
        if not self._api_key:
            raise ValueError("缺少 Intervals.icu API Key")
        self._athlete_id = validate_athlete_id(athlete_id)
        self._base_url = base_url.rstrip("/")
        self._timeout = max(3.0, min(float(timeout), 60.0))
        self._proxy = (proxy or "").strip()
        self._transport = transport

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "trust_env": False,
            "auth": httpx.BasicAuth("API_KEY", self._api_key),
            "headers": {"Accept": "application/json"},
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return kwargs

    async def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException:
            raise HealthServiceError(f"请求 Intervals.icu 超时（>{self._timeout:.0f}秒）") from None
        except httpx.HTTPError as exc:
            detail = sanitize_secret(str(exc), self._api_key)
            raise HealthServiceError(f"请求 Intervals.icu 网络失败: {detail}") from None

        if response.status_code == 401:
            raise HealthServiceError("Intervals.icu 鉴权失败（HTTP 401），请检查 API Key 是否正确")
        if response.status_code == 403:
            raise HealthServiceError("Intervals.icu 拒绝访问（HTTP 403），请检查 Athlete ID 或账号权限")
        if response.status_code == 404:
            raise HealthServiceError("Intervals.icu 目标记录不存在（HTTP 404）")
        if response.status_code == 429:
            raise HealthServiceError("Intervals.icu 请求过于频繁（HTTP 429），请稍后再试")
        if response.status_code >= 400:
            raise HealthServiceError(f"Intervals.icu 接口返回异常状态（HTTP {response.status_code}）")

        try:
            return response.json()
        except ValueError:
            raise HealthServiceError("Intervals.icu 返回内容不是有效 JSON") from None

    async def fetch_wellness(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        data = await self._get_json(
            f"/athlete/{self._athlete_id}/wellness",
            params={"oldest": oldest, "newest": newest},
        )
        if not isinstance(data, list):
            raise HealthServiceError("Intervals.icu wellness 返回格式异常（期望 JSON 数组）")
        return [item for item in data if isinstance(item, dict)]

    async def fetch_activities(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        data = await self._get_json(
            f"/athlete/{self._athlete_id}/activities",
            params={"oldest": oldest, "newest": newest},
        )
        if not isinstance(data, list):
            raise HealthServiceError("Intervals.icu activities 返回格式异常（期望 JSON 数组）")
        return [item for item in data if isinstance(item, dict)]

    async def fetch_activity(self, activity_id: str) -> dict[str, Any]:
        cleaned_id = str(activity_id or "").strip()
        if not ACTIVITY_ID_RE.fullmatch(cleaned_id):
            raise ValueError("activity_id 格式无效")
        data = await self._get_json(f"/activity/{cleaned_id}")
        if not isinstance(data, dict):
            raise HealthServiceError("Intervals.icu activity 返回格式异常（期望 JSON 对象）")
        return data


class HealthArchiveService:
    """结合实时拉取与本地 SQLite 全量归档的只读运动健康服务。"""

    def __init__(
        self,
        db_path: Path,
        client: IntervalsClient,
        *,
        tz_name: str = "Asia/Shanghai",
    ):
        self.db_path = Path(db_path)
        self.client = client
        self.tz = validate_timezone(tz_name)
        self.tz_name = self.tz.key
        self._ready = False
        self._sync_lock = asyncio.Lock()

    def today_local(self) -> date:
        return datetime.now(self.tz).date()

    @contextmanager
    def _db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            if not self._ready:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS wellness_archive (
                        date TEXT PRIMARY KEY,
                        updated_at TEXT NOT NULL,
                        synced_at REAL NOT NULL,
                        raw_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS activity_archive (
                        id TEXT PRIMARY KEY,
                        start_date_local TEXT NOT NULL,
                        date TEXT NOT NULL,
                        activity_type TEXT NOT NULL,
                        name TEXT NOT NULL,
                        synced_at REAL NOT NULL,
                        raw_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_activity_date ON activity_archive(date, start_date_local);
                    CREATE TABLE IF NOT EXISTS sync_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                conn.commit()
                self._ready = True
            with conn:
                yield conn
        finally:
            conn.close()

    def _set_meta(self, db: sqlite3.Connection, updates: dict[str, str]) -> None:
        db.executemany(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES (?, ?)",
            list(updates.items()),
        )

    def _upsert_wellness_sync(self, records: list[dict[str, Any]], now_ts: float) -> int:
        rows: list[tuple[str, str, float, str]] = []
        for rec in records:
            day = str(rec.get("id") or "").strip()
            if not DATE_RE.fullmatch(day):
                continue
            updated = str(rec.get("updated") or "")
            rows.append((day, updated, now_ts, json.dumps(rec, ensure_ascii=False, separators=(",", ":"))))
        if not rows:
            return 0
        with self._db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO wellness_archive (date, updated_at, synced_at, raw_json) VALUES (?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def _upsert_activities_sync(self, activities: list[dict[str, Any]], now_ts: float) -> int:
        rows: list[tuple[str, str, str, str, str, float, str]] = []
        for act in activities:
            act_id = str(act.get("id") or "").strip()
            if not act_id:
                continue
            start_local = str(act.get("start_date_local") or act.get("start_date") or "").strip()
            day = start_local[:10] if len(start_local) >= 10 and DATE_RE.fullmatch(start_local[:10]) else ""
            act_type = str(act.get("type") or "").strip()
            name = clean_inline_text(act.get("name"), 200)
            rows.append((
                act_id,
                start_local,
                day,
                act_type,
                name,
                now_ts,
                json.dumps(act, ensure_ascii=False, separators=(",", ":")),
            ))
        if not rows:
            return 0
        with self._db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO activity_archive "
                "(id, start_date_local, date, activity_type, name, synced_at, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def _record_sync_meta_sync(self, updates: dict[str, str]) -> None:
        with self._db() as db:
            self._set_meta(db, updates)

    def resolve_date_range(
        self,
        *,
        oldest: Any = None,
        newest: Any = None,
        days: Any = None,
        default_days: int = 7,
        max_days: int = MAX_LIVE_RANGE_DAYS,
    ) -> tuple[str, str]:
        today = self.today_local()
        end_date = parse_iso_date(newest, "newest") if newest not in (None, "") else today
        if oldest not in (None, ""):
            start_date = parse_iso_date(oldest, "oldest")
        else:
            if days in (None, ""):
                span = default_days
            elif isinstance(days, bool):
                raise ValueError("days 必须为正整数")
            else:
                try:
                    span = int(days)
                except (TypeError, ValueError):
                    raise ValueError("days 必须为正整数") from None
            if span < 1 or span > max_days:
                raise ValueError(f"days 须在 1 到 {max_days} 之间")
            start_date = end_date - timedelta(days=span - 1)
        if start_date > end_date:
            raise ValueError("oldest 不能晚于 newest")
        if (end_date - start_date).days + 1 > max_days:
            raise ValueError(f"单次查询日期跨度不能超过 {max_days} 天")
        return start_date.isoformat(), end_date.isoformat()

    async def sync_range(
        self,
        oldest: str,
        newest: str,
        *,
        include_wellness: bool = True,
        include_activities: bool = True,
    ) -> dict[str, Any]:
        """实时拉取指定区间并全量归档到本地 SQLite（只读远端，写入本地归档）。"""
        async with self._sync_lock:
            now_ts = time.time()
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            wellness_count = 0
            activity_count = 0
            try:
                if include_wellness:
                    raw_wellness = await self.client.fetch_wellness(oldest, newest)
                    wellness_count = await asyncio.to_thread(self._upsert_wellness_sync, raw_wellness, now_ts)
                if include_activities:
                    raw_activities = await self.client.fetch_activities(oldest, newest)
                    activity_count = await asyncio.to_thread(self._upsert_activities_sync, raw_activities, now_ts)
            except Exception as exc:
                safe_err = sanitize_secret(str(exc), self.client._api_key)
                await asyncio.to_thread(
                    self._record_sync_meta_sync,
                    {"last_error": safe_err, "last_error_at": now_iso},
                )
                raise

            meta = {
                "last_sync_at": now_iso,
                "last_sync_range": f"{oldest}..{newest}",
                "last_error": "",
            }
            if include_wellness:
                meta["last_sync_wellness_count"] = str(wellness_count)
            if include_activities:
                meta["last_sync_activity_count"] = str(activity_count)
            await asyncio.to_thread(self._record_sync_meta_sync, meta)
            return {
                "oldest": oldest,
                "newest": newest,
                "archived_wellness_days": wellness_count,
                "archived_activities": activity_count,
                "synced_at": now_iso,
            }

    def _read_wellness_archive_sync(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT raw_json FROM wellness_archive WHERE date >= ? AND date <= ? ORDER BY date ASC",
                (oldest, newest),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                parsed = json.loads(row["raw_json"])
                if isinstance(parsed, dict):
                    result.append(parsed)
            except ValueError:
                continue
        return result

    def _read_activities_archive_sync(
        self,
        oldest: str,
        newest: str,
        *,
        activity_type: str = "",
        limit: int = DEFAULT_ACTIVITIES_LIMIT,
    ) -> list[dict[str, Any]]:
        with self._db() as db:
            if activity_type:
                rows = db.execute(
                    "SELECT raw_json FROM activity_archive "
                    "WHERE date >= ? AND date <= ? AND LOWER(activity_type) = LOWER(?) "
                    "ORDER BY start_date_local DESC LIMIT ?",
                    (oldest, newest, activity_type, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT raw_json FROM activity_archive "
                    "WHERE date >= ? AND date <= ? "
                    "ORDER BY start_date_local DESC LIMIT ?",
                    (oldest, newest, limit),
                ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                parsed = json.loads(row["raw_json"])
                if isinstance(parsed, dict):
                    result.append(parsed)
            except ValueError:
                continue
        return result

    def _read_single_activity_archive_sync(self, activity_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT raw_json FROM activity_archive WHERE id = ?",
                (activity_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(row["raw_json"])
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None

    async def query_wellness(
        self,
        *,
        oldest: Any = None,
        newest: Any = None,
        days: Any = None,
        source: str = "auto",
        fields: list[str] | None = None,
        include_empty_days: bool = False,
    ) -> dict[str, Any]:
        mode = (source or "auto").strip().lower()
        if mode not in {"auto", "live", "archive"}:
            raise ValueError("source 仅支持 auto / live / archive")
        max_days = MAX_ARCHIVE_RANGE_DAYS if mode == "archive" else MAX_LIVE_RANGE_DAYS
        start_str, end_str = self.resolve_date_range(
            oldest=oldest, newest=newest, days=days, default_days=7, max_days=max_days
        )

        effective_source = mode
        live_error = ""
        if mode in {"auto", "live"}:
            try:
                await self.sync_range(start_str, end_str, include_wellness=True, include_activities=False)
                effective_source = "live_and_archived"
            except Exception as exc:
                if mode == "live":
                    raise
                effective_source = "archive_fallback"
                live_error = sanitize_secret(str(exc), self.client._api_key)

        raw_rows = await asyncio.to_thread(self._read_wellness_archive_sync, start_str, end_str)
        records: list[dict[str, Any]] = []
        omitted_empty = 0
        for raw in raw_rows:
            cleaned, has_metrics = clean_wellness_record(raw, fields=fields)
            if has_metrics or include_empty_days:
                records.append(cleaned)
            else:
                omitted_empty += 1

        response: dict[str, Any] = {
            "source": effective_source,
            "range": {"oldest": start_str, "newest": end_str},
            "days_returned": len(records),
            "empty_days_omitted": omitted_empty,
            "summary": summarize_wellness_records(records),
            "records": records,
        }
        if live_error:
            response["live_error"] = live_error
        return response

    async def query_activities(
        self,
        *,
        oldest: Any = None,
        newest: Any = None,
        days: Any = None,
        source: str = "auto",
        activity_type: str = "",
        limit: Any = None,
    ) -> dict[str, Any]:
        mode = (source or "auto").strip().lower()
        if mode not in {"auto", "live", "archive"}:
            raise ValueError("source 仅支持 auto / live / archive")
        if limit in (None, ""):
            resolved_limit = DEFAULT_ACTIVITIES_LIMIT
        elif isinstance(limit, bool):
            raise ValueError("limit 必须为正整数")
        else:
            try:
                resolved_limit = int(limit)
            except (TypeError, ValueError):
                raise ValueError("limit 必须为正整数") from None
        if resolved_limit < 1 or resolved_limit > MAX_ACTIVITIES_LIMIT:
            raise ValueError(f"limit 须在 1 到 {MAX_ACTIVITIES_LIMIT} 之间")

        max_days = MAX_ARCHIVE_RANGE_DAYS if mode == "archive" else MAX_LIVE_RANGE_DAYS
        start_str, end_str = self.resolve_date_range(
            oldest=oldest, newest=newest, days=days, default_days=14, max_days=max_days
        )
        type_filter = str(activity_type or "").strip()

        effective_source = mode
        live_error = ""
        if mode in {"auto", "live"}:
            try:
                await self.sync_range(start_str, end_str, include_wellness=False, include_activities=True)
                effective_source = "live_and_archived"
            except Exception as exc:
                if mode == "live":
                    raise
                effective_source = "archive_fallback"
                live_error = sanitize_secret(str(exc), self.client._api_key)

        raw_rows = await asyncio.to_thread(
            self._read_activities_archive_sync,
            start_str,
            end_str,
            activity_type=type_filter,
            limit=resolved_limit,
        )
        activities = [clean_activity_record(raw, detailed=False) for raw in raw_rows]

        response: dict[str, Any] = {
            "source": effective_source,
            "range": {"oldest": start_str, "newest": end_str},
            "count": len(activities),
            "activities": activities,
        }
        if type_filter:
            response["activity_type"] = type_filter
        if live_error:
            response["live_error"] = live_error
        return response

    async def get_activity_detail(self, activity_id: str, *, source: str = "auto") -> dict[str, Any]:
        cleaned_id = str(activity_id or "").strip()
        if not ACTIVITY_ID_RE.fullmatch(cleaned_id):
            raise ValueError("activity_id 格式无效")
        mode = (source or "auto").strip().lower()
        if mode not in {"auto", "live", "archive"}:
            raise ValueError("source 仅支持 auto / live / archive")

        raw: dict[str, Any] | None = None
        effective_source = "archive"
        live_error = ""

        if mode in {"auto", "archive"}:
            raw = await asyncio.to_thread(self._read_single_activity_archive_sync, cleaned_id)

        if mode == "live" or (mode == "auto" and raw is None):
            try:
                fetched = await self.client.fetch_activity(cleaned_id)
                await asyncio.to_thread(self._upsert_activities_sync, [fetched], time.time())
                raw = fetched
                effective_source = "live_and_archived"
            except Exception as exc:
                if raw is None:
                    raise
                effective_source = "archive_fallback"
                live_error = sanitize_secret(str(exc), self.client._api_key)

        if raw is None:
            raise HealthServiceError(f"本地归档中未找到运动记录: {cleaned_id}")

        result: dict[str, Any] = {
            "source": effective_source,
            "activity": clean_activity_record(raw, detailed=True),
        }
        if live_error:
            result["live_error"] = live_error
        return result

    def _status_sync(self) -> dict[str, Any]:
        with self._db() as db:
            w_stats = db.execute(
                "SELECT COUNT(*) AS cnt, MIN(date) AS min_d, MAX(date) AS max_d FROM wellness_archive"
            ).fetchone()
            a_stats = db.execute(
                "SELECT COUNT(*) AS cnt, MIN(date) AS min_d, MAX(date) AS max_d FROM activity_archive"
            ).fetchone()
            meta_rows = db.execute("SELECT key, value FROM sync_meta").fetchall()
            recent_raw = db.execute(
                "SELECT raw_json FROM wellness_archive ORDER BY date DESC LIMIT 90"
            ).fetchall()

        available_fields: set[str] = set()
        for row in recent_raw:
            try:
                parsed = json.loads(row["raw_json"])
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            cleaned, _ = clean_wellness_record(parsed)
            for key in cleaned:
                if key not in {"date", "updated", "sleep_hours", "sleep_text"}:
                    available_fields.add(key)

        meta = {row["key"]: row["value"] for row in meta_rows}
        return {
            "provider": "intervals.icu",
            "readonly": True,
            "timezone": self.tz_name,
            "wellness_archive": {
                "total_days": int(w_stats["cnt"] or 0),
                "oldest_date": w_stats["min_d"],
                "newest_date": w_stats["max_d"],
                "active_fields_recent_90d": sorted(available_fields),
            },
            "activity_archive": {
                "total_activities": int(a_stats["cnt"] or 0),
                "oldest_date": a_stats["min_d"],
                "newest_date": a_stats["max_d"],
            },
            "last_sync_at": meta.get("last_sync_at") or None,
            "last_sync_range": meta.get("last_sync_range") or None,
            "last_error": meta.get("last_error") or None,
        }

    async def status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._status_sync)
