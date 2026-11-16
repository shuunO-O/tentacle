"""统一 WebSocket 事件中心与离线 OpenAPI 导出测试（全部使用合成对象与临时目录）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from memory.concept_dedup import ConceptDedupManager
from runtime.maintenance import MaintenanceController
from scripts.export_openapi import build_openapi_schema
from subagents.manager import SubagentManager
from web.adapter import ConnectionManager
from web.events import WebEventHub


class FakeConn:
    def __init__(self, listeners: int = 1):
        self.listeners = listeners
        self.events: list[dict] = []

    def count(self) -> int:
        return self.listeners

    async def broadcast(self, payload: dict) -> None:
        self.events.append(payload)


def test_export_openapi_schema_covers_rest_and_ws_models():
    schema = build_openapi_schema()
    paths = schema["paths"]
    assert "/api/auth/state" in paths
    assert "/api/chat/send" in paths
    assert "/api/memory/graph" in paths
    assert "/api/configuration" in paths
    assert "/api/plugins" in paths
    assert "/api/subagents" in paths
    assert "/api/maintenance" in paths
    assert "/api/harmony/register" not in paths  # 已整体迁入内置 harmony 插件 (/api/plugins/ext/harmony/*)
    assert "/api/neo4j/graph" not in paths  # deprecated aliases stay hidden from generated schema
    schemas = schema["components"]["schemas"]
    assert {"WsEventEnvelope", "WsSubagentUpdatedData", "WsDedupProgressData", "WsMaintenanceUpdatedData"} <= set(schemas)


async def test_event_hub_pushes_domain_events_only_with_active_listeners(tmp_path):
    conn = FakeConn(listeners=0)
    hub = WebEventHub(conn, settings=SimpleNamespace(has_password=True, requires_device=False))

    # 无连接时不应触发广播
    await hub.broadcast("status.snapshot", {"connections": 0})
    assert conn.events == []

    conn.listeners = 1
    (tmp_path / "version.json").write_text('{"version":"v1.7"}', encoding="utf-8")
    controller = MaintenanceController(
        SimpleNamespace(_restarting=False),
        tmp_path,
        SimpleNamespace(is_socket=lambda: True),
    )
    hub.attach_maintenance(controller)
    controller.reserve("check")
    await asyncio.sleep(0.02)
    assert any(item["type"] == "maintenance.updated" and item["data"]["web_allowed"] is True for item in conn.events)
    await hub.stop()


async def test_subagent_and_dedup_observers_debounce_and_flush(tmp_path):
    conn = FakeConn(listeners=1)
    manager = SubagentManager(SimpleNamespace(enabled=True, db_path=tmp_path / "sub.db", max_concurrency=1))
    manager.presentation = AsyncMock(return_value={"enabled": True, "tasks": [{"id": "a" * 32, "status": "running"}]})
    dedup = ConceptDedupManager(tmp_path / "dedup.db")

    hub = WebEventHub(conn, subagent_manager=manager)
    hub.attach_subagents(manager)
    hub.attach_dedup(dedup)

    # 突发子代理事件应在防抖窗口内合并
    manager._emit("turn_running", task_id="a" * 32, turn_id=1)
    manager._emit("turn_finished", task_id="a" * 32, turn_id=1, status="completed")
    await asyncio.sleep(0.15)
    subagent_events = [e for e in conn.events if e["type"] == "subagent.updated"]
    assert len(subagent_events) == 1
    assert subagent_events[0]["data"]["tasks"][0]["status"] == "running"

    # 去重进度防抖，终态立即发出并取消前导防抖
    dedup._emit("scan_progress", run_id="r1", run={"id": "r1", "checked": 1})
    dedup._emit("scan_finished", run_id="r1", run={"id": "r1", "status": "completed"})
    await asyncio.sleep(0.05)
    dedup_events = [e for e in conn.events if e["type"] == "dedup.progress"]
    assert len(dedup_events) == 1
    assert dedup_events[0]["data"]["event"] == "scan_finished"
    await hub.stop()


async def test_status_loop_emits_only_on_signature_change():
    conn = FakeConn(listeners=1)
    tick = {"tokens": 100}
    hub = WebEventHub(
        conn,
        status_provider=lambda: {
            "session": {"session_id": "s1"},
            "tokens": {"accumulated": tick["tokens"]},
            "usage": {"total": 1},
            "heartbeat": {"next_beat_at": 10},
            "connections": 1,
            "time": 123.0,
        },
    )
    hub.start_status_loop(interval_seconds=0.02)
    await asyncio.sleep(0.07)
    status_events = [e for e in conn.events if e["type"] == "status.snapshot"]
    assert len(status_events) == 1
    tick["tokens"] = 200
    await asyncio.sleep(0.05)
    status_events = [e for e in conn.events if e["type"] == "status.snapshot"]
    assert len(status_events) == 2
    await hub.stop()


async def test_connection_manager_placeholder():
    assert ConnectionManager().count() == 0
