"""Use one local DeepSeek Harness Web Host for browser and delegated work."""

import asyncio
from email.parser import BytesParser
from email.policy import default
import json
import os
import time
import re
import signal
import sys
import uuid
from contextlib import suppress
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from subagents.async_utils import settle


_PROFILE_PATCH = """- id: session-log-deepseek
  config:
    enabled: false
- id: hmr
  disabled: true
- insert:
    - id: tentacle-bridge
      name: {bridge}
"""
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class DshError(RuntimeError):
    """An execution error whose text is safe to show in a task receipt."""


class DshCleanupError(DshError):
    """An operation could not be stopped safely."""


def valid_session_id(value):
    return isinstance(value, str) and _SESSION_ID.fullmatch(value) is not None


def _text(message):
    return "".join(part["text"] for part in message.get("content", []) if part.get("type") == "text")


def _decode_rpc_packet(response: httpx.Response):
    content_type = response.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "multipart/form-data":
        raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + response.content
        message = BytesParser(policy=default).parsebytes(raw)
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") == "metadata":
                return json.loads(part.get_payload(decode=True))
        raise ValueError("DSH response missing metadata")
    return response.json()


class DshRunner:
    def __init__(self, config):
        self.config = config
        self._lock = asyncio.Lock()
        self._process = None
        self._client = None
        self._drain = None
        self._login_url = None
        self._renew_at = 0

    async def client(self):
        async with self._lock:
            if self._process and self._process.returncode is None and self._client:
                if time.monotonic() >= self._renew_at:
                    await self._refresh_login()
                return self._client
            await self._stop()
            if sys.platform != "linux":
                raise DshError("DSH coding 执行仅支持 Linux")
            home = self.config.dsh_home
            workspace = self.config.workspace_root
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            workspace.mkdir(parents=True, exist_ok=True)
            env = {name: os.environ[name] for name in (
                "LANG", "LC_ALL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
            ) if name in os.environ}
            env.update(
                PATH=os.pathsep.join(path for path in os.get_exec_path() if os.path.isabs(path)) or os.defpath,
                HOME=str(home), DSH_HOME=str(home), DSH_PERMISSION_MODE="workspace-write",
                DSH_TELEMETRY_DISABLED="1",
            )
            with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".yaml", dir=home) as profile:
                profile.write(_PROFILE_PATCH.format(bridge=Path(__file__).with_name("dsh_host.js").resolve().as_uri()))
                profile.flush()
                try:
                    launch = asyncio.create_task(asyncio.create_subprocess_exec(
                        self.config.dsh_binary, "--profile", "web", "--patch", profile.name,
                        "--no-open", "--host", "127.0.0.1", "--port", "0",
                        cwd=workspace, env=env, start_new_session=True,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    ))
                    try:
                        self._process = await settle(launch)
                    except asyncio.CancelledError:
                        self._process = launch.result()
                        raise
                    async with asyncio.timeout(40):
                        url = await self._startup_url()
                        self._drain = asyncio.create_task(self._discard_stdout())
                        self._client = httpx.AsyncClient(base_url=url.copy_with(query=None), trust_env=False, timeout=15)
                        self._login_url = url
                        await self._refresh_login()
                        bridge = await self._client.get("/api/tentacle-bridge")
                        if bridge.status_code != 200 or bridge.json().get("version") != 1:
                            raise DshError("DSH Web Host 未加载 Tentacle 会话桥")
                    return self._client
                except BaseException as error:
                    await settle(self._stop())
                    if isinstance(error, (TimeoutError, OSError, HTTPException, httpx.HTTPError)):
                        raise DshError("无法启动 DSH Web Host，请检查 Linux 上的 DSH 安装与 profile") from None
                    raise

    async def _refresh_login(self):
        try:
            cookies = await asyncio.to_thread(self._login, self._login_url)
        except (OSError, HTTPException):
            raise DshError("DSH Web Host 登录失败") from None
        for name, value in cookies.items():
            self._client.cookies.set(name, value)
        self._renew_at = time.monotonic() + 86400

    @staticmethod
    def _login(url):
        # The launch URL carries a credential. Keep it out of HTTP client logs.
        connection = HTTPConnection(url.host, url.port, timeout=8)
        try:
            connection.request("GET", url.raw_path.decode("ascii"))
            response = connection.getresponse()
            cookie = SimpleCookie()
            cookie.load(response.getheader("Set-Cookie", ""))
            if response.status != 303 or not cookie:
                raise DshError("DSH Web Host 登录失败")
            return {name: entry.value for name, entry in cookie.items()}
        finally:
            connection.close()

    async def _startup_url(self):
        while line := await self._process.stdout.readline():
            line = line.decode(errors="replace").strip()
            if line.startswith("dsh web:"):
                url = httpx.URL(line.removeprefix("dsh web:").strip())
                if (url.scheme == "http" and url.host == "127.0.0.1" and url.port
                        and url.path == "/" and url.params.get("token")):
                    return url
                break
        raise DshError("DSH 未提供本地 Web Host 的登录入口")

    async def _discard_stdout(self):
        while await self._process.stdout.read(65536):
            pass

    async def stop(self):
        async with self._lock:
            await self._stop()

    async def _stop(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._login_url = None
        if self._process is not None:
            process = self._process
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            self._process = None
        if self._drain is not None:
            await asyncio.gather(self._drain, return_exceptions=True)
            self._drain = None

    async def rpc(self, method, args):
        client = await self.client()
        rpc_id = str(uuid.uuid4())
        payload = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": {"args": {"_request" if method == "session/list" else "request": args}},
        }
        try:
            async with asyncio.timeout(25):
                # Call DSH's native /api controller RPC directly with the browser session
                # cookie so workspace binding, request IDs and Web approvals stay shared.
                response = await client.post(f"/api/{method}", json=payload, timeout=25)
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                packet = _decode_rpc_packet(response)
                if (not isinstance(packet, dict)
                        or packet.get("type") != "server-response"
                        or packet.get("rpcId") != rpc_id):
                    raise ValueError("invalid server-response")
                result = packet.get("result")
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise ValueError("rpc error")
                return result.get("value")
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError):
            raise DshError("DSH 请求失败，请检查会话和服务状态") from None

    async def sessions(self):
        response = await self.rpc("session/list", {})
        return [row for row in response["items"] if row.get("origin") != "subagent"
                and valid_session_id(row.get("sessionId")) and row.get("cwd")]

    async def session(self, session_id):
        if not valid_session_id(session_id):
            raise ValueError("DSH 会话 ID 无效")
        for row in await self.sessions():
            if row["sessionId"] == session_id:
                return row
        raise ValueError("DSH 会话不存在或不可关联")

    async def recent(self, session_id, limit):
        response = await self.rpc("session/events", {"address": {"kind": "session", "sessionId": session_id}, "maxMessages": limit})
        messages = []
        for record in response["records"]:
            event = record["event"]
            if event["type"] == "user/message":
                messages.append({"role": "user", "text": _text(event["data"])})
            elif event["type"] == "assistant/message":
                messages.append({"role": "assistant", "text": _text(event["data"]["message"])})
        return [{"role": item["role"], "text": item["text"][-2000:]} for item in messages if item["text"]][-limit:]

    async def run(self, workspace, session_id, message, *, save_session):
        if session_id is None:
            workspace_result = await self.rpc("workspace/create", {"path": str(workspace)})
            session = await self.rpc("session/create", {"workspaceId": workspace_result["workspace"]["workspaceId"]})
            session_id = session["sessionId"]
            await settle(save_session(session_id))
        summary = await self.session(session_id)
        if Path(summary["cwd"]) != workspace:
            raise DshError("DSH 会话工作区与任务不一致")
        state = await self.rpc("session/state", {"sessionId": session_id})
        cursor = state["cursor"]
        request_id = str(uuid.uuid4())
        turn = _Turn(request_id, self.config.max_output_chars)
        try:
            await settle(self.rpc("session/prompt", {
                "sessionId": session_id, "requestId": request_id,
                "mode": "queue", "content": [{"type": "text", "text": message}],
            }))
            while True:
                wait = await self.rpc("session/wait", {
                    "sessionId": session_id, "afterSeq": cursor, "maxMessages": 1, "timeoutMs": 1500,
                })
                state = wait["state"]
                if state["cursor"] > cursor:
                    for event in await self._events_after(session_id, cursor, state["cursor"]):
                        turn.observe(event)
                        if turn.reason:
                            return turn.reason, "".join(turn.parts), turn.usage
                    cursor = state["cursor"]
                if wait["reason"] == "input-required":
                    await asyncio.sleep(1)
                if wait["reason"] == "idle":
                    raise DshError("DSH 未记录本次提交的完成结果")
        except (asyncio.CancelledError, DshError):
            # A lost response can still leave our input admitted by DSH.
            await settle(self._cancel_owned(session_id, request_id))
            raise

    async def _events_after(self, session_id, cursor, end):
        pages = []
        before = end + 1
        while before > cursor + 1:
            page = await self.rpc("session/events", {
                "address": {"kind": "session", "sessionId": session_id}, "throughSeq": end,
                "beforeSeq": before, "maxMessages": 16,
            })
            events = [record["event"] for record in page["records"]]
            if not events or events[0]["seq"] >= before:
                raise DshError("DSH 历史事件序列不完整")
            pages.append([event for event in events if event["seq"] > cursor])
            if not page["hasMore"]:
                break
            before = events[0]["seq"]
        return [event for page in reversed(pages) for event in page]

    async def _cancel_owned(self, session_id, request_id):
        with suppress(DshError, TimeoutError):
            async with asyncio.timeout(3):
                state = await self.rpc("session/state", {"sessionId": session_id})
                for item in state.get("queue") or []:
                    if item.get("rpcId") == request_id:
                        await self.rpc("session/updateQueue", {
                            "sessionId": session_id, "itemId": item["id"], "action": {"kind": "remove"},
                        })
                        break
        with suppress(DshError, TimeoutError):
            async with asyncio.timeout(2):
                await self.bridge({"sessionId": session_id, "requestId": request_id})

    async def bridge(self, args):
        try:
            response = await (await self.client()).post("/api/tentacle-bridge", json=args)
            response.raise_for_status()
            return response.json()
        except (ValueError, httpx.HTTPError):
            raise DshError("DSH 会话桥暂不可用") from None


class _Turn:
    def __init__(self, request_id, remaining):
        self.request_id = request_id
        self.remaining = remaining
        self.claimed = False
        self.parts = []
        self.usage = {}
        self.reason = None

    def observe(self, event):
        kind, data = event["type"], event["data"]
        if kind == "user/message" and data.get("source", {}).get("rpcId") == self.request_id:
            self.claimed = True
        elif self.claimed and kind == "assistant/message":
            if self.remaining:
                text = _text(data["message"])[:self.remaining]
                self.parts.append(text)
                self.remaining -= len(text)
            for source, target in (("inputTokens", "prompt_tokens"), ("outputTokens", "completion_tokens")):
                count = (data.get("usage") or {}).get(source, 0)
                self.usage[target] = self.usage.get(target, 0) + count
        elif self.claimed and kind == "turn/end":
            reason = data["reason"]["kind"]
            self.reason = {"completed": "end_turn", "aborted": "cancelled", "max-tokens": "max_tokens"}.get(reason, reason)
