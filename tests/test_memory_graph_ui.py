"""Opt-in browser checks for native Canvas2D MemoryGraphPanel. All requests are intercepted.

Run with TENTACLE_UI_TESTS=1 and a separately installed Playwright/Firefox.
TENTACLE_UI_ARTIFACTS optionally saves synthetic-data screenshots to that directory.
TENTACLE_UI_CHROMIUM optionally selects an installed Chromium executable instead.
"""

from copy import deepcopy
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

pytestmark = pytest.mark.skipif(os.getenv("TENTACLE_UI_TESTS") != "1", reason="opt-in browser checks")
STATIC = Path(__file__).resolve().parents[1] / "web" / "static"
GRAPH = {
    "nodes": [
        {"id": "Person:测试人物", "label": "Person", "name": "测试人物", "summary": "合成数据"},
        {"id": "Event:event_1", "label": "Event", "name": None, "summary": "测试散步", "content": "测试正文"},
        {"id": "Concept:星空", "label": "Concept", "name": "星空", "summary": "<img src=x onerror=alert(1)>"},
        {"id": "Location:公园", "label": "Location", "name": "公园"},
    ],
    "relationships": [
        {"source": "Person:测试人物", "target": "Event:event_1", "type": "PARTICIPATED_IN"},
        {"source": "Event:event_1", "target": "Concept:星空", "type": "MENTIONS", "properties": {"note": "测试"}},
        {"source": "Event:event_1", "target": "Location:公园", "type": "OCCURRED_AT"},
    ],
}


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        executable = os.getenv("TENTACLE_UI_CHROMIUM")
        instance = driver.chromium.launch(executable_path=executable) if executable else driver.firefox.launch()
        yield instance
        instance.close()


@pytest.fixture
def ui(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    context.add_cookies([{"name": "tentacle_device", "value": "test-device", "url": "http://tentacle-ui.test"}])
    context.add_init_script("""if (window === window.top) {
      localStorage.setItem('tentacle_token', 'test-token');
      window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } close() {} send() {} };
    }""")
    page = context.new_page()
    model = {
        "graph": deepcopy(GRAPH),
        "graph_status": 200,
        "ok": True,
        "settings_status": 200,
        "device_ok": True,
        "calls": [],
        "errors": [],
    }
    page.on("pageerror", lambda error: model["errors"].append(str(error)))

    def route(request):
        url = urlparse(request.request.url)
        model["calls"].append((url.path, parse_qs(url.query), request.request.headers))
        if url.path == "/api/settings":
            request.fulfill(status=model["settings_status"], json={"agent_name": "Test", "show_reasoning": False, "theme": "dark"})
        elif url.path == "/api/chat/history":
            request.fulfill(json={"messages": []})
        elif url.path == "/api/chat/commands":
            request.fulfill(json={"commands": []})
        elif url.path == "/api/subagents":
            request.fulfill(json={"agents": [], "tasks": []})
        elif url.path == "/api/chat/sync":
            request.fulfill(json={"messages": [], "has_more": False, "instance_id": "synthetic-graph-ui",
                                  "seq": 0, "rounds": [], "events": [], "finished": []})
        elif url.path == "/api/auth/state":
            request.fulfill(json={"device_auth": True, "device_ok": model["device_ok"], "has_password": True})
        elif url.path == "/api/auth/login":
            model["settings_status"] = 200
            request.fulfill(json={"token": "test-token"})
        elif url.path == "/api/memory/graph":
            graph = model["graph"]
            if model["ok"] and len(graph["nodes"]) <= 10000:
                labels = url.query and parse_qs(url.query).get("labels", [""])[0].split(",")
                if labels:
                    nodes = [node for node in graph["nodes"] if node["label"] in labels]
                    ids = {node["id"] for node in nodes}
                    graph = {"nodes": nodes, "relationships": [rel for rel in graph["relationships"]
                             if rel["source"] in ids and rel["target"] in ids]}
            request.fulfill(status=model["graph_status"], json={"ok": model["ok"], "data": graph})
        else:
            relative = unquote(url.path.removeprefix("/static/")).lstrip("/")
            if url.path in {"/", "/graph"}:
                relative = "index.html"
            elif url.path == "/favicon.ico":
                relative = "favicon.svg"
            target = (STATIC / relative).resolve()
            if not target.is_relative_to(STATIC) or not target.is_file():
                request.abort()
                pytest.fail(f"Unexpected request: {url.path}")
            request.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target)[0] or "text/plain")

    context.route("**/*", route)
    yield page, model
    assert not model["errors"]
    context.close()


def open_graph(page):
    page.goto("http://tentacle-ui.test/")
    page.locator('[data-testid="workbench-root"]').wait_for()
    menu = page.locator('[data-testid="mobile-menu-btn"]')
    if menu.is_visible():
        menu.click()
    page.locator('[data-panel-nav="graph"]').click()
    panel = page.locator('[data-testid="memory-graph-panel"]')
    panel.wait_for()
    return panel


def ready(panel, count=4):
    from playwright.sync_api import expect
    expect(panel.locator('[data-testid="memory-graph-refresh"]')).to_be_enabled()
    expect(panel.locator('[data-testid="memory-graph-count"]')).to_contain_text(f"{count} 节点")


def test_live_graph_search_theme_and_navigation(ui):
    from playwright.sync_api import expect
    page, model = ui
    panel = open_graph(page)
    ready(panel)
    graph_calls = [call for call in model["calls"] if call[0] == "/api/memory/graph"]
    assert len(graph_calls) == 1
    assert graph_calls[0][2]["authorization"] == "Bearer test-token"
    assert "tentacle_device=test-device" in graph_calls[0][2]["cookie"]

    panel.locator('[data-testid="memory-graph-search"]').fill("散步")
    panel.locator('[data-testid="memory-graph-search"]').press("Enter")
    inspector = panel.locator('[data-testid="memory-graph-inspector"]')
    expect(inspector).to_contain_text("测试散步")
    expect(inspector).to_contain_text("相邻节点 (3)")
    panel.get_by_role("button", name="关闭检查器").click()
    expect(inspector).to_have_count(0)

    page.get_by_role("button", name="切换深浅主题").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.locator('[data-panel-nav="chat"]').click()
    expect(page.locator('[data-testid="workbench-root"]')).to_have_attribute("data-primary-panel", "chat")
    page.locator('[data-panel-nav="graph"]').click()
    ready(panel)

    panel.get_by_role("button", name="适配画布").click()
    panel.get_by_role("button", name="放大").click()
    assert panel.locator('[data-testid="memory-graph-canvas"]').is_visible()


def test_filter_import_export_and_escaped_details(ui, tmp_path):
    from playwright.sync_api import expect
    page, _ = ui
    panel = open_graph(page)
    ready(panel)

    location_chip = panel.locator('[data-filter-type="Location"]')
    location_chip.click()
    expect(location_chip).to_have_attribute("aria-pressed", "false")

    panel.locator('[data-testid="memory-graph-file-input"]').set_input_files({
        "name": "local.json",
        "mimeType": "application/json",
        "buffer": json.dumps(GRAPH).encode(),
    })
    ready(panel)
    expect(panel.locator('[data-testid="memory-graph-source"]')).to_have_text("local.json")

    panel.locator('[data-testid="memory-graph-search"]').fill("星空")
    panel.locator('[data-testid="memory-graph-search"]').press("Enter")
    inspector = panel.locator('[data-testid="memory-graph-inspector"]')
    expect(inspector).to_contain_text("<img src=x onerror=alert(1)>")
    assert inspector.locator("img").count() == 0

    with page.expect_download() as download:
        panel.locator('[data-testid="memory-graph-export"]').click()
    path = tmp_path / "export.json"
    download.value.save_as(path)
    exported = json.loads(path.read_text(encoding="utf-8"))
    assert len(exported["nodes"]) == 4 and exported["relationships"] == GRAPH["relationships"]
    assert "test-token" not in path.read_text(encoding="utf-8")

    panel.locator('[data-testid="memory-graph-refresh"]').click()
    ready(panel)
    expect(panel.locator('[data-testid="memory-graph-source"]')).to_have_text("记忆库实时视图")


def test_failures_preserve_graph_and_auth_expiry(ui):
    from playwright.sync_api import expect
    page, model = ui
    panel = open_graph(page)
    ready(panel)
    model["ok"] = False
    panel.locator('[data-testid="memory-graph-refresh"]').click()
    expect(page.get_by_text("记忆图谱暂不可用")).to_be_visible()
    ready(panel)

    model["graph_status"] = 401
    model["settings_status"] = 401
    model["device_ok"] = False
    panel.locator('[data-testid="memory-graph-refresh"]').click()
    expect(page.locator('[data-testid="auth-dialog"]')).to_be_visible()
    expect(page.locator('[data-testid="login-form"]')).to_be_visible()
    expect(page.locator('[data-testid="enroll-form"]')).to_be_visible()


def test_initial_auth_blocks_lazy_load(ui):
    from playwright.sync_api import expect
    page, model = ui
    model["settings_status"] = 401
    page.goto("http://tentacle-ui.test/")
    expect(page.locator('[data-testid="auth-dialog"]')).to_be_visible()
    assert not any(call[0] == "/api/memory/graph" for call in model["calls"])
    page.locator("#login-password").fill("test")
    page.locator('[data-testid="login-submit"]').click()
    page.locator('[data-panel-nav="graph"]').click()
    panel = page.locator('[data-testid="memory-graph-panel"]')
    ready(panel)


def grouped_graph():
    graph = deepcopy(GRAPH)
    graph["nodes"][2]["equivalence_group"] = "synthetic-group"
    graph["nodes"].append({
        "id": "Concept:concept_2",
        "label": "Concept",
        "name": "星空的另一称呼",
        "equivalence_group": "synthetic-group",
    })
    return graph


@pytest.mark.parametrize("width", [1440, 390])
def test_concept_group_navigation_detach_and_local_import_is_read_only(ui, width):
    from playwright.sync_api import expect
    page, model = ui
    page.set_viewport_size({"width": width, "height": 900})
    model["graph"] = grouped_graph()
    writes = []

    def detach(route):
        writes.append(route.request.post_data_json)
        assert route.request.headers["authorization"] == "Bearer test-token"
        model["graph"]["nodes"][2]["equivalence_group"] = "isolated-group"
        route.fulfill(json={"ok": True, "concept_id": "星空", "group_id": "isolated-group"})

    page.route("**/api/memory/concepts/detach", detach)
    panel = open_graph(page)
    ready(panel, 5)
    panel.locator('[data-testid="memory-graph-search"]').fill("星空")
    panel.locator('[data-testid="memory-graph-search"]').press("Enter")
    inspector = panel.locator('[data-testid="memory-graph-inspector"]')
    expect(inspector).to_contain_text("synthetic-group")

    page.once("dialog", lambda dialog: dialog.dismiss())
    panel.locator('[data-testid="memory-graph-detach"]').click()
    assert not writes

    page.once("dialog", lambda dialog: dialog.accept())
    panel.locator('[data-testid="memory-graph-detach"]').click()
    expect(page.get_by_text("已将该概念从合并组")).to_be_visible()
    assert writes == [{"concept_id": "Concept:星空"}]

    # Local import is read-only: detach control is hidden even with an equivalence group.
    panel.locator('[data-testid="memory-graph-file-input"]').set_input_files({
        "name": "local.json",
        "mimeType": "application/json",
        "buffer": json.dumps(grouped_graph()).encode(),
    })
    ready(panel, 5)
    panel.locator('[data-testid="memory-graph-search"]').fill("星空")
    panel.locator('[data-testid="memory-graph-search"]').press("Enter")
    expect(panel.locator('[data-testid="memory-graph-detach"]')).to_have_count(0)
    assert len(writes) == 1


def test_concept_detach_lost_reply_requires_refresh_and_never_retries(ui):
    from playwright.sync_api import expect
    page, model = ui
    model["graph"] = grouped_graph()
    writes = []

    def fail(route):
        writes.append(route.request.post_data_json)
        route.fulfill(status=503, json={"detail": "合成错误"})

    page.route("**/api/memory/concepts/detach", fail)
    panel = open_graph(page)
    ready(panel, 5)
    panel.locator('[data-testid="memory-graph-search"]').fill("星空")
    panel.locator('[data-testid="memory-graph-search"]').press("Enter")
    page.once("dialog", lambda dialog: dialog.accept())
    panel.locator('[data-testid="memory-graph-detach"]').click()
    expect(page.get_by_text("服务端拆分结果未确认")).to_be_visible()
    expect(panel.locator('[data-testid="memory-graph-detach"]')).to_be_disabled()
    assert len(writes) == 1

    panel.locator('[data-testid="memory-graph-refresh"]').click()
    ready(panel, 5)
    panel.locator('[data-testid="memory-graph-search"]').fill("星空")
    panel.locator('[data-testid="memory-graph-search"]').press("Enter")
    expect(panel.locator('[data-testid="memory-graph-detach"]')).to_be_enabled()
    assert len(writes) == 1


def test_committed_detach_survives_failed_graph_refresh(ui):
    from playwright.sync_api import expect
    page, model = ui
    model["graph"] = grouped_graph()
    writes = []

    def detach(route):
        writes.append(route.request.post_data_json)
        model["graph_status"] = 503
        route.fulfill(json={"ok": True, "concept_id": "星空", "group_id": "isolated-group"})

    page.route("**/api/memory/concepts/detach", detach)
    panel = open_graph(page)
    ready(panel, 5)
    panel.locator('[data-testid="memory-graph-search"]').fill("星空")
    panel.locator('[data-testid="memory-graph-search"]').press("Enter")
    page.once("dialog", lambda dialog: dialog.accept())
    panel.locator('[data-testid="memory-graph-detach"]').click()
    expect(panel.locator('[data-testid="memory-graph-inspector"]')).to_contain_text("isolated-group")
    expect(panel.locator('[data-testid="memory-graph-refresh"]')).to_be_enabled()
    assert writes == [{"concept_id": "Concept:星空"}]


@pytest.mark.parametrize("width", [1440, 768, 640, 390, 320])
def test_responsive_graph(ui, width):
    from playwright.sync_api import expect
    page, _ = ui
    page.set_viewport_size({"width": width, "height": 820})
    panel = open_graph(page)
    ready(panel)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    bounds = panel.locator('[data-testid="memory-graph-canvas"]').bounding_box()
    assert bounds and bounds["width"] >= min(width - 80, 260) and bounds["height"] >= 400
    panel.locator('[data-testid="memory-graph-search"]').fill("星空")
    panel.locator('[data-testid="memory-graph-search"]').press("Enter")
    inspector = panel.locator('[data-testid="memory-graph-inspector"]')
    expect(inspector).to_be_visible()
    assert panel.get_by_role("button", name="关闭检查器").bounding_box()["x"] < width
    artifacts = os.getenv("TENTACLE_UI_ARTIFACTS")
    if artifacts:
        directory = Path(artifacts)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"memory-graph-{width}.png"))
