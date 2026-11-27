"""内置 health（运动健康 / Intervals.icu 只读）插件测试：空值过滤、全量归档、离线回退、安全边界与 PluginManager 集成。"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from agent.commands import CommandRegistry
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from plugins.manifest import bundled_plugins_dir, parse_manifest
from runtime.tool_registry import ToolRegistry


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "bundled" / "health"


def _load_service_module():
    spec = importlib.util.spec_from_file_location("test_health_service", PLUGIN_DIR / "service.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_registry() -> ToolRegistry:
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    return reg


def _noisy_wellness(day: str, **overrides) -> dict:
    base = {
        "id": day,
        "ctl": None,
        "atl": None,
        "rampRate": None,
        "ctlLoad": None,
        "atlLoad": None,
        "sportInfo": None,
        "updated": "2026-11-27T06:59:08.802+00:00",
        "weight": None,
        "restingHR": None,
        "hrv": None,
        "hrvSDNN": None,
        "menstrualPhase": None,
        "menstrualPhasePredicted": None,
        "kcalConsumed": None,
        "sleepSecs": None,
        "sleepScore": None,
        "sleepQuality": None,
        "avgSleepingHR": None,
        "soreness": None,
        "fatigue": None,
        "stress": None,
        "mood": None,
        "motivation": None,
        "injury": None,
        "spO2": None,
        "systolic": None,
        "diastolic": None,
        "hydration": None,
        "hydrationVolume": None,
        "readiness": None,
        "baevskySI": None,
        "bloodGlucose": None,
        "lactate": None,
        "bodyFat": None,
        "abdomen": None,
        "vo2max": None,
        "comments": None,
        "steps": None,
        "floorsClimbed": None,
        "respiration": None,
        "carbohydrates": None,
        "protein": None,
        "fatTotal": None,
        "locked": None,
        "tempWeight": False,
        "tempRestingHR": False,
    }
    base.update(overrides)
    return base


def _sample_activity(act_id: str, day: str, act_type: str = "Run", **overrides) -> dict:
    base = {
        "id": act_id,
        "start_date_local": f"{day}T07:30:00",
        "type": act_type,
        "name": f"Morning {act_type}\nInjected",
        "description": None,
        "moving_time": 1845,
        "elapsed_time": 1900,
        "distance": 5230.0,
        "total_elevation_gain": 42.5,
        "average_heartrate": 148.0,
        "max_heartrate": 172.0,
        "average_speed": 2.83,
        "calories": 340,
        "icu_training_load": 48,
        "icu_intensity": 82.5,
        "perceived_exertion": None,
        "feel": None,
        "source": "HUAWEI",
        "device_name": "Demo Watch",
        "huge_stream": list(range(50)),
        "extra_note": "clean scalar",
        "empty_field": None,
    }
    base.update(overrides)
    return base


def test_manifest_matches_bundled_contract():
    manifest = parse_manifest(PLUGIN_DIR)
    assert manifest.id == "health"
    assert manifest.name == "运动健康"
    assert manifest.entrypoint == "plugin.py:setup"
    assert set(manifest.permissions) == {
        "tools:register",
        "fs:plugin_data",
        "network:outbound",
        "background:task",
    }
    assert manifest.config_schema["api_key_env"].type == "env"
    assert manifest.config_schema["api_key_env"].default == "INTERVALS_API_KEY"
    assert manifest.config_schema["api_key_env"].required is True
    assert manifest.config_schema["proxy"].type == "proxy"
    assert manifest.config_schema["sync_interval_minutes"].default == 60


def test_wellness_cleaning_and_summary_filters_nulls_and_supports_all_metrics():
    svc_mod = _load_service_module()

    r1, has1 = svc_mod.clean_wellness_record(_noisy_wellness("2026-11-25", steps=5000))
    assert has1 is True
    assert r1 == {
        "date": "2026-11-25",
        "updated": "2026-11-27T06:59:08.802+00:00",
        "steps": 5000,
    }

    r2, has2 = svc_mod.clean_wellness_record(
        _noisy_wellness(
            "2026-11-26",
            steps=8000,
            sleepSecs=27000,
            sleepScore=86,
            restingHR=54,
            weight=68.4,
            spO2=98,
            comments="睡得不错\n状态良好",
        )
    )
    assert has2 is True
    assert r2["steps"] == 8000
    assert r2["sleepSecs"] == 27000
    assert r2["sleep_hours"] == 7.5
    assert r2["sleep_text"] == "7小时30分"
    assert r2["restingHR"] == 54
    assert r2["weight"] == 68.4
    assert r2["spO2"] == 98
    assert r2["comments"] == "睡得不错 状态良好"
    assert "tempWeight" not in r2
    assert "hrv" not in r2

    # 全空记录识别为无有效指标
    r_empty, has_empty = svc_mod.clean_wellness_record(_noisy_wellness("2026-11-24"))
    assert has_empty is False
    assert r_empty == {"date": "2026-11-24", "updated": "2026-11-27T06:59:08.802+00:00"}

    # 字段白名单过滤
    r_filtered, has_filtered = svc_mod.clean_wellness_record(
        _noisy_wellness("2026-11-26", steps=8000, restingHR=54),
        fields=["steps"],
    )
    assert has_filtered is True
    assert set(r_filtered) == {"date", "updated", "steps"}

    # 区间汇总自动覆盖所有出现的数值指标
    summary = svc_mod.summarize_wellness_records([r1, r2])
    assert summary["steps"] == {"days": 2, "avg": 6500.0, "min": 5000, "max": 8000, "total": 13000}
    assert summary["sleepSecs"]["avg_hours"] == 7.5
    assert summary["sleepSecs"]["avg_text"] == "7小时30分"
    assert summary["restingHR"] == {"days": 1, "avg": 54.0, "min": 54, "max": 54}
    assert summary["weight"] == {"days": 1, "avg": 68.4, "min": 68.4, "max": 68.4}


def test_activity_cleaning_compact_vs_detailed():
    svc_mod = _load_service_module()
    raw = _sample_activity("i999", "2026-11-26", "Run")

    compact = svc_mod.clean_activity_record(raw, detailed=False)
    assert compact["id"] == "i999"
    assert compact["name"] == "Morning Run Injected"
    assert compact["distance_km"] == 5.23
    assert compact["moving_time_text"] == "30分45秒"
    assert "description" not in compact
    assert "huge_stream" not in compact
    assert "extra_note" not in compact

    detailed = svc_mod.clean_activity_record(raw, detailed=True)
    assert detailed["extra_note"] == "clean scalar"
    assert "huge_stream" not in detailed
    assert "empty_field" not in detailed


async def test_live_archive_fallback_and_secret_sanitization(tmp_path):
    svc_mod = _load_service_module()
    secret_key = "fake-intervals-secret-998877"
    requests_seen: list[tuple[str, str, dict[str, str]]] = []
    fail_network = {"flag": False, "status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        expected_auth = "Basic " + base64.b64encode(f"API_KEY:{secret_key}".encode()).decode()
        assert request.headers.get("Authorization") == expected_auth
        requests_seen.append((request.method, request.url.path, dict(request.url.params)))

        if fail_network["flag"]:
            raise httpx.ConnectError(f"cannot reach host with secret {secret_key}")
        if fail_network["status"] != 200:
            return httpx.Response(fail_network["status"], text="error")

        if request.url.path.endswith("/wellness"):
            return httpx.Response(
                200,
                json=[
                    _noisy_wellness("2026-11-25"),
                    _noisy_wellness("2026-11-26", steps=6400, sleepSecs=25200, restingHR=56),
                    _noisy_wellness("2026-11-27", steps=4100),
                ],
            )
        if request.url.path.endswith("/activities"):
            return httpx.Response(
                200,
                json=[
                    _sample_activity("i101", "2026-11-26", "Run"),
                    _sample_activity("i102", "2026-11-27", "Walk", distance=2100.0, moving_time=1200),
                ],
            )
        if request.url.path.endswith("/activity/i202"):
            return httpx.Response(200, json=_sample_activity("i202", "2026-11-20", "Ride"))
        return httpx.Response(404, text="not found")

    client = svc_mod.IntervalsClient(
        api_key=secret_key,
        athlete_id="0",
        transport=httpx.MockTransport(handler),
    )
    db_path = tmp_path / "health.db"
    service = svc_mod.HealthArchiveService(db_path, client, tz_name="Asia/Shanghai")

    # 1. 实时拉取并归档 wellness（过滤空值与全空天）
    res = await service.query_wellness(oldest="2026-11-25", newest="2026-11-27", source="auto")
    assert res["source"] == "live_and_archived"
    assert res["days_returned"] == 2
    assert res["empty_days_omitted"] == 1
    assert [r["date"] for r in res["records"]] == ["2026-11-26", "2026-11-27"]
    assert res["summary"]["steps"]["total"] == 10500

    # 验证 SQLite 归档中保存的是全量原始 JSON（包含 null 字段和空日期行）
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT date, raw_json FROM wellness_archive ORDER BY date").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["2026-11-25", "2026-11-26", "2026-11-27"]
    raw_stored = json.loads(rows[1][1])
    assert "bloodGlucose" in raw_stored and raw_stored["bloodGlucose"] is None
    assert raw_stored["tempWeight"] is False

    # 2. 离线 archive 模式不发网络请求
    before_calls = len(requests_seen)
    arch = await service.query_wellness(
        oldest="2026-11-25", newest="2026-11-27", source="archive", include_empty_days=True
    )
    assert len(requests_seen) == before_calls
    assert arch["source"] == "archive"
    assert arch["days_returned"] == 3

    # 3. 实时拉取运动记录并支持按类型筛选
    acts = await service.query_activities(oldest="2026-11-25", newest="2026-11-27", source="auto")
    assert acts["source"] == "live_and_archived"
    assert acts["count"] == 2
    runs = await service.query_activities(
        oldest="2026-11-25", newest="2026-11-27", source="archive", activity_type="run"
    )
    assert runs["count"] == 1
    assert runs["activities"][0]["id"] == "i101"

    # 4. 单条运动详情：本地命中不发网络；本地缺失自动拉取并归档
    before_calls = len(requests_seen)
    d1 = await service.get_activity_detail("i101", source="auto")
    assert len(requests_seen) == before_calls
    assert d1["source"] == "archive"
    assert d1["activity"]["extra_note"] == "clean scalar"

    d2 = await service.get_activity_detail("i202", source="auto")
    assert d2["source"] == "live_and_archived"
    assert d2["activity"]["type"] == "Ride"

    # 5. 网络断开时 auto 优雅回退本地归档且脱敏 API Key；live 显式报错
    fail_network["flag"] = True
    fallback = await service.query_wellness(oldest="2026-11-25", newest="2026-11-27", source="auto")
    assert fallback["source"] == "archive_fallback"
    assert fallback["days_returned"] == 2
    assert secret_key not in fallback["live_error"]
    assert "***" in fallback["live_error"]

    with pytest.raises(svc_mod.HealthServiceError) as exc_info:
        await service.query_wellness(oldest="2026-11-25", newest="2026-11-27", source="live")
    assert secret_key not in str(exc_info.value)

    # 6. HTTP 401 返回明确鉴权错误
    fail_network["flag"] = False
    fail_network["status"] = 401
    with pytest.raises(svc_mod.HealthServiceError, match="HTTP 401"):
        await service.query_wellness(oldest="2026-11-25", newest="2026-11-27", source="live")

    # 7. status 返回归档覆盖与已出现指标清单
    st = await service.status()
    assert st["readonly"] is True
    assert st["wellness_archive"]["total_days"] == 3
    assert st["wellness_archive"]["oldest_date"] == "2026-11-25"
    assert st["wellness_archive"]["newest_date"] == "2026-11-27"
    assert st["wellness_archive"]["active_fields_recent_90d"] == ["restingHR", "sleepSecs", "steps"]
    assert st["activity_archive"]["total_activities"] == 3
    assert secret_key not in (st["last_error"] or "")


def test_validation_rejects_bad_ids_and_dates(tmp_path):
    svc_mod = _load_service_module()
    with pytest.raises(ValueError, match="athlete_id"):
        svc_mod.IntervalsClient(api_key="demo-key", athlete_id="../admin")
    with pytest.raises(ValueError, match="无效时区"):
        svc_mod.validate_timezone("Mars/Olympus")

    client = svc_mod.IntervalsClient(api_key="demo-key", athlete_id="i12345")
    service = svc_mod.HealthArchiveService(tmp_path / "h.db", client)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        service.resolve_date_range(oldest="2026/11/01", newest="2026-11-27")
    with pytest.raises(ValueError, match="不能晚于"):
        service.resolve_date_range(oldest="2026-11-28", newest="2026-11-27")
    with pytest.raises(ValueError, match="不能超过"):
        service.resolve_date_range(oldest="2020-01-01", newest="2026-11-27", max_days=366)


async def test_bundled_health_plugin_loads_via_plugin_manager(tmp_path, monkeypatch):
    # 1. 缺少环境变量时优雅降级
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    reg = _fresh_registry()
    pm = PluginManager(
        plugins_dir=tmp_path / "plugins",
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
        bundled_dir=bundled_plugins_dir(),
    )
    try:
        states = await pm.startup({
            "plugins": {
                "items": {
                    "health": {
                        "enabled": True,
                        "trusted": True,
                        "config": {"sync_interval_minutes": 0},
                    }
                }
            }
        })
        assert states["health"].bundled is True
        assert states["health"].status == "degraded"
        assert states["health"].missing_env == ["INTERVALS_API_KEY"]
        assert not reg.has("p_health__health")
    finally:
        await pm.shutdown()

    # 2. 配置环境变量后正常激活、工具可用，并验证后台任务启停
    monkeypatch.setenv("INTERVALS_API_KEY", "test-intervals-key-123456")
    pm2 = PluginManager(
        plugins_dir=tmp_path / "plugins",
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
        bundled_dir=bundled_plugins_dir(),
    )
    try:
        states = await pm2.startup({
            "plugins": {
                "items": {
                    "health": {
                        "enabled": True,
                        "trusted": True,
                        "config": {"sync_interval_minutes": 30, "sync_lookback_days": 7},
                    }
                }
            }
        })
        st = states["health"]
        assert st.bundled is True
        assert st.status == "active"
        assert st.background_tasks == 1
        assert reg.has("p_health__health")
        assert reg._tools["p_health__health"]["is_async"] is True

        # 注入模拟 transport 测试工具端到端执行（不访问外网）
        service = pm2._active_runtimes["health"]["ctx"].health_service
        service.client._transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json=[_noisy_wellness("2026-11-27", steps=7200)]
                if req.url.path.endswith("/wellness")
                else [_sample_activity("i555", "2026-11-27", "Run")],
            )
        )

        synced = json.loads(await reg.execute("p_health__health", {"operation": "sync", "days": 3}))
        assert synced["status"] == "ok"
        assert synced["archived_wellness_days"] == 1
        assert synced["archived_activities"] == 1

        wellness = json.loads(
            await reg.execute(
                "p_health__health",
                {"operation": "wellness", "oldest": "2026-11-27", "newest": "2026-11-27", "source": "archive"},
            )
        )
        assert wellness["status"] == "ok"
        assert wellness["records"][0]["steps"] == 7200

        status_out = json.loads(await reg.execute("p_health__health", {"operation": "status"}))
        assert status_out["status"] == "ok"
        assert status_out["wellness_archive"]["total_days"] == 1

        bad_op = json.loads(await reg.execute("p_health__health", {"operation": "write"}))
        assert bad_op["status"] == "error"
        assert "不支持的操作" in bad_op["error"]

        # 稍微让出事件循环，确认后台任务稳定等待且未崩溃
        await asyncio.sleep(0.02)
        assert st.status == "active"
    finally:
        await pm2.shutdown()

    assert not reg.has("p_health__health")


async def test_background_sync_loop_archives_and_survives_transient_error(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVALS_API_KEY", "bg-secret-token-778899")
    reg = _fresh_registry()
    pm = PluginManager(
        plugins_dir=tmp_path / "plugins",
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
        bundled_dir=bundled_plugins_dir(),
    )
    plugin_mod_name = "tentacle_plugins.health.plugin"
    try:
        states = await pm.startup({
            "plugins": {
                "items": {
                    "health": {
                        "enabled": True,
                        "trusted": True,
                        "config": {"sync_interval_minutes": 1, "sync_lookback_days": 3},
                    }
                }
            }
        })
        st = states["health"]
        assert st.status == "active"
        assert st.background_tasks == 1
        assert plugin_mod_name in __import__("sys").modules
        await asyncio.sleep(0)
    finally:
        await pm.shutdown()

    # 测试后台同步异常不会让插件降级，且记录脱敏错误
    pm2 = PluginManager(
        plugins_dir=tmp_path / "plugins",
        data_dir=tmp_path / "data2",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
        bundled_dir=bundled_plugins_dir(),
    )
    try:
        states = await pm2.startup({
            "plugins": {
                "items": {
                    "health": {
                        "enabled": True,
                        "trusted": True,
                        "config": {"sync_interval_minutes": 0},
                    }
                }
            }
        })
        ctx = pm2._active_runtimes["health"]["ctx"]

        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("transient failure with bg-secret-token-778899")

        ctx.health_service.client._transport = httpx.MockTransport(failing_handler)
        with pytest.raises(Exception):
            await ctx.health_service.sync_range("2026-11-25", "2026-11-27")
        snap = await ctx.health_service.status()
        assert snap["last_error"] is not None
        assert "bg-secret-token-778899" not in snap["last_error"]
        assert "***" in snap["last_error"]
        assert states["health"].status == "active"
    finally:
        await pm2.shutdown()


def test_web_plugins_snapshot_exposes_health_env_slot(tmp_path, monkeypatch):
    from config.store import ConfigStore
    from web.plugins import _build_snapshot

    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("plugins:\n  items: {}\n  mcp: {}\n", encoding="utf-8")
    store = ConfigStore(cfg_path)
    pm = PluginManager(
        plugins_dir=tmp_path / "plugins",
        data_dir=tmp_path / "data",
        tool_registry=_fresh_registry(),
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
        bundled_dir=bundled_plugins_dir(),
    )
    snap = _build_snapshot(plugin_manager=pm, store=store)
    health_items = [item for item in snap["plugins"] if item["id"] == "health"]
    assert len(health_items) == 1
    item = health_items[0]
    assert item["bundled"] is True
    assert item["env_vars"] == ["INTERVALS_API_KEY"]
    assert item["missing_env"] == ["INTERVALS_API_KEY"]
    assert item["env_status"] == {"INTERVALS_API_KEY": False}

    monkeypatch.setenv("INTERVALS_API_KEY", "fake-key-present")
    snap2 = _build_snapshot(plugin_manager=pm, store=store)
    item2 = next(x for x in snap2["plugins"] if x["id"] == "health")
    assert item2["missing_env"] == []
    assert item2["env_status"] == {"INTERVALS_API_KEY": True}
