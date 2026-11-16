"""程序时区与每日摘要使用同一日历；临时数据，不依赖宿主时区或外部服务。"""

import logging
import os
import time
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import runtime.timezone as app_timezone
from agent.session import AgentSession
from runtime.timezone import configure_timezone, day_boundaries, local_datetime, timezone_name
from agent.compressor import Compressor
from runtime.heartbeat import Heartbeat
from runtime.logging import ProgramTimeFormatter
from agent.protocol import LoopResult
from runtime.scheduler import compute_next_run
from agent.session_manager import SessionManager
from runtime.usage import UsageTracker
from web.settings import WebSettings


@pytest.fixture(autouse=True)
def isolate_timezone(monkeypatch):
    monkeypatch.setattr(app_timezone, "_timezone_name", "Asia/Shanghai")


def utc_stamp(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def test_program_timezone_does_not_mutate_server_timezone():
    before = os.environ.get("TZ"), time.tzname
    configure_timezone("America/New_York")
    assert local_datetime(utc_stamp("2026-10-22T18:00:00")).isoformat() == "2026-10-22T14:00:00-04:00"
    assert (os.environ.get("TZ"), time.tzname) == before


@pytest.mark.parametrize("zone,day,hours", [
    ("Asia/Shanghai", date(2026, 10, 23), 24),
    ("America/New_York", date(2026, 3, 8), 23),
    ("America/New_York", date(2026, 11, 1), 25),
])
def test_calendar_day_boundaries_include_dst(zone, day, hours):
    configure_timezone(zone)
    start, end = day_boundaries(day)
    assert end - start == pytest.approx(hours * 3600, abs=0.00001)
    assert local_datetime(start).date() == day
    assert local_datetime(end).date() == day


def test_messages_logs_public_settings_and_usage_share_program_timezone(tmp_path):
    configure_timezone("America/New_York")
    stamp = utc_stamp("2026-10-23T01:30:00")
    assert AgentSession._format_timestamp(stamp) == "[10.22-21:30]"
    assert Heartbeat._format_time(stamp) == "[10.22-21:30]"
    record = logging.LogRecord("synthetic", logging.INFO, "synthetic.py", 1, "synthetic", (), None)
    record.created = stamp
    assert ProgramTimeFormatter().formatTime(record, "%Y-%m-%d %H:%M") == "2026-10-22 21:30"
    assert WebSettings(str(tmp_path / "settings.json")).get_public()["timezone"] == "America/New_York"
    tracker = object.__new__(UsageTracker)
    points = tracker._aggregate([(stamp, 1, 1, 2, 0, 0, 0)], "day")
    assert points[0]["bucket"] == "2026-10-22"


def test_once_cron_and_explicit_offsets_use_program_timezone():
    configure_timezone("America/New_York")
    after = utc_stamp("2026-10-23T00:00:00")
    expected = utc_stamp("2026-10-23T13:00:00")
    assert compute_next_run("once", "2026-10-23 09:00", after) == expected
    assert compute_next_run("cron", "0 9 * * *", after) == expected
    assert compute_next_run("once", "2026-10-23T09:00:00+08:00", after) == utc_stamp("2026-10-23T01:00:00")
    with pytest.raises(ValueError, match="不存在"):
        compute_next_run("once", "2026-03-08 02:30", utc_stamp("2026-03-01T00:00:00"))


def test_heartbeat_window_uses_configured_timezone(tmp_path):
    hb = Heartbeat(None, state_file=tmp_path / 'runtime.heartbeat.json')
    session = SimpleNamespace(start_time=utc_stamp("2026-10-21T12:00:00"))
    stamp = utc_stamp("2026-10-23T05:00:00")
    assert not hb._session_switch_due(session, stamp)  # 上海 13:00
    configure_timezone("America/New_York")
    assert hb._session_switch_due(session, stamp)  # 纽约 01:00


async def test_daily_summary_uses_program_dates_and_persists_progress(tmp_path, monkeypatch):
    stamp = utc_stamp("2026-10-22T18:00:00")  # 上海已经 23 日凌晨
    monkeypatch.setattr(app_timezone, "time", SimpleNamespace(time=lambda: stamp))
    manager = object.__new__(SessionManager)
    manager._last_compressed_date = date(2026, 10, 21)
    manager._compression_state_saved = True
    manager.compression_state_path = tmp_path / "compression.json"
    queried = []
    manager.chronicle = SimpleNamespace(get_messages_by_time_range=lambda *bounds: queried.append(bounds) or [{"content": "synthetic"}])
    manager.compressor = SimpleNamespace(compress_date=AsyncMock(return_value=True))
    assert await manager.maybe_compress_daily()
    assert queried == [day_boundaries(date(2026, 10, 22))]
    assert manager.daily_compression_ready(stamp)
    manager.compressor.compress_date.assert_awaited_once()
    assert not await manager.maybe_compress_daily()
    assert timezone_name() in manager.compression_state_path.read_text()


async def test_empty_calendar_day_is_marked_complete_without_model(tmp_path, monkeypatch):
    stamp = utc_stamp("2026-10-22T18:00:00")
    monkeypatch.setattr(app_timezone, "time", SimpleNamespace(time=lambda: stamp))
    manager = object.__new__(SessionManager)
    manager._last_compressed_date = date(2026, 10, 21)
    manager._compression_state_saved = True
    manager.compression_state_path = tmp_path / "compression.json"
    manager.chronicle = SimpleNamespace(get_messages_by_time_range=lambda *bounds: [])
    manager.compressor = SimpleNamespace(compress_date=AsyncMock())
    assert not await manager.maybe_compress_daily()
    assert manager.daily_compression_ready(stamp)
    manager.compressor.compress_date.assert_not_awaited()


@pytest.mark.parametrize("reason", ["error", "yielded", "iteration_limit"])
async def test_incomplete_compressor_response_does_not_mark_summary_success(monkeypatch, reason):
    compressor = object.__new__(Compressor)
    compressor.manager = SimpleNamespace(get_model=lambda role: object())
    compressor._system_prompt = "synthetic"
    compressor.chronicle = SimpleNamespace(append_summaries=lambda summaries: pytest.fail("must not save"))
    monkeypatch.setattr('agent.compressor.AgentLoop', lambda **kw: SimpleNamespace(
        run=AsyncMock(return_value=LoopResult([], '[{"summary":"partial"}]', {}, stop_reason=reason)),
    ))
    assert not await compressor._compress_messages([{"content": "synthetic"}], 0, 1)
