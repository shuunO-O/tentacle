"""Shared DSH session and browser gateway regressions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from subagents.dsh import DshError, DshRunner, _Turn
from web.coding import coding_router


def test_turn_projection_starts_at_our_request_and_stops_before_next_sender():
    turn = _Turn("ours", 4)
    events = [
        ("user/message", {"source": {"rpcId": "browser"}}),
        ("assistant/message", {"message": {"content": [{"type": "text", "text": "browser"}]}}),
        ("turn/end", {"reason": {"kind": "completed"}}),
        ("user/message", {"source": {"rpcId": "ours"}}),
        ("assistant/message", {"message": {"content": [{"type": "text", "text": "ours"}]},
                               "usage": {"inputTokens": 4, "outputTokens": 2}}),
        ("turn/end", {"reason": {"kind": "completed"}}),
        ("user/message", {"source": {"rpcId": "browser-next"}}),
    ]
    for kind, data in events:
        turn.observe({"type": kind, "data": data})
        if turn.reason:
            break
    assert turn.reason == "end_turn"
    assert turn.parts == ["ours"]
    assert turn.usage == {"prompt_tokens": 4, "completion_tokens": 2}


async def test_rpc_uses_native_api_and_keeps_dsh_request_id():
    async def host(request):
        assert request.method == "POST"
        assert request.url.path in {"/api/session/prompt", "/api/session/list", "/api/session/events"}
        packet = json.loads(request.content)
        assert packet["type"] == "client-request"
        if request.url.path == "/api/session/prompt":
            assert packet["method"] == "session/prompt"
            assert packet["payload"] == {"args": {"request": {"requestId": "stable"}}}
            return httpx.Response(200, json={
                "type": "server-response", "rpcId": packet["rpcId"],
                "result": {"ok": True, "value": {"accepted": True}},
            })
        if request.url.path == "/api/session/list":
            assert packet["method"] == "session/list"
            assert packet["payload"] == {"args": {"_request": {}}}
            return httpx.Response(200, json={
                "type": "server-response", "rpcId": packet["rpcId"],
                "result": {"ok": True, "value": {"items": []}},
            })
        boundary = "tentacle-boundary"
        meta = json.dumps({
            "type": "server-response", "rpcId": packet["rpcId"],
            "result": {"ok": True, "value": {"records": [], "hasMore": False}},
        }).encode()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="metadata"\r\n\r\n'.encode()
            + meta
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return httpx.Response(
            200,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            content=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(host), base_url="http://127.0.0.1") as client:
        runner = DshRunner(None)
        runner.client = AsyncMock(return_value=client)
        assert await runner.rpc("session/prompt", {"requestId": "stable"}) == {"accepted": True}
        assert await runner.rpc("session/list", {}) == {"items": []}
        assert await runner.rpc("session/events", {"address": {"kind": "session", "sessionId": "s1"}}) == {
            "records": [], "hasMore": False,
        }


async def test_rpc_rejects_gateway_failure_and_mismatched_rpc_id():
    mode = "error"

    async def host(request):
        packet = json.loads(request.content)
        if mode == "error":
            return httpx.Response(200, json={
                "type": "server-response", "rpcId": packet["rpcId"],
                "result": {"ok": False, "error": {"code": "session/not-found", "message": "missing", "details": {}}},
            })
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": "wrong-id",
            "result": {"ok": True, "value": {}},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(host), base_url="http://127.0.0.1") as client:
        runner = DshRunner(None)
        runner.client = AsyncMock(return_value=client)
        with pytest.raises(DshError):
            await runner.rpc("session/state", {"sessionId": "missing"})
        mode = "mismatch"
        with pytest.raises(DshError):
            await runner.rpc("session/state", {"sessionId": "s1"})


async def test_coding_access_requires_current_device_and_login(tmp_path):
    seen = []

    async def host(request):
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"DSH"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(host), base_url="http://127.0.0.1") as upstream:
        async def upstream_client():
            return upstream

        def device_ok(connection):
            return connection.cookies.get("device") == "bound"

        async def auth(request: Request):
            if not device_ok(request) or request.headers.get("authorization") != "Bearer synthetic":
                raise HTTPException(401)

        def resolve(path):
            if path != "project":
                raise ValueError
            return tmp_path / path

        settings = SimpleNamespace(has_password=True, valid_token=lambda token: token == "synthetic")
        manager = SimpleNamespace(
            config=SimpleNamespace(enabled=True, workspace_root=tmp_path),
            coding_available=True, runner=SimpleNamespace(client=upstream_client), resolve_workspace=resolve,
        )
        app = FastAPI()
        app.include_router(coding_router(manager, Depends(auth), settings, device_ok))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://tentacle.test") as client:
            access = "/api/subagents/dsh/access"
            headers = {"Authorization": "Bearer synthetic"}
            assert (await client.get("/coding/")).status_code == 401
            assert (await client.post(access, headers=headers)).status_code == 401
            client.cookies.set("device", "bound")
            assert (await client.post(access)).status_code == 401
            assert (await client.post(access, headers={**headers, "Origin": "https://foreign.test"})).status_code == 403
            assert (await client.post(access, headers=headers)).json() == {"url": "/coding/"}
            assert (await client.get("/coding/")).text == "DSH"
            assert "cookie" not in seen[-1].headers and "authorization" not in seen[-1].headers
            assert (await client.get("/coding/api/tentacle-bridge")).status_code == 404
            assert (await client.post("/coding/acp", json={"method": "initialize"})).status_code == 404
            assert (await client.get("/coding/acp/stream")).status_code == 404
            assert (await client.get("/coding/?token=anything")).status_code == 404

            packet = {"type": "client-request", "method": "workspace/create", "rpcId": "fixture",
                      "payload": {"args": {"request": {"path": "/outside"}}}}
            assert (await client.post("/coding/api/workspace/create", json=packet)).status_code == 400
            packet["payload"]["args"]["request"]["path"] = "project"
            assert (await client.post("/coding/api/workspace/create", json=packet)).status_code == 200
            assert json.loads(seen[-1].content)["payload"]["args"]["request"]["path"] == str(tmp_path / "project")
            client.cookies.delete("device")
            assert (await client.get("/coding/")).status_code == 401
