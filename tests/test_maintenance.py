"""Web authentication and status queries with a synthetic host."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import Depends, FastAPI, HTTPException, Request
import httpx
import pytest

from runtime.maintenance import MaintenanceController
from runtime.maintenance_protocol import OPERATIONS
from web.maintenance import maintenance_router


async def test_web_requires_existing_auth_and_starts_after_accepting():
    app = FastAPI()
    controller = Mock()
    settings = SimpleNamespace(has_password=False, requires_device=False)

    async def auth(request: Request):
        if settings.has_password and request.headers.get("Authorization") != "Bearer synthetic":
            raise HTTPException(401)

    app.include_router(maintenance_router(controller, Depends(auth), settings))
    operation = {"id": "e" * 32, "state": "waiting", "action": "update", "version": "v1.8"}
    controller.reserve.return_value = operation
    controller.snapshot.return_value = {"version": {"version": "v1.7"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://test") as client:
        status = await client.get("/api/maintenance")
        assert status.json() == {"version": {"version": "v1.7"}, "web_allowed": False}
        assert (await client.post("/api/maintenance", json={"action": "restart"})).status_code == 403
        settings.has_password = True
        assert (await client.post("/api/maintenance", json={"action": "restart"})).status_code == 401
        controller.reserve.assert_not_called()
        response = await client.post("/api/maintenance", json={"action": "update", "version": "v1.8"},
                                     headers={"Authorization": "Bearer synthetic"})
    assert response.status_code == 202 and response.json() == operation
    controller.reserve.assert_called_once_with("update", "v1.8")
    controller.start.assert_called_once()


def test_status_uses_persistent_operations_and_marks_a_dead_worker(tmp_path):
    op = {"id": "a" * 32, "action": "update", "state": "running", "version": "v1.8",
          "worker_pid": 1, "worker_identity": "different-boot"}
    earlier = {"id": "b" * 32, "action": "restart", "state": "succeeded", "version": None}
    (tmp_path / OPERATIONS).write_text(json.dumps([op, earlier]))
    controller = MaintenanceController(None, tmp_path)
    visible = controller.snapshot()["operation"]
    assert visible["id"] == op["id"] and visible["state"] == "interrupted"
    assert "worker_pid" not in visible and "worker_identity" not in visible
    assert controller.snapshot(earlier["id"])["operation"] == earlier


def test_stale_update_check_cannot_be_reserved(tmp_path):
    (tmp_path / "version.json").write_text('{"version":"v1.8"}')
    (tmp_path / "update-check.json").write_text(json.dumps({
        "ok": True, "available": True, "data_compatible": True, "current": "v1.7", "latest": "v1.8",
        "target": {"version": "v1.8"},
    }))
    controller = MaintenanceController(SimpleNamespace(_restarting=False), tmp_path,
                                       SimpleNamespace(is_socket=lambda: True))
    assert not controller.snapshot()["can_update"]
    with pytest.raises(ValueError, match="重新|请先"):
        controller.reserve("update", "v1.8")


def test_updater_incompatible_check_blocks_can_update_and_reserve(tmp_path):
    (tmp_path / "version.json").write_text('{"version":"v1.8"}')
    (tmp_path / "update-check.json").write_text(json.dumps({
        "ok": True,
        "available": True,
        "data_compatible": True,
        "updater_compatible": False,
        "current": "v1.8",
        "latest": "v1.9",
        "target": {"version": "v1.9"},
    }))
    controller = MaintenanceController(
        SimpleNamespace(_restarting=False),
        tmp_path,
        SimpleNamespace(is_socket=lambda: True),
    )
    assert not controller.snapshot()["can_update"]
    with pytest.raises(ValueError, match="重新|请先"):
        controller.reserve("update", "v1.9")
