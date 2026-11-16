"""实际 ASGI 请求与广播回归测试；仅用临时设置和合成事件，不启动应用实例。"""

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


class Socket:
    """ASGI WebSocket 测试客户端，使用队列代替网络连接。"""

    def __init__(self, app, cookie="", token=""):
        self.input = asyncio.Queue()
        self.output = asyncio.Queue()
        scope = {
            "type": "websocket", "path": "/ws/chat", "raw_path": b"/ws/chat", "root_path": "",
            "scheme": "wss", "server": ("testserver", 443), "client": ("testclient", 123),
            "query_string": urlencode({"token": token}).encode(), "subprotocols": [],
            "headers": [(b"cookie", f"{DEVICE_COOKIE}={cookie}".encode())],
        }
        self.task = asyncio.create_task(app(scope, self.input.get, self.output.put))

    async def open(self):
        await self.input.put({"type": "websocket.connect"})
        assert (await self.next())["type"] == "websocket.accept"
        hello = await self.next()
        assert json.loads(hello["text"])["type"] == "hello"

    async def next(self):
        return await asyncio.wait_for(self.output.get(), timeout=2)

    async def close(self):
        await self.input.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, timeout=2)


@pytest.fixture
async def runtime(tmp_path):
    settings = WebSettings(str(tmp_path / "webui_settings.json"))
    manager = ConnectionManager()
    app = create_app(manager, None, None, None, None,
                     SimpleNamespace(db_path=tmp_path / "unused.db"), settings, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        yield SimpleNamespace(settings=settings, manager=manager, app=app, client=client, path=tmp_path)


def freeze(runtime):
    (runtime.path / "device_auth_freeze.json").write_text(json.dumps({"created": time.time()}))


def pending(runtime, code="synthetic-code"):
    (runtime.path / "device_enroll_pending.json").write_text(json.dumps({
        "code_sha256": hashlib.sha256(code.encode()).hexdigest(), "expires": time.time() + 900,
    }))
    return code


def bind(runtime):
    token = runtime.settings.create_device("synthetic device")
    runtime.client.cookies.set(DEVICE_COOKIE, token)
    return token


@pytest.mark.parametrize("invalidate", ["revoke", "freeze", "corrupt_freeze", "password", "clear_password", "expiry"])
async def test_websocket_stops_after_credentials_invalidated(runtime, monkeypatch, invalidate):
    s, client = runtime.settings, runtime.client
    cookie = bind(runtime)
    s.set_device_auth(True)
    token = ""
    if invalidate in {"clear_password", "expiry"}:
        s.set_password("synthetic password")
        token = s.issue_token()
        client.headers["Authorization"] = "Bearer " + token
    ws = Socket(runtime.app, cookie, token)
    await ws.open()
    try:
        if invalidate == "revoke":
            response = await client.delete(f"/api/auth/devices/{hashlib.sha256(cookie.encode()).hexdigest()}")
            assert response.status_code == 200
        elif invalidate == "freeze":
            freeze(runtime)
        elif invalidate == "corrupt_freeze":
            (runtime.path / "device_auth_freeze.json").write_text("{")
        elif invalidate == "expiry":
            monkeypatch.setattr("web.settings.time.time", lambda: s._tokens[token] + 1)
        else:
            response = await client.post("/api/auth/set_password", json={
                "password": "" if invalidate == "clear_password" else "new synthetic password",
            })
            assert response.status_code == 200
        # 失效后不得发出合成消息；若是带外变更或 TTL 到期，广播前的复查也须生效。
        await runtime.manager.broadcast({"type": "message", "data": {"text": "MUST_NOT_BE_SENT"}})
        assert await ws.next() == {"type": "websocket.close", "code": 4401, "reason": ""}
        assert runtime.manager.count() == 0
        assert ws.output.empty()
    finally:
        await ws.close()


async def test_revocation_between_broadcast_scheduling_and_send(runtime, monkeypatch):
    cookie = bind(runtime)
    runtime.settings.set_device_auth(True)
    ws = Socket(runtime.app, cookie)
    await ws.open()
    original = asyncio.wait_for

    async def scheduled_wait(awaitable, timeout):
        # 在超时包装器进入发送协程前模拟一次调度切换。
        if timeout == 10:
            await asyncio.sleep(0)
        return await original(awaitable, timeout)

    monkeypatch.setattr("web.adapter.asyncio.wait_for", scheduled_wait)
    try:
        # 模拟广播已进入、发送协程尚未被调度时凭证发生变更。
        asyncio.get_running_loop().call_soon(
            runtime.settings.revoke_device, hashlib.sha256(cookie.encode()).hexdigest())
        await runtime.manager.broadcast({"type": "message", "data": {"text": "MUST_NOT_BE_SENT"}})
        assert (await ws.next())["code"] == 4401
        assert ws.output.empty()
    finally:
        await ws.close()


async def test_idle_socket_closes_after_out_of_band_freeze(runtime, monkeypatch):
    original = asyncio.wait_for

    async def fast_poll(awaitable, timeout):
        return await original(awaitable, timeout=0.01 if timeout == 5 else timeout)

    monkeypatch.setattr("web.server.asyncio.wait_for", fast_poll)
    cookie = bind(runtime)
    ws = Socket(runtime.app, cookie)
    await ws.open()
    freeze(runtime)
    assert (await ws.next())["code"] == 4401
    await ws.task
    assert runtime.manager.count() == 0


async def test_revoking_one_device_keeps_other_connection(runtime):
    s, client = runtime.settings, runtime.client
    first_cookie = bind(runtime)
    second_cookie = s.create_device("synthetic second device")
    s.set_device_auth(True)
    first, second = Socket(runtime.app, first_cookie), Socket(runtime.app, second_cookie)
    await first.open()
    await second.open()
    try:
        response = await client.delete(f"/api/auth/devices/{hashlib.sha256(first_cookie.encode()).hexdigest()}")
        assert response.status_code == 200
        assert (await first.next())["code"] == 4401
        payload = {"type": "message", "data": {"text": "synthetic allowed message"}}
        await runtime.manager.broadcast(payload)
        assert json.loads((await second.next())["text"]) == payload
        assert runtime.manager.count() == 1
    finally:
        await first.close()
        await second.close()


async def test_enabling_gate_disconnects_anonymous_socket(runtime):
    ws = Socket(runtime.app)
    await ws.open()
    try:
        code = pending(runtime)
        response = await runtime.client.put("/api/auth/device_auth", json={"enabled": True, "code": code})
        assert response.status_code == 200
        assert (await ws.next())["code"] == 4401
        assert runtime.manager.count() == 0
        assert (await runtime.client.get("/api/settings")).status_code == 200
    finally:
        await ws.close()


@pytest.mark.parametrize("password", [False, True])
async def test_enabling_without_cookie_always_requires_host_code(runtime, password):
    s, client = runtime.settings, runtime.client
    if password:
        s.set_password("synthetic password")
        client.headers["Authorization"] = "Bearer " + s.issue_token()
    response = await client.put("/api/auth/device_auth", json={"enabled": True})
    assert response.status_code == 403
    assert not s.list_devices()
    assert not s.device_auth
    assert "set-cookie" not in response.headers
    code = pending(runtime)
    response = await client.put("/api/auth/device_auth", json={"enabled": True, "code": code})
    assert response.status_code == 200
    assert len(s.list_devices()) == 1
    assert s.valid_device(client.cookies.get(DEVICE_COOKIE)) is not None
    assert (await client.get("/api/settings")).status_code == 200
    assert (await client.post("/api/auth/enroll", json={"code": code})).status_code == 403


async def test_cookie_cannot_mint_replacement_via_password(runtime):
    s, client = runtime.settings, runtime.client
    bind(runtime)
    s.set_device_auth(True)
    assert (await client.put("/api/auth/device_auth", json={"enabled": False})).status_code == 200
    assert (await client.post("/api/auth/set_password", json={"password": "synthetic password"})).status_code == 200
    login = await client.post("/api/auth/login", json={"password": "synthetic password"})
    client.headers["Authorization"] = "Bearer " + login.json()["token"]
    client.cookies.clear()
    assert (await client.put("/api/auth/device_auth", json={"enabled": True})).status_code == 403
    assert len(s.list_devices()) == 1


async def test_enabling_with_existing_cookie_does_not_register_device(runtime):
    cookie = bind(runtime)
    response = await runtime.client.put("/api/auth/device_auth", json={"enabled": True})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert runtime.client.cookies.get(DEVICE_COOKIE) == cookie
    assert len(runtime.settings.list_devices()) == 1


@pytest.mark.parametrize("gate", [False, True])
async def test_freeze_requires_new_host_binding_even_when_gate_off(runtime, gate):
    s, client = runtime.settings, runtime.client
    bind(runtime)
    s.set_device_auth(gate)
    freeze(runtime)
    for path in ["/api/settings", "/api/auth/devices", "/api/files/synthetic/content"]:
        assert (await client.get(path)).status_code == 401
    state = (await client.get("/api/auth/state")).json()
    assert state["freeze"] and state["device_auth"] and not state["device_ok"]
    client.cookies.clear()
    assert (await client.get("/api/settings")).status_code == 401
    assert (await client.post("/api/auth/set_password", json={"password": "synthetic"})).status_code == 401
    ws = Socket(runtime.app)
    await ws.input.put({"type": "websocket.connect"})
    assert (await ws.next())["code"] == 4401
    await ws.task
    code = pending(runtime)
    response = await client.post("/api/auth/enroll", json={"code": code})
    assert response.status_code == 200
    assert (await client.get("/api/settings")).status_code == 200
    for enabled in [False, True]:
        response = await client.put("/api/auth/device_auth", json={"enabled": enabled})
        assert response.status_code == 403
    client.cookies.clear()
    assert (await client.get("/api/settings")).status_code == 401
    (runtime.path / "device_auth_freeze.json").unlink()
    if not gate:
        assert (await client.get("/api/settings")).status_code == 200


async def test_malformed_freeze_cannot_be_overridden_by_binding(runtime):
    bind(runtime)
    (runtime.path / "device_auth_freeze.json").write_text("{")
    assert (await runtime.client.get("/api/settings")).status_code == 401
    code = pending(runtime)
    assert (await runtime.client.post("/api/auth/enroll", json={"code": code})).status_code == 503
    assert (runtime.path / "device_enroll_pending.json").exists()


async def test_storage_failure_is_not_acknowledged(runtime, monkeypatch):
    cookie = bind(runtime)
    before = runtime.settings.path.read_bytes()

    def full_disk(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("web.auth_files.os.replace", full_disk)
    response = await runtime.client.delete(f"/api/auth/devices/{hashlib.sha256(cookie.encode()).hexdigest()}")
    assert response.status_code == 503
    assert runtime.settings.path.read_bytes() == before
    assert runtime.settings.valid_device(cookie) is not None


async def test_close_requires_bound_device_and_reopen_requires_host(runtime):
    client, settings = runtime.client, runtime.settings
    response = await client.put("/api/auth/new_devices", json={"enabled": False})
    assert response.status_code == 403
    assert settings.allow_new_devices
    cookie = bind(runtime)
    settings.set_password("synthetic password")
    client.headers["Authorization"] = "Bearer " + settings.issue_token()
    response = await client.put("/api/auth/new_devices", json={"enabled": False})
    assert response.status_code == 200
    assert not settings.allow_new_devices
    assert settings.valid_device(cookie) is not None
    assert (await client.get("/api/settings")).status_code == 200
    info = (await client.get("/api/auth/devices")).json()
    assert info["allow_new_devices"] is False
    assert (await client.put("/api/auth/new_devices", json={"enabled": True})).status_code == 403
    (runtime.path / "device_access_policy.json").write_text(json.dumps({"allow_new_devices": True}))
    assert settings.allow_new_devices


@pytest.mark.parametrize("cookie_kind", ["missing", "garbage", "revoked", "frozen"])
async def test_closed_policy_denies_every_http_route_before_body_or_handler(runtime, cookie_kind):
    settings, client = runtime.settings, runtime.client
    if cookie_kind == "garbage":
        client.cookies.set(DEVICE_COOKIE, "synthetic-invalid")
    elif cookie_kind in {"revoked", "frozen"}:
        cookie = bind(runtime)
        if cookie_kind == "revoked":
            settings.revoke_device(hashlib.sha256(cookie.encode()).hexdigest())
        else:
            freeze(runtime)
    settings.deny_new_devices()
    settings.set_password("synthetic password")
    client.headers["Authorization"] = "Bearer " + settings.issue_token()
    code = pending(runtime)
    for method, path in [
        ("GET", "/"), ("GET", "/graph"), ("GET", "/favicon.ico"),
        ("GET", "/static/favicon.svg"), ("GET", "/api/auth/state"),
        ("GET", "/api/files/synthetic/content"), ("GET", "/openapi.json"),
        ("POST", "/api/auth/login"), ("POST", "/api/auth/enroll"),
        ("PUT", "/api/auth/device_auth"), ("PUT", "/api/auth/new_devices"),
        ("POST", "/api/files/upload"), ("GET", "/unknown-route"),
        ("OPTIONS", "/api/settings"), ("HEAD", "/"),
    ]:
        reads = []

        async def unread_body():
            reads.append(True)
            raise AssertionError("Closed entrance must not consume the request body")
            yield b""  # Async-generator stream for httpx.

        response = await client.request(method, path, content=unread_body())
        assert response.status_code == 403, (method, path, response.text)
        assert "no-store" in response.headers["cache-control"]
        assert not reads
    assert code and (runtime.path / "device_enroll_pending.json").exists()


async def test_closed_policy_denies_valid_code_even_from_existing_device(runtime):
    bind(runtime)
    runtime.settings.deny_new_devices()
    code = pending(runtime)
    response = await runtime.client.post("/api/auth/enroll", json={"code": code})
    assert response.status_code == 403
    assert (runtime.path / "device_enroll_pending.json").exists()
    assert (await runtime.client.put("/api/auth/device_auth", json={"enabled": False})).status_code == 200
    runtime.client.cookies.clear()
    assert (await runtime.client.get("/api/settings")).status_code == 403


async def test_close_disconnects_anonymous_socket_but_keeps_old_device_socket(runtime):
    cookie = bind(runtime)
    anonymous, old = Socket(runtime.app), Socket(runtime.app, cookie)
    await anonymous.open()
    await old.open()
    try:
        response = await runtime.client.put("/api/auth/new_devices", json={"enabled": False})
        assert response.status_code == 200
        assert (await anonymous.next())["code"] == 4401
        payload = {"type": "message", "data": {"text": "synthetic old-device message"}}
        await runtime.manager.broadcast(payload)
        assert json.loads((await old.next())["text"]) == payload
        assert runtime.manager.count() == 1
    finally:
        await anonymous.close()
        await old.close()
    new = Socket(runtime.app)
    await new.input.put({"type": "websocket.connect"})
    assert (await new.next())["code"] == 4401
    await new.task


async def test_host_close_blocks_broadcast_without_password_or_normal_device_gate(runtime):
    ws = Socket(runtime.app)
    await ws.open()
    try:
        runtime.settings.deny_new_devices()
        await runtime.manager.broadcast({"type": "message", "data": {"text": "MUST_NOT_BE_SENT"}})
        assert (await ws.next())["code"] == 4401
        assert ws.output.empty()
    finally:
        await ws.close()


async def test_old_device_can_read_static_image_and_login_when_policy_closed(runtime, monkeypatch):
    from web import server

    bind(runtime)
    runtime.settings.deny_new_devices()
    root = runtime.path / "synthetic-root"
    image = root / "data" / "files" / "synthetic.png"
    image.parent.mkdir(parents=True)
    image_bytes = b"\x89PNG\r\n\x1a\nSYNTHETIC_IMAGE"
    image.write_bytes(image_bytes)
    monkeypatch.setenv("TENTACLE_HOME", str(root))

    async def get_file(local_id):
        return {"path": "data/files/synthetic.png", "mime": "image/png"}

    monkeypatch.setattr(server, "get_file_manager", lambda: SimpleNamespace(get_file=get_file))
    for path in ["/", "/graph", "/static/favicon.svg", "/favicon.ico", "/api/auth/state", "/api/files/synthetic/content"]:
        response = await runtime.client.get(path)
        assert response.status_code == 200, path
        assert "no-store" in response.headers["cache-control"]
        if path.endswith("/content"):
            assert response.content == image_bytes
    runtime.settings.set_password("synthetic password")
    assert (await runtime.client.get("/api/settings")).status_code == 401
    assert (await runtime.client.post("/api/auth/login", json={"password": "synthetic password"})).status_code == 200


async def test_policy_write_failure_does_not_claim_closed(runtime, monkeypatch):
    bind(runtime)

    def full_disk(*args):
        raise OSError("synthetic full disk")

    monkeypatch.setattr("web.auth_files.os.replace", full_disk)
    response = await runtime.client.put("/api/auth/new_devices", json={"enabled": False})
    assert response.status_code == 503
    assert runtime.settings.allow_new_devices
