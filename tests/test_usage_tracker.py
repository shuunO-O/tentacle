"""
UsageTracker 测试（tmp_path 临时库，不碰生产 data/）

覆盖：新库建表含新列、旧 schema 库启动自动迁移（ALTER ADD COLUMN 幂等）、
record_usage 新字段写入、聚合查询 points/summary 含 reasoning_tokens。
"""

import asyncio
import sqlite3
import time

import pytest

import runtime.usage as ut_mod
from runtime.timezone import local_datetime
from runtime.usage import UsageTracker


@pytest.fixture
def tracker(tmp_path):
    """指向临时库的 UsageTracker（单例重置保证隔离）"""
    ut_mod.UsageTracker._instance = None
    t = UsageTracker(db_path=str(tmp_path / "test_chronicle.db"))
    yield t
    ut_mod.UsageTracker._instance = None


def _columns(db_path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(usage_logs)")}


def test_fresh_schema_has_new_columns(tracker):
    cols = _columns(tracker.db_path)
    assert {"reasoning_tokens", "cache_write_tokens", "provider_id", "model_id", "role"} <= cols


def test_old_schema_migration(tmp_path):
    """旧库（无新列）打开后自动补列，旧数据可读、新列默认 0"""
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_time REAL NOT NULL,
                platform TEXT NOT NULL DEFAULT 'unknown',
                model TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO usage_logs (created_time, platform, model, prompt_tokens,"
            " completion_tokens, total_tokens, cached_tokens)"
            " VALUES (?, 'test', 'm', 10, 5, 15, 3)",
            (time.time(),),
        )

    ut_mod.UsageTracker._instance = None
    t = UsageTracker(db_path=str(db))
    assert {"reasoning_tokens", "cache_write_tokens", "provider_id", "model_id", "role"} <= _columns(db)

    result = asyncio.run(t.query_usage(granularity="day", window=1))
    today = result["summary"]["today"]
    assert today["prompt_tokens"] == 10
    assert today["cached_tokens"] == 3
    assert today["reasoning_tokens"] == 0
    assert today["requests"] == 1


async def test_record_usage_new_fields(tracker):
    await tracker.record_usage(
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
        cached_tokens=30, reasoning_tokens=17, cache_write_tokens=0,
        platform="test", model="m1",
    )
    result = await tracker.query_usage(granularity="day", window=1)
    today = result["summary"]["today"]
    assert today["prompt_tokens"] == 100
    assert today["completion_tokens"] == 50
    assert today["cached_tokens"] == 30
    assert today["reasoning_tokens"] == 17
    assert today["cache_write_tokens"] == 0


async def test_aggregate_points_contain_new_fields(tracker):
    for pt, ct, tt, kct, rt in [(100, 50, 150, 30, 10), (200, 80, 280, 0, 20)]:
        await tracker.record_usage(
            prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            cached_tokens=kct, reasoning_tokens=rt,
            platform="test", model="m1",
        )
    result = await tracker.query_usage(granularity="hour", window=1)
    assert len(result["points"]) >= 1
    point = result["points"][-1]
    assert point["reasoning_tokens"] == 30
    assert point["cached_tokens"] == 30
    assert point["prompt_tokens"] == 300


async def test_summary_cache_rate_weighted_and_independent_of_chart_window(tracker):
    now = time.time()
    tracker._db_insert(now - 3 * 86400, 'test', 'gemini', 900, 10, 910, 810, 0, 0)
    tracker._db_insert(now, 'test', 'gemini', 100, 10, 110, 0, 0, 0)
    result = await tracker.query_usage(granularity='hour', window=1)
    assert sum(p['prompt_tokens'] for p in result['points']) == 100
    assert result['summary']['today']['cache_hit_rate'] == 0
    assert result['summary']['week']['cache_hit_rate'] == 81.0
    assert result['summary']['month']['cached_tokens'] == 810


async def test_empty_summary_cache_rate(tracker):
    result = await tracker.query_usage()
    assert result['summary']['today']['cache_hit_rate'] is None


async def test_dimension_filters_preserve_recorded_ownership(tracker):
    for provider, model_id, role, tokens in [
        ("account-a", "a-shared", "chat", 100),
        ("account-b", "b-shared", "chat", 200),
        ("account-a", "a-shared", "compress", 300),
    ]:
        await tracker.record_usage(tokens, 0, tokens, model="shared", provider_id=provider,
                                   model_id=model_id, role=role)
    config = {"providers": {"account-a": {"label": "我的账号"}},
              "models": {"a-shared": {"label": "常用模型"}},
              "roles": {"chat": {"label": "主对话", "model": "b-shared"}}}
    result = await tracker.query_usage(provider_id="account-a", role="chat", config=config)
    assert result["summary"]["today"]["total_tokens"] == 100
    assert sum(p["total_tokens"] for p in result["points"]) == 100
    assert {p["id"]: p["label"] for p in result["options"]["providers"]}["account-a"] == "我的账号"
    assert (await tracker.query_usage(model_id="b-shared"))["summary"]["today"]["total_tokens"] == 200
    assert (await tracker.query_usage(provider_id="account-b", role="compress"))["points"] == []
    assert (await tracker.query_usage())["summary"]["today"]["total_tokens"] == 600


async def test_legacy_dimensions_are_explicit_and_not_inferred(tracker):
    await tracker.record_usage(10, 5, 15, model="old-name")
    result = await tracker.query_usage(provider_id=":unknown", model_id="legacy:old-name", role=":unknown")
    assert result["summary"]["today"]["requests"] == 1
    assert result["options"]["providers"] == [{"id": ":unknown", "label": "未记录"}]
    assert result["options"]["models"][0]["label"] == "old-name（历史记录）"
    assert (await tracker.query_usage(provider_id="' OR 1=1 --"))["points"] == []


async def test_server_model_identity_keeps_slashes_and_versions(tracker):
    model_id = 'connection/vendor/snapshot.v1'
    await tracker.record_usage(10, 5, 15, model='vendor/snapshot.v1',
                               provider_id='connection', model_id=model_id, role='chat')
    result = await tracker.query_usage(provider_id='connection', model_id=model_id)
    assert result['summary']['today']['total_tokens'] == 15
    assert result['options']['models'] == [
        {'id': model_id, 'label': model_id, 'provider_ids': ['connection']}]


async def test_unused_and_reassigned_models_remain_selectable(tracker):
    await tracker.record_usage(10, 0, 10, provider_id="old", model_id="reassigned", role="chat")
    config = {"config_version": 2, "providers": {"new": {}},
              "models": {"reassigned": {"provider": "new", "name": "shared"},
                         "unused": {"provider": "new", "name": "unused"}},
              "roles": {"unused-role": {"model": "unused"}}}
    result = await tracker.query_usage(config=config)
    models = {item["id"]: item for item in result["options"]["models"]}
    assert set(models["reassigned"]["provider_ids"]) == {"old", "new"}
    assert models["unused"]["provider_ids"] == ["new"]
    assert (await tracker.query_usage(provider_id="new"))["summary"]["today"]["requests"] == 0


async def test_cumulative_usage_includes_older_history_with_same_filters(tracker):
    tracker._db_insert(time.time() - 60 * 86400, "test", "old", 100, 20, 120, 50, 0, 0,
                       "provider", "model", "chat")
    await tracker.record_usage(10, 2, 12, provider_id="provider", model_id="model", role="chat")
    await tracker.record_usage(500, 0, 500, provider_id="other", model_id="other", role="chat")
    result = await tracker.query_usage(granularity="hour", window=1, provider_id="provider", role="chat")
    assert result["summary"]["month"]["total_tokens"] == 12
    assert result["summary"]["all"]["total_tokens"] == 132
    assert result["summary"]["all"]["requests"] == 2
    assert result["summary"]["all"]["cache_hit_rate"] == 45.5
    assert (await tracker.query_usage(provider_id="missing"))["summary"]["all"]["requests"] == 0


async def test_balance_selection_and_cache_are_isolated_per_connection(tracker, monkeypatch):
    calls = []
    class Response:
        status = 200
        def __init__(self, key):
            self.key = key
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def json(self, **kwargs):
            return {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": self.key}]}
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == 10
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            calls.append((url, kwargs["headers"]["Authorization"]))
            return Response("10" if calls[-1][1] == "Bearer synthetic-a" else "20")
    monkeypatch.setattr(ut_mod.aiohttp, "ClientSession", Session)
    monkeypatch.setenv("TEST_BALANCE_A", "synthetic-a")
    monkeypatch.setenv("TEST_BALANCE_B", "synthetic-b")
    config = {"config_version": 2, "providers": {
        "a": {"type": "deepseek", "api_key_env": "TEST_BALANCE_A", "label": "账号 A"},
        "b": {"type": "openai_compatible", "api_key_env": "TEST_BALANCE_B",
              "base_url": "https://example.test/v1", "balance_mode": "deepseek",
              "balance_url": "https://billing.example.test/custom/credits?version=1"},
        "unsupported": {"type": "openai"},
    }}
    a = await tracker.get_provider_balances(config, "a")
    assert a["items"][0]["balances"][0]["total_balance"] == "10"
    both = await tracker.get_provider_balances(config)
    assert len(calls) == 2
    assert calls[1][0] == "https://billing.example.test/custom/credits?version=1"
    assert both["items"][1]["balances"][0]["total_balance"] == "20"
    assert both["items"][2]["ok"] is False
    await tracker.get_provider_balances(config, "a", force_refresh=True)
    assert len(calls) == 3
    monkeypatch.setenv("TEST_BALANCE_A", "synthetic-b")
    changed = await tracker.get_provider_balances(config, "a")
    assert changed["items"][0]["balances"][0]["total_balance"] == "20"
    assert len(calls) == 4
    config["providers"]["b"]["balance_mode"] = "none"
    disabled = await tracker.get_provider_balances(config, "b")
    assert "关闭" in disabled["items"][0]["error"] and len(calls) == 4
    assert (await tracker.get_provider_balances(config, ":unknown"))["items"][0]["ok"] is False
    assert len(calls) == 4


async def test_date_range_limits_chart_and_summary(tracker):
    now = time.time()
    tracker._db_insert(now - 10 * 86400, "test", "m", 100, 10, 110, 50, 0, 0)
    tracker._db_insert(now - 3 * 86400, "test", "m", 200, 20, 220, 0, 0, 0)
    start = local_datetime(now - 4 * 86400).date()
    end = local_datetime(now - 2 * 86400).date()

    result = await tracker.query_usage(start_date=start.isoformat(), end_date=end.isoformat())
    assert result["range"] == {"start_date": start.isoformat(), "end_date": end.isoformat(), "days": 3}
    assert len(result["points"]) == 1
    assert sum(p["prompt_tokens"] for p in result["points"]) == 200
    # 区间外（10 天前）的记录不进入所选范围，也不进入被区间裁剪的汇总
    assert result["summary"]["all"]["total_tokens"] == 220
    assert result["summary"]["all"]["cache_hit_rate"] == 0.0
    assert result["summary"]["today"]["requests"] == 0
    assert result["summary"]["week"]["total_tokens"] == 220
    # 未给日期时不返回 range，行为保持原样
    assert (await tracker.query_usage())["range"] is None


async def test_start_date_only_and_end_date_only(tracker):
    now = time.time()
    tracker._db_insert(now - 10 * 86400, "test", "m", 100, 0, 100, 0, 0, 0)
    tracker._db_insert(now - 1 * 86400, "test", "m", 200, 0, 200, 0, 0, 0)

    start = local_datetime(now - 2 * 86400).date()
    forward = await tracker.query_usage(start_date=start.isoformat())
    assert forward["range"]["end_date"] is None
    assert forward["range"]["days"] >= 2
    assert sum(p["total_tokens"] for p in forward["points"]) == 200

    end = local_datetime(now - 5 * 86400).date()
    backward = await tracker.query_usage(end_date=end.isoformat())
    assert backward["range"]["start_date"] is None
    assert backward["range"]["days"] is None
    assert sum(p["total_tokens"] for p in backward["points"]) == 100
    assert backward["summary"]["all"]["total_tokens"] == 100


async def test_invalid_date_filters_raise(tracker):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await tracker.query_usage(start_date="2026-13-01")
    with pytest.raises(ValueError, match="不能晚于"):
        await tracker.query_usage(start_date="2026-10-10", end_date="2026-10-01")
    with pytest.raises(ValueError, match="最多 92 天"):
        await tracker.query_usage(granularity="hour", start_date="2026-01-01", end_date="2026-10-01")
