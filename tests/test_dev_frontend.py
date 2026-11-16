"""Development preview isolation and CDP routing; no network/browser/app startup."""

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dev.preview import ROOT, create_preview


def client(scenario="default", peer="127.0.0.1"):
    return TestClient(create_preview(scenario=scenario), client=(peer, 50000))


def test_preview_serves_exact_working_tree_and_never_serves_private_paths():
    assets = ROOT / "web/static/assets"
    js_files = sorted(assets.glob("*.js"))
    css_files = sorted(assets.glob("*.css"))
    assert js_files and css_files, "应先运行 npm --prefix frontend run build 生成 web/static 产物"
    js_rel = f"assets/{js_files[0].name}"
    css_rel = f"assets/{css_files[0].name}"
    with client() as c:
        for url, file in [("/", "index.html"), ("/graph", "index.html"),
                          (f"/static/{js_rel}?v=old", js_rel),
                          (f"/static/{css_rel}", css_rel),
                          ("/static/favicon.svg", "favicon.svg")]:
            response = c.get(url)
            assert response.content == (ROOT / "web/static" / file).read_bytes()
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-tentacle-preview"] == "synthetic"
        for path in ("/.env", "/config.yaml", "/data/chronicle.db", "/static/../.env"):
            assert c.get(path).status_code == 404
        assert c.post("/api/admin/backups/restore", json={}).status_code == 501
        assert c.post("/api/memory/dedup/start", json={}).status_code == 501
        assert c.get("/api/not-implemented").status_code == 404


def test_peer_gate_covers_static_api_and_websocket_ignoring_forwarded_headers():
    with client(peer="203.0.113.1") as c:
        for path in ("/", "/graph", "/static/favicon.svg", "/api/settings", "/__preview__/state"):
            assert c.get(path, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        with pytest.raises(WebSocketDisconnect) as error:
            with c.websocket_connect("/ws/chat"):
                pass
        assert error.value.code == 4403


def test_preview_stream_segments_and_settings_are_in_memory_and_instance_local():
    with client() as c, c.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "hello"
        response = c.post("/api/chat/send", json={"text": "synthetic", "request_id": "synthetic-request"}).json()
        assert response["ok"] is True
        events = [ws.receive_json() for _ in range(9)]
        assert [e["type"] for e in events] == [
            "thinking_delta", "message_delta", "message_delta", "message_seg_end",
            "message_delta", "message_delta", "usage", "final", "round_finished",
        ]
        assert events[-1]["data"] == response["round"]
        assert response["round"]["request_id"] == "synthetic-request"
        assert c.put("/api/settings", json={"show_reasoning": True}).json()["show_reasoning"] is True
        assert ws.receive_json()["type"] == "settings"
    with client() as c:
        assert c.get("/api/settings").json()["show_reasoning"] is False


async def test_preview_reconnect_sync_and_stop_use_only_the_existing_synthetic_round():
    app = create_preview(transport="polling")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        task = asyncio.create_task(c.post("/api/chat/send", json={"text": "synthetic", "request_id": "preview-stop"}))

        async def ready():
            while not any(e["type"] == "message_delta" for e in app.state.events):
                await asyncio.sleep(0)

        await asyncio.wait_for(ready(), 2)
        snapshot = (await c.get("/api/chat/sync")).json()
        assert len(snapshot["rounds"]) == 1
        round_id = snapshot["rounds"][0]["round_id"]
        assert any(e["type"] == "message_delta" for e in snapshot["events"])
        assert (await c.post("/api/chat/stop", json={"round_id": round_id})).json()["accepted"]
        result = (await asyncio.wait_for(task, 2)).json()
        assert result["round"]["status"] == "cancelled"
        finished = (await c.get("/api/chat/sync")).json()
        assert finished["rounds"] == finished["events"] == []
        assert finished["finished"][-1] == result["round"]
        reply = [m for m in finished["messages"] if m["round_id"] == round_id and m["role"] == "assistant"]
        assert len(reply) == 1 and reply[0]["interrupted"]


def test_preview_exposes_jev_roles_and_dedup_switch_through_configuration_api():
    with client() as c:
        current = c.get("/api/configuration").json()
        roles = {item["id"] for item in current["entities"]["roles"]}
        assert {"concept_alignment", "concept_dedup"} <= roles
        fields = {item["path"]: item for item in current["fields"]}
        assert "concept_alignment" in fields["memory.concept_alignment.model_role"]["choices"]
        saved = c.put("/api/configuration", json={"revision": current["revision"],
                      "changes": {"memory.concept_alignment.enabled": True}})
        assert saved.status_code == 200
        field = next(f for f in saved.json()["status"]["fields"] if f["path"] == "memory.concept_alignment.enabled")
        assert field["value"] is True and field["pending"] and field["effect"] == "restart"
        assert 'id="setAgentName"' not in c.get("/").text
        assert c.put("/api/settings", json={"agent_name": "obsolete-name"}).json()["agent_name"] != "obsolete-name"


def test_login_empty_and_error_scenarios():
    with client("login") as c:
        assert c.get("/api/settings").status_code == 401
        assert c.get("/api/auth/state").json()["has_password"] is True
        assert c.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        token = c.post("/api/auth/login", json={"password": "preview"}).json()["token"]
        c.headers["Authorization"] = f"Bearer {token}"
        assert c.get("/api/settings").status_code == 200
        with c.websocket_connect("/ws/chat?token=" + token) as ws:
            assert ws.receive_json()["type"] == "hello"
        assert c.post("/__preview__/scenario", json={"scenario": "empty"}).status_code == 200
        assert c.get("/api/chat/history").json() == {"messages": [], "has_more": False}
        assert c.post("/__preview__/scenario", json={"scenario": "error"}).status_code == 200
        assert c.post("/api/chat/send", json={"text": "test"}).status_code == 503
        assert c.get("/api/status").status_code == 503


def test_cdp_preview_injects_transport_only_in_html_and_delivers_same_event_sequence():
    js_files = sorted((ROOT / "web/static/assets").glob("*.js"))
    assert js_files
    js_rel = f"assets/{js_files[0].name}"
    with TestClient(create_preview(transport="polling"), client=("127.0.0.1", 50000)) as c:
        assert '/__preview__/websocket.js' in c.get("/").text
        assert '/__preview__/websocket.js' in c.get("/graph").text
        assert c.get(f"/static/{js_rel}").content == (ROOT / "web/static" / js_rel).read_bytes()
        initial = c.get("/__preview__/events").json()
        assert initial["events"][0]["type"] == "hello"
        c.post("/api/chat/send", json={"text": "synthetic"})
        events = c.get(f"/__preview__/events?since={initial['cursor']}").json()
        assert len(events["events"]) == 9
        assert events["events"][3]["type"] == "message_seg_end"
        assert events["events"][-1]["type"] == "round_finished"
        assert c.get(f"/__preview__/events?since={events['cursor']}").json()["events"] == []


def test_preview_supports_provider_model_role_configuration_tree():
    with TestClient(create_preview(), client=("127.0.0.1", 50000)) as c:
        cfg = c.get("/api/configuration").json()
        assert {item["id"] for item in cfg["entities"]["providers"]} == {"example", "typesafe"}
        assert {item["id"] for item in cfg["entities"]["models"]} == {"example/chat", "example/fast", "typesafe/jev"}
        assert {"chat", "worker", "concept_alignment", "concept_dedup"} <= {
            item["id"] for item in cfg["entities"]["roles"]
        }


def test_preview_exposes_synthetic_dsh_coding_entry():
    with TestClient(create_preview(), client=("127.0.0.1", 50000)) as c:
        listing = c.get("/api/subagents")
        assert listing.status_code == 200
        payload = listing.json()
        assert payload["coding_enabled"] is True

        sessions = c.get("/api/subagents/dsh/sessions")
        assert sessions.status_code == 200
        first_sid = sessions.json()["sessions"][0]["session_id"]

        attached = c.post("/api/subagents/dsh/attach", json={"session_id": first_sid})
        assert attached.status_code == 200
        assert attached.json()["status"] == "attached"
        assert attached.json()["dsh_session_id"] == first_sid

        denied = c.post("/api/subagents/dsh/access")
        assert denied.status_code == 503


def test_preview_exposes_synthetic_plugins_and_bundled_browser():
    with TestClient(create_preview(), client=("127.0.0.1", 50000)) as c:
        assert c.get("/plugins").status_code == 200
        snap = c.get("/api/plugins")
        assert snap.status_code == 200
        data = snap.json()
        browser = next(p for p in data["plugins"] if p["id"] == "browser")
        assert browser["bundled"] is True
        assert c.delete("/api/plugins/local/browser?delete_files=true").status_code == 422
        updated = c.post(
            "/api/plugins/local/browser/decision",
            json={"enabled": True, "config": {"allowed_domains": ["example.com"]}},
        )
        assert updated.status_code == 200
        browser_after = next(p for p in updated.json()["plugins"] if p["id"] == "browser")
        assert browser_after["saved_config"]["allowed_domains"] == ["example.com"]

