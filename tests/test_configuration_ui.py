"""Opt-in browser regression using intercepted requests and a temporary config store."""

import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from config.store import ConfigStore


def _fulfill_spa(route, static: Path, path: str) -> bool:
    relative = unquote(path.removeprefix("/static/")).lstrip("/")
    if path == "/":
        relative = "index.html"
    elif path == "/favicon.ico":
        relative = "favicon.svg"
    elif not path.startswith("/static/"):
        return False
    target = (static / relative).resolve()
    if not target.is_relative_to(static) or not target.is_file():
        return False
    route.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target)[0] or "application/octet-stream")
    return True


def _boot_payloads(path: str):
    if path == "/api/auth/state":
        return {"device_auth": False, "device_ok": True, "has_password": False}
    if path == "/api/settings":
        return {"agent_name": "Test", "show_reasoning": False, "theme": "dark"}
    if path == "/api/chat/commands":
        return {"commands": []}
    if path == "/api/chat/history":
        return {"messages": []}
    if path == "/api/chat/sync":
        return {"messages": [], "has_more": False, "instance_id": "synthetic-ui",
                "seq": 0, "rounds": [], "events": [], "finished": []}
    if path == "/api/subagents":
        return {"agents": [], "tasks": []}
    return None


@pytest.mark.skipif(os.getenv("TENTACLE_UI_TESTS") != "1", reason="opt-in browser checks")
def test_unassigned_role_can_select_and_save_model(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    root = Path(__file__).resolve().parents[1]
    static = root / "web/static"
    defaults = root / "config.defaults.yaml"
    (tmp_path / defaults.name).write_bytes(defaults.read_bytes())
    store = ConfigStore(tmp_path / "config.yaml")
    store.structure([
        {"op": "create", "kind": "providers", "id": "example", "value": {"type": "deepseek"}},
        {"op": "create", "kind": "models", "id": "example/fast", "value": {"provider": "example", "name": "synthetic-fast"}},
    ], store.read()[0], apply=True)

    def snapshot():
        return {**store.public(), "active_role": "chat", "tools": {}}

    def serve(route):
        request = route.request
        path = urlparse(request.url).path
        if _fulfill_spa(route, static, path):
            return
        boot = _boot_payloads(path)
        if boot is not None:
            route.fulfill(json=boot)
        elif path == "/api/configuration" and request.method == "GET":
            route.fulfill(json=snapshot())
        elif path in {"/api/configuration/preview", "/api/configuration"}:
            payload = request.post_data_json
            operation = store.preview if path.endswith("/preview") else store.save
            result = operation(payload["changes"], payload["revision"])
            route.fulfill(json={**result, "status": snapshot()})
        else:
            route.fulfill(json={})

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(channel="chrome")
        try:
            page = browser.new_page()
            page.route("**/*", serve)
            page.goto("http://tentacle-ui.test/")
            page.locator('[data-panel-nav="config"]').click()
            search = page.get_by_placeholder("筛选供应商 / 模型 / 角色…")
            search.wait_for()
            playwright.expect(page.locator("[data-config-role]")).to_have_count(len(snapshot()["entities"]["roles"]))
            search.fill("chat")
            playwright.expect(page.locator("[data-config-role]")).to_have_count(1)
            page.locator('[data-config-role="chat"]').click()
            search.fill("")

            model = page.get_by_label("使用模型")
            playwright.expect(model).to_have_value("__null__")
            model.select_option("example/fast")
            page.locator('[data-testid="config-preview-btn"]').click()
            page.locator('[data-testid="config-save-btn"]').click()
            playwright.expect(page.locator('[data-testid="config-dirty-badge"]')).to_have_count(0)
            assert store.read()[2]["roles"]["chat"]["model"] == "example/fast"

            model.select_option("__null__")
            playwright.expect(page.locator('[data-testid="config-dirty-badge"]')).to_be_visible()
        finally:
            browser.close()


@pytest.mark.skipif(os.getenv("TENTACLE_UI_TESTS") != "1", reason="opt-in browser checks")
def test_maintenance_reconnects_and_reads_result_by_operation_id(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    root = Path(__file__).resolve().parents[1]
    static = root / "web/static"
    defaults = root / "config.defaults.yaml"
    (tmp_path / defaults.name).write_bytes(defaults.read_bytes())
    store = ConfigStore(tmp_path / "config.yaml")
    op = {"id": "a" * 32, "action": "update", "version": "v1.8", "state": "waiting"}
    requested = disconnected = False

    def serve(route):
        nonlocal requested, disconnected
        request = route.request
        url = urlparse(request.url)
        if _fulfill_spa(route, static, url.path):
            return
        boot = _boot_payloads(url.path)
        if boot is not None:
            route.fulfill(json=boot)
        elif url.path == "/api/status":
            route.fulfill(json={"agent_name": "Test", "connections": 1, "tokens": {"accumulated": 0}})
        elif url.path == "/api/configuration":
            route.fulfill(json={**store.public(), "active_role": "chat", "tools": {},
                                "restart": {"available": True, "state": "idle"}})
        elif url.path == "/api/maintenance" and request.method == "POST":
            assert request.post_data_json == {"action": "update", "version": "v1.8"}
            requested = True
            route.fulfill(status=202, json=op)
        elif url.path == "/api/maintenance":
            if requested and not disconnected:
                assert parse_qs(url.query)["operation_id"] == [op["id"]]
                disconnected = True
                route.abort("connectionreset")
                return
            route.fulfill(json={
                "available": True, "web_allowed": True, "can_update": not requested,
                "version": {"version": "v1.8" if requested else "v1.7"},
                "check": {"ok": True, "current": "v1.7", "latest": "v1.8"},
                "operation": {**op, "state": "succeeded"} if requested else None,
            })
        else:
            route.fulfill(json={})

    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(channel="chrome")
        try:
            page = browser.new_page()
            page.route("**/*", serve)
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto("http://tentacle-ui.test/")
            page.locator('[data-panel-nav="status"]').click()
            playwright.expect(page.locator('[data-testid="host-version"]')).to_have_text("v1.7")
            page.get_by_role("button", name="应用更新").click()
            playwright.expect(page.locator('[data-testid="host-operation"]')).to_contain_text("连接暂时断开，正在等待服务恢复…")
            playwright.expect(page.locator('[data-testid="host-operation"]')).to_contain_text("succeeded")
            playwright.expect(page.locator('[data-testid="host-version"]')).to_have_text("v1.8")
        finally:
            browser.close()
