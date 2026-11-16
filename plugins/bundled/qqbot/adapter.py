"""QQ 官方机器人插件适配器（C2C 私聊双向通道）。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Optional
import uuid

import aiohttp

from agent.segments import split_segments
from attachments.manager import get_file_manager
from attachments.outgoing import open_outgoing_file, split_file_markers
from runtime.paths import state_path
from .attachments import receive_attachments
from .constants import (
    API_BASE,
    CONNECT_TIMEOUT_SECONDS,
    DEDUP_MAX_SIZE,
    DEDUP_WINDOW_SECONDS,
    DEFAULT_API_TIMEOUT,
    GATEWAY_URL_PATH,
    INTENTS,
    MAX_MESSAGE_LENGTH,
    MAX_QUICK_DISCONNECT_COUNT,
    MAX_RECONNECT_ATTEMPTS,
    MSG_TYPE_TEXT,
    QUICK_DISCONNECT_THRESHOLD,
    RATE_LIMIT_DELAY,
    RECONNECT_BACKOFF,
    TOKEN_URL,
)

logger = logging.getLogger(__name__)

QQ_MAX_FILE_BYTES = 20 * 1024 * 1024

# 不可恢复的致命断线码(直接停止重连,提示排查)
_FATAL_CODES = {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}
# 会话失效码:需清 session 重新 Identify
_SESSION_ERROR_CODES = {
    4006, 4007, 4900, 4901, 4902, 4903, 4904, 4905,
    4906, 4907, 4908, 4909, 4910, 4911, 4912, 4913,
}


def read_qq_file(value: str) -> tuple[str, bytes]:
    """在安全策略检查下读取受 QQ 20 MiB 限制的本地出站文件。"""
    path, stream, metadata = open_outgoing_file(value)
    with stream:
        if metadata.st_size > QQ_MAX_FILE_BYTES:
            raise ValueError("QQ 文件超过 20 MiB 上限，请通过 Web 下载")
        data = stream.read(QQ_MAX_FILE_BYTES + 1)
        if len(data) > QQ_MAX_FILE_BYTES:
            raise ValueError("QQ 文件超过 20 MiB 上限，请通过 Web 下载")
        if not data:
            raise ValueError("QQ 不支持发送空文件")
        return path.name, data


class QQCloseError(Exception):
    """携带 WS 关闭码的异常，供重连循环区分处理。"""

    def __init__(self, code: int, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"QQ WebSocket closed: code={code} reason={reason}")


class QQAdapter:
    """QQ 官方机器人适配器（C2C 私聊转发站）。"""

    def __init__(
        self,
        session=None,
        app_id: str = "",
        client_secret: str = "",
        allow_from: Optional[list] = None,
        max_chunk: int = MAX_MESSAGE_LENGTH,
        default_to: str = "",
        proxy: str = "",
        split_messages: bool = True,
        split_max_segments: int = 3,
        chat_file: Optional[Path] = None,
        emit_message: Optional[Callable[..., Awaitable[Any]]] = None,
    ):
        self.session = session
        self._emit_message = emit_message
        self._app_id = str(app_id or "").strip()
        self._client_secret = str(client_secret or "").strip()
        self._allow_from = [str(x).strip() for x in (allow_from or []) if str(x).strip()]
        self._max_chunk = int(max_chunk) or MAX_MESSAGE_LENGTH
        self._default_to = str(default_to or "").strip()
        self._proxy = str(proxy or "").strip()
        self._split_messages = bool(split_messages)
        self._split_max_segments = int(split_max_segments) or 3
        self._send_interval = 0.2

        self._running = False
        self._http: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval: float = 30.0
        self._send_lock = asyncio.Lock()
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._ready_at: Optional[float] = None
        self._awaiting_heartbeat_ack = False

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        self._seen_messages: dict[str, float] = {}
        self._latest_chat: Optional[dict] = None
        self._chat_file = Path(chat_file) if chat_file is not None else state_path("data/plugins/qqbot/chat.json")
        self._load_latest_chat()

    async def run(self) -> None:
        """连接并维持 WS 长连接与退避重连。"""
        self._running = True
        reconnect_attempts = 0
        quick_disconnects = 0

        while self._running:
            connect_ts = time.monotonic()
            self._ready_at = None
            self._awaiting_heartbeat_ack = False
            try:
                await self._connect()
                logger.info("[QQBot] WS 已建立,等待 READY/RESUMED")
                await self._listen_forever()
            except asyncio.CancelledError:
                break
            except QQCloseError as exc:
                code = exc.code
                logger.warning("[QQBot] WS 关闭: code=%s reason=%s", code, exc.reason)
                if code in _FATAL_CODES:
                    desc = {
                        4013: "intent 未授权/未开通对应能力",
                        4014: "intent 未授权",
                        4914: "bot 离线或仅沙箱可连",
                        4915: "bot 被封禁",
                    }.get(code, f"致命错误(code={code})")
                    logger.error("[QQBot] 停止重连:%s,请检查 QQ 开放平台后台(凭据/intent/沙箱状态)", desc)
                    break
                if code == 4004:
                    self._access_token = None
                    self._token_expires_at = 0.0
                if code == 4008:
                    logger.info("[QQBot] 限频(4008),等待 %ds", RATE_LIMIT_DELAY)
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                if code in _SESSION_ERROR_CODES:
                    self._session_id = None
                    self._last_seq = None
            except Exception as exc:
                logger.warning("[QQBot] WS 异常: %s", exc)
            finally:
                await self._teardown_ws()

            if not self._running:
                break

            now = time.monotonic()
            if self._ready_at is not None and now - self._ready_at >= QUICK_DISCONNECT_THRESHOLD:
                reconnect_attempts = 0
                quick_disconnects = 0
            elif now - connect_ts < QUICK_DISCONNECT_THRESHOLD:
                quick_disconnects += 1
                if quick_disconnects >= MAX_QUICK_DISCONNECT_COUNT:
                    logger.error(
                        "[QQBot] 连续 %d 次快速断连,请检查 AppID/Secret 是否正确、"
                        "C2C 私聊能力是否已开通(开放平台后台)",
                        MAX_QUICK_DISCONNECT_COUNT,
                    )
                    break
            else:
                quick_disconnects = 0

            reconnect_attempts += 1
            if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                logger.error("[QQBot] 重连次数超限,停止")
                break
            delay = RECONNECT_BACKOFF[min(reconnect_attempts - 1, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[QQBot] %ds 后重连(第 %d 次)...", delay, reconnect_attempts)
            await asyncio.sleep(delay)

        self._running = False
        logger.info("[QQBot] 运行结束")

    async def stop(self) -> None:
        """停止连接并释放网络资源。"""
        self._running = False
        await self._teardown_ws()
        if self._http:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None

    async def _connect(self) -> None:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        await self._ensure_token()
        url = await self._get_gateway_url()
        await self._open_ws(url)

    async def _open_ws(self, url: str) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = await self._http.ws_connect(
            url,
            timeout=CONNECT_TIMEOUT_SECONDS,
            proxy=self._proxy or None,
        )
        logger.info("[QQBot] WebSocket 已连接: %s", url.split("?")[0])

    async def _ensure_token(self) -> str:
        """返回有效 access_token，过期时加锁刷新（单飞）。"""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.time() < self._token_expires_at - 60:
                return self._access_token
            if self._http is None:
                self._http = aiohttp.ClientSession()
            resp = await self._http.post(
                TOKEN_URL,
                json={"appId": self._app_id, "clientSecret": self._client_secret},
                timeout=DEFAULT_API_TIMEOUT,
                proxy=self._proxy or None,
            )
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"获取 access_token 失败[{resp.status}]: {data}")
            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"access_token 响应缺少 access_token: {data}")
            self._access_token = token
            self._token_expires_at = time.time() + int(data.get("expires_in", 7200))
            logger.info("[QQBot] access_token 已刷新(有效期 %ss)", data.get("expires_in", 7200))
            return token

    async def _get_gateway_url(self) -> str:
        """走统一 REST 通道获取 WS 网关地址。"""
        data = await self._api_request("GET", GATEWAY_URL_PATH)
        url = data.get("url")
        if not url:
            raise RuntimeError(f"gateway 响应缺少 url: {data}")
        return url

    async def _listen_forever(self) -> None:
        """持续收帧直到连接关闭。"""
        handshake_deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
        while self._running and self._ws and not self._ws.closed:
            if self._ready_at is None:
                msg = await asyncio.wait_for(
                    self._ws.receive(),
                    timeout=max(0, handshake_deadline - time.monotonic()),
                )
            else:
                msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    await self._dispatch_payload(payload)
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                raise QQCloseError(msg.data, msg.extra)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                if self._ws.close_code is not None:
                    raise QQCloseError(self._ws.close_code)
                raise RuntimeError("WS 连接已关闭")
        if self._running and self._ws and self._ws.close_code is not None:
            raise QQCloseError(self._ws.close_code)

    async def _dispatch_payload(self, payload: dict) -> None:
        op = payload.get("op")
        t = payload.get("t")
        s = payload.get("s")
        d = payload.get("d")

        if isinstance(s, int) and (self._last_seq is None or s > self._last_seq):
            self._last_seq = s

        if op == 10:
            d_data = d if isinstance(d, dict) else {}
            interval_ms = int(d_data.get("heartbeat_interval", 30000))
            self._heartbeat_interval = interval_ms / 1000.0 * 0.8
            if self._session_id and self._last_seq is not None:
                await self._send_resume()
            else:
                await self._send_identify()
            if self._heartbeat_task is None:
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            return

        if op == 0 and t:
            if t == "READY":
                self._session_id = d.get("session_id")
                self._ready_at = time.monotonic()
                logger.info("[QQBot] Ready, session_id=%s", self._session_id)
            elif t == "RESUMED":
                self._ready_at = time.monotonic()
                logger.info("[QQBot] 会话恢复成功")
            elif t == "C2C_MESSAGE_CREATE":
                asyncio.create_task(self._handle_c2c_message(d))
            elif t == "GROUP_AT_MESSAGE_CREATE":
                logger.debug("[QQBot] 群@消息忽略(仅支持 C2C): id=%s", str(d.get("id", "")))
            else:
                logger.debug("[QQBot] 未处理事件: %s", t)
            return

        if op == 11:
            self._awaiting_heartbeat_ack = False
            return

        if op == 7:
            logger.info("[QQBot] 服务端要求重连(op 7)")
            if self._ws and not self._ws.closed:
                await self._ws.close()
            return

        if op == 9:
            if not d:
                self._session_id = None
                self._last_seq = None
            if self._ws and not self._ws.closed:
                await self._ws.close()
            return

        logger.debug("[QQBot] 未知 op: %s", op)

    async def _send_identify(self) -> None:
        token = await self._ensure_token()
        payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": INTENTS,
                "shard": [0, 1],
                "properties": {"$os": "linux", "$browser": "tentacle", "$device": "tentacle"},
            },
        }
        async with self._send_lock:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(payload)
        logger.info("[QQBot] Identify 已发送")

    async def _send_resume(self) -> None:
        token = await self._ensure_token()
        payload = {
            "op": 6,
            "d": {
                "token": f"QQBot {token}",
                "session_id": self._session_id,
                "seq": self._last_seq,
            },
        }
        try:
            async with self._send_lock:
                if self._ws and not self._ws.closed:
                    await self._ws.send_json(payload)
            logger.info("[QQBot] Resume 已发送")
        except Exception as exc:
            logger.warning("[QQBot] Resume 发送失败,重连后重新 Identify: %s", exc)
            self._session_id = None
            self._last_seq = None
            raise

    async def _heartbeat_loop(self) -> None:
        """按 Hello 间隔的 80% 周期发送 op 1 心跳（带最新 seq）。"""
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._ws or self._ws.closed:
                    return
                if self._awaiting_heartbeat_ack:
                    logger.warning("[QQBot] 心跳 ACK 超时,主动重连")
                    await self._ws.close()
                    return
                try:
                    async with self._send_lock:
                        if self._ws and not self._ws.closed:
                            self._awaiting_heartbeat_ack = True
                            await asyncio.wait_for(
                                self._ws.send_json({"op": 1, "d": self._last_seq}),
                                timeout=self._heartbeat_interval,
                            )
                except Exception as exc:
                    logger.warning("[QQBot] 心跳发送失败,主动重连: %s", exc)
                    await self._ws.close()
                    return
        except asyncio.CancelledError:
            pass

    async def _teardown_ws(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    async def _handle_c2c_message(self, d: Any) -> None:
        if not isinstance(d, dict):
            return
        msg_id = str(d.get("id", ""))
        if not msg_id or self._is_duplicate(msg_id):
            return
        author = d.get("author") if isinstance(d.get("author"), dict) else {}
        user_openid = str(author.get("user_openid", "")).strip()
        if not user_openid:
            return
        if self._allow_from and user_openid not in self._allow_from:
            logger.info("[QQBot] 忽略非白名单 C2C 消息: %s", user_openid)
            return
        content = str(d.get("content", "")).strip()
        attachments = d.get("attachments") or []
        if not content and not attachments:
            logger.debug("[QQBot] 空文本消息忽略: %s", msg_id)
            return

        self._latest_chat = {"type": "c2c", "chat_id": user_openid, "ts": time.time()}
        self._save_latest_chat()
        logger.info("[QQBot] C2C 消息 id=%s from=%s content=%r", msg_id, user_openid, content[:80])

        try:
            records = None
            if attachments:
                if self._http is None:
                    self._http = aiohttp.ClientSession()
                try:
                    records = await receive_attachments(attachments, self._http, get_file_manager(), proxy=self._proxy or None)
                except (ValueError, RuntimeError) as exc:
                    await self._deliver_to(f"附件接收失败：{exc}", {"type": "c2c", "chat_id": user_openid})
                    return
            if self._emit_message is not None:
                result = await self._emit_message(
                    content,
                    platform="qq",
                    user_id=user_openid,
                    images=records,
                )
            else:
                kwargs: dict[str, Any] = {"platform": "qq", "user_id": user_openid}
                if records:
                    kwargs["images"] = records
                result = await self.session.run_round(content, **kwargs)
            if result.stop_reason == "command":
                await self._deliver_to(result.final_answer, {"type": "c2c", "chat_id": user_openid})
        except Exception:
            logger.exception("[QQBot] run_round 异常")

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}
        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    def _load_latest_chat(self) -> None:
        """启动时从插件数据目录恢复最近会话目标。"""
        try:
            if self._chat_file.exists():
                data = json.loads(self._chat_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("chat_id"):
                    self._latest_chat = data
                    logger.info("[QQBot] 已恢复最近 QQ 会话: %s", data.get("chat_id"))
        except Exception as exc:
            logger.warning("[QQBot] 恢复会话目标失败: %s", exc)

    def _save_latest_chat(self) -> None:
        """入站消息更新会话目标后落盘。"""
        try:
            self._chat_file.parent.mkdir(parents=True, exist_ok=True)
            self._chat_file.write_text(
                json.dumps(self._latest_chat, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[QQBot] 保存会话目标失败: %s", exc)

    def _resolve_target(self) -> Optional[dict]:
        """取发送目标：最近会话优先，缺省回退配置 default_to。"""
        if self._latest_chat and self._latest_chat.get("chat_id"):
            return self._latest_chat
        if self._default_to:
            logger.info("[QQBot] 无最近会话,使用默认发送对象 default_to")
            return {"type": "c2c", "chat_id": self._default_to, "ts": time.time()}
        return None

    async def _deliver(self, text: str) -> dict:
        return await self._deliver_to(text, self._resolve_target())

    async def _deliver_to(self, text: str, chat: Optional[dict]) -> dict:
        """把 send_message(platform=qq) 文本及 `[file:...]` 附件发往最近一次 C2C 会话。"""
        if chat is None:
            raise RuntimeError("QQ 发送失败：无可用会话目标")
        text = text.strip()
        if not text:
            raise ValueError("QQ 发送失败：消息不能为空")
        chunks: list[tuple[str, str]] = []
        for kind, value in split_file_markers(text):
            if kind == "file":
                chunks.append((kind, value))
                continue
            segments = (
                split_segments(value, max_segments=self._split_max_segments)
                if self._split_messages
                else [value.strip()]
            )
            for seg in segments:
                chunks.extend(("text", seg[i:i + self._max_chunk]) for i in range(0, len(seg), self._max_chunk))
        sent_chunks = 0
        sent_files = 0
        try:
            for kind, chunk in chunks:
                if kind == "file":
                    await self._send_c2c_file(chat["chat_id"], chunk)
                    sent_files += 1
                else:
                    await self._send_c2c_text(chat["chat_id"], chunk)
                sent_chunks += 1
                await asyncio.sleep(self._send_interval)
            logger.info("[QQBot] 已向 %s 发送 %d 条消息", chat["chat_id"], len(chunks))
        except Exception as exc:
            raise RuntimeError(
                f"QQ 发送未完成：已确认发送 {sent_chunks}/{len(chunks)} 段；"
                f"失败段可能未送达或送达状态未知，请勿盲目重发整条消息。原因：{exc}"
            ) from exc
        result = {"sent_chunks": sent_chunks, "total_chunks": len(chunks)}
        if sent_files:
            result["sent_files"] = sent_files
        return result

    async def _api_request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        token = await self._ensure_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }
        resp = await self._http.request(
            method,
            f"{API_BASE}{path}",
            headers=headers,
            json=body,
            timeout=DEFAULT_API_TIMEOUT,
            proxy=self._proxy or None,
        )
        data = await resp.json() if resp.status != 204 else {}
        if resp.status >= 400:
            msg = data.get("message") or data.get("code") or data
            raise RuntimeError(f"QQ API 错误[{resp.status}] {path}: {msg}")
        return data

    async def _send_c2c_text(self, openid: str, content: str, reply_to: Optional[str] = None) -> dict:
        body = {
            "content": content[:MAX_MESSAGE_LENGTH],
            "msg_type": MSG_TYPE_TEXT,
            "msg_seq": self._next_msg_seq(openid),
        }
        if reply_to:
            body["msg_id"] = reply_to
        return await self._api_request("POST", f"/v2/users/{openid}/messages", body)

    async def _send_c2c_file(self, openid: str, path: str) -> dict:
        def payload():
            filename, data = read_qq_file(path)
            return {
                "file_type": 4,
                "file_name": filename,
                "file_data": base64.b64encode(data).decode("ascii"),
                "srv_send_msg": False,
            }

        uploaded = await self._api_request("POST", f"/v2/users/{openid}/files", await asyncio.to_thread(payload))
        file_info = uploaded.get("file_info")
        if not isinstance(file_info, str) or not file_info:
            raise RuntimeError("QQ 文件上传未返回 file_info，未发送附件")
        return await self._api_request(
            "POST",
            f"/v2/users/{openid}/messages",
            {"msg_type": 7, "media": {"file_info": file_info}, "msg_seq": self._next_msg_seq(openid)},
        )

    @staticmethod
    def _next_msg_seq(key: str) -> int:
        """生成 0~65535 随机序号（QQ 要求不与上次重复）。"""
        time_part = int(time.time()) % 100000000
        rand = int(uuid.uuid4().hex[:4], 16)
        return (time_part ^ rand) % 65536

    @staticmethod
    def _parse_json(raw: Any) -> Optional[dict]:
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except (json.JSONDecodeError, TypeError):
            logger.warning("[QQBot] JSON 解析失败: %r", raw)
            return None
