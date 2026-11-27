"""内置运动健康插件（只读 Intervals.icu）：实时查询 + SQLite 全量归档 + 读取期空值过滤。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .service import (
    HealthArchiveService,
    HealthServiceError,
    IntervalsClient,
    MAX_LIVE_RANGE_DAYS,
    sanitize_secret,
)


_INITIAL_SYNC_DELAY_SECONDS = 10.0


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _coerce_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def setup(ctx: Any) -> None:
    """注册只读 health 工具与可选的后台定期归档任务。"""
    cfg = ctx.config if isinstance(ctx.config, dict) else {}
    api_key_env = str(cfg.get("api_key_env") or "INTERVALS_API_KEY").strip() or "INTERVALS_API_KEY"
    api_key = (ctx.get_env(api_key_env) or "").strip()
    if not api_key:
        raise RuntimeError(f"缺少必要环境变量: {api_key_env}")

    athlete_id = str(cfg.get("athlete_id") or "0").strip() or "0"
    tz_name = str(cfg.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    proxy = str(cfg.get("proxy") or "").strip()
    timeout = _coerce_float(cfg.get("request_timeout_seconds"), 15.0, minimum=3.0, maximum=60.0)
    sync_interval_minutes = _coerce_int(cfg.get("sync_interval_minutes"), 60, minimum=0, maximum=1440)
    sync_lookback_days = _coerce_int(cfg.get("sync_lookback_days"), 7, minimum=1, maximum=90)

    client = IntervalsClient(
        api_key=api_key,
        athlete_id=athlete_id,
        timeout=timeout,
        proxy=proxy,
    )
    service = HealthArchiveService(
        ctx.data_path("health.db"),
        client,
        tz_name=tz_name,
    )
    stop_event = asyncio.Event()

    async def health_tool(args: dict[str, Any]) -> str:
        operation = str(args.get("operation") or "wellness").strip().lower()
        try:
            if operation == "wellness":
                raw_fields = args.get("fields")
                if raw_fields is not None and (
                    not isinstance(raw_fields, list) or not all(isinstance(x, str) for x in raw_fields)
                ):
                    raise ValueError("fields 必须为字符串数组")
                result = await service.query_wellness(
                    oldest=args.get("oldest"),
                    newest=args.get("newest"),
                    days=args.get("days"),
                    source=str(args.get("source") or "auto"),
                    fields=raw_fields,
                    include_empty_days=bool(args.get("include_empty_days", False)),
                )
            elif operation == "activities":
                result = await service.query_activities(
                    oldest=args.get("oldest"),
                    newest=args.get("newest"),
                    days=args.get("days"),
                    source=str(args.get("source") or "auto"),
                    activity_type=str(args.get("activity_type") or ""),
                    limit=args.get("limit"),
                )
            elif operation == "activity_detail":
                result = await service.get_activity_detail(
                    str(args.get("activity_id") or ""),
                    source=str(args.get("source") or "auto"),
                )
            elif operation == "sync":
                oldest_str, newest_str = service.resolve_date_range(
                    oldest=args.get("oldest"),
                    newest=args.get("newest"),
                    days=args.get("days"),
                    default_days=30,
                    max_days=MAX_LIVE_RANGE_DAYS,
                )
                result = await service.sync_range(oldest_str, newest_str)
            elif operation == "status":
                result = await service.status()
            else:
                raise ValueError(
                    f"不支持的操作: {operation}，请使用 wellness / activities / activity_detail / sync / status"
                )
            return json.dumps({"status": "ok", **result}, ensure_ascii=False)
        except (ValueError, HealthServiceError) as exc:
            return json.dumps(
                {"status": "error", "error": sanitize_secret(str(exc), api_key)},
                ensure_ascii=False,
            )

    ctx.register_tool(
        name="health",
        description=(
            "只读查询并归档运动健康数据（Intervals.icu，可承接华为运动健康等同步）："
            "wellness 查每日健康（步数、睡眠、静息心率、体重等，自动过滤空值并输出统计，默认实时拉取并归档，断网自动回退本地归档）；"
            "activities 查运动记录列表；activity_detail 按 activity_id 查单次运动详情；"
            "sync 手动同步并全量归档历史区间到本地数据库；status 查看归档范围、记录数及已同步字段。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["wellness", "activities", "activity_detail", "sync", "status"],
                    "description": (
                        "操作类型：wellness（每日健康与统计，默认）、activities（运动记录列表）、"
                        "activity_detail（单次运动详情）、sync（手动同步历史区间并归档）、status（查看本地归档状态）。"
                    ),
                },
                "days": {
                    "type": "integer",
                    "description": "向前查询/同步的天数（wellness 默认 7，activities 默认 14，sync 默认 30）；提供 oldest 时忽略。",
                },
                "oldest": {
                    "type": "string",
                    "description": "起始日期（含），YYYY-MM-DD 格式，例如 2026-11-01。",
                },
                "newest": {
                    "type": "string",
                    "description": "结束日期（含），YYYY-MM-DD 格式，默认本地今天。",
                },
                "source": {
                    "type": "string",
                    "enum": ["auto", "live", "archive"],
                    "description": (
                        "数据来源：auto（默认，实时拉取并写入本地归档，失败时自动回退本地归档）、"
                        "live（强制实时拉取并归档）、archive（仅查本地归档，离线可用且支持超长跨度）。"
                    ),
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "仅 wellness 有效：可选字段白名单（如 [\"steps\", \"sleepSecs\", \"restingHR\"]）；留空自动返回全部非空字段。",
                },
                "include_empty_days": {
                    "type": "boolean",
                    "description": "仅 wellness 有效：是否保留当天没有任何有效指标的空日期行（默认 false）。",
                },
                "activity_type": {
                    "type": "string",
                    "description": "仅 activities 有效：按运动类型筛选（如 Run、Walk、Ride）。",
                },
                "limit": {
                    "type": "integer",
                    "description": "仅 activities 有效：返回条数上限（默认 20，最大 100）。",
                },
                "activity_id": {
                    "type": "string",
                    "description": "仅 activity_detail 必填：运动记录 ID。",
                },
            },
            "required": ["operation"],
        },
        func=health_tool,
        is_async=True,
    )

    async def _background_sync_loop() -> None:
        interval_seconds = float(sync_interval_minutes * 60)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_INITIAL_SYNC_DELAY_SECONDS)
            return
        except TimeoutError:
            pass

        while not stop_event.is_set():
            try:
                oldest_str, newest_str = service.resolve_date_range(
                    days=sync_lookback_days,
                    default_days=sync_lookback_days,
                    max_days=MAX_LIVE_RANGE_DAYS,
                )
                result = await service.sync_range(oldest_str, newest_str)
                ctx.logger.debug(
                    "health 后台归档完成: range=%s..%s wellness=%d activities=%d",
                    oldest_str,
                    newest_str,
                    result["archived_wellness_days"],
                    result["archived_activities"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ctx.logger.warning("health 后台归档失败（将在下个周期重试）: %s", sanitize_secret(str(exc), api_key))

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                pass

    if sync_interval_minutes > 0:
        ctx.create_background_task(_background_sync_loop, name="health-archive-sync")

    ctx.health_service = service
    ctx.health_stop_event = stop_event


async def teardown(ctx: Any) -> None:
    stop_event = getattr(ctx, "health_stop_event", None)
    if stop_event is not None:
        stop_event.set()
