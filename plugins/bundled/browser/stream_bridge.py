"""Tentacle browser 插件的实时视口桥接（直连 agent-browser 原生 stream WebSocket）。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shlex
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ALLOWED_MOUSE_EVENTS = frozenset({"mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"})
_ALLOWED_MOUSE_BUTTONS = frozenset({"left", "right", "middle", "none"})
_ALLOWED_KEY_EVENTS = frozenset({"keyDown", "keyUp", "rawKeyDown", "char"})
_MAX_INPUT_BATCH = 64
_MAX_BATCH_DELAY_MS = 400
_IDLE_TIMEOUT_SECONDS = 45.0
_WATCHDOG_INTERVAL_SECONDS = 5.0
_DEFAULT_MAX_FPS = 10


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def _parse_cli_json(output: str) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    """解析 agent-browser `--json` 输出，兼容 `{success, data}` 包装与直接 JSON 对象。"""
    cleaned = _strip_ansi(output).strip()
    if not cleaned:
        return None, "agent-browser 返回空输出"

    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        for line in reversed(cleaned.splitlines()):
            candidate = line.strip()
            if not candidate or candidate[0] not in "{[":
                continue
            try:
                parsed = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue

    if parsed is None:
        return None, cleaned[:300]

    if isinstance(parsed, dict):
        if parsed.get("success") is False:
            err = parsed.get("error") or parsed.get("message") or cleaned[:300]
            return None, str(err)
        if "data" in parsed and isinstance(parsed["data"], (dict, list)):
            return parsed["data"], None
        return parsed, None
    if isinstance(parsed, list):
        return parsed, None
    return None, cleaned[:300]


def _extract_stream_port(payload: Any) -> tuple[bool, int | None]:
    if not isinstance(payload, dict):
        return False, None
    enabled = bool(payload.get("enabled", False))
    raw_port = payload.get("port")
    port: int | None = None
    if isinstance(raw_port, int) and 1 <= raw_port <= 65535:
        port = raw_port
    elif isinstance(raw_port, str) and raw_port.isdigit():
        val = int(raw_port)
        if 1 <= val <= 65535:
            port = val
    return enabled or port is not None, port


def _extract_tabs(payload: Any) -> list[dict[str, Any]]:
    raw_tabs: list[Any] = []
    if isinstance(payload, list):
        raw_tabs = payload
    elif isinstance(payload, dict):
        for key in ("tabs", "pages", "items"):
            if isinstance(payload.get(key), list):
                raw_tabs = payload[key]
                break

    tabs: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_tabs):
        if not isinstance(item, dict):
            continue
        tab_index = item.get("index", idx)
        try:
            tab_index = int(tab_index)
        except (TypeError, ValueError):
            tab_index = idx
        tabs.append({
            "index": tab_index,
            "title": str(item.get("title") or "").strip(),
            "url": str(item.get("url") or "").strip(),
            "active": bool(item.get("active", False)),
        })
    return tabs


def normalize_navigation_url(raw_url: str) -> str:
    """规范化并校验地址栏 URL，仅允许 http(s) 与 about:blank。"""
    candidate = str(raw_url or "").strip()
    if not candidate:
        raise ValueError("URL 不能为空")
    if any(ch in candidate for ch in ("\r", "\n", "\x00")):
        raise ValueError("URL 包含非法控制字符")
    if candidate.lower() == "about:blank":
        return "about:blank"

    parsed = urlsplit(candidate)
    if not parsed.scheme:
        candidate = f"https://{candidate}"
        parsed = urlsplit(candidate)

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持 http://、https:// 或 about:blank 地址")
    return candidate


def _clamp_finite(value: Any, low: float, high: float, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return max(low, min(high, num))


def _clamp_int(value: Any, low: int, high: int, default: int = 0) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def sanitize_input_event(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """校验前端传入的交互事件并转为 agent-browser stream WS 载荷（不记录内容）。"""
    if not isinstance(raw, dict):
        raise ValueError("事件格式无效")
    ev_type = str(raw.get("type") or "").strip()
    delay_ms = _clamp_int(raw.get("delayMs", 0), 0, 80, 0)

    if ev_type == "input_mouse":
        event_type = str(raw.get("eventType") or "").strip()
        if event_type not in _ALLOWED_MOUSE_EVENTS:
            raise ValueError(f"不支持的鼠标事件类型: {event_type}")
        msg: dict[str, Any] = {
            "type": "input_mouse",
            "eventType": event_type,
            "x": round(_clamp_finite(raw.get("x", 0), 0.0, 10000.0), 2),
            "y": round(_clamp_finite(raw.get("y", 0), 0.0, 10000.0), 2),
        }
        if "button" in raw and raw["button"] is not None:
            button = str(raw["button"]).strip()
            if button in _ALLOWED_MOUSE_BUTTONS:
                msg["button"] = button
        if "clickCount" in raw and raw["clickCount"] is not None:
            msg["clickCount"] = _clamp_int(raw["clickCount"], 0, 3, 1)
        if "modifiers" in raw and raw["modifiers"] is not None:
            msg["modifiers"] = _clamp_int(raw["modifiers"], 0, 15, 0)
        if event_type == "mouseWheel":
            msg["deltaX"] = round(_clamp_finite(raw.get("deltaX", 0), -5000.0, 5000.0), 2)
            msg["deltaY"] = round(_clamp_finite(raw.get("deltaY", 0), -5000.0, 5000.0), 2)
        return [msg], delay_ms

    if ev_type == "input_keyboard":
        event_type = str(raw.get("eventType") or "").strip()
        if event_type not in _ALLOWED_KEY_EVENTS:
            raise ValueError(f"不支持的键盘事件类型: {event_type}")
        msg = {
            "type": "input_keyboard",
            "eventType": event_type,
        }
        if isinstance(raw.get("key"), str) and raw["key"]:
            msg["key"] = raw["key"][:64]
        if isinstance(raw.get("code"), str) and raw["code"]:
            msg["code"] = raw["code"][:64]
        if isinstance(raw.get("text"), str) and raw["text"]:
            msg["text"] = raw["text"][:256]
        if "modifiers" in raw and raw["modifiers"] is not None:
            msg["modifiers"] = _clamp_int(raw["modifiers"], 0, 15, 0)
        if "windowsVirtualKeyCode" in raw and raw["windowsVirtualKeyCode"] is not None:
            msg["windowsVirtualKeyCode"] = _clamp_int(raw["windowsVirtualKeyCode"], 0, 255, 0)
        return [msg], delay_ms

    if ev_type == "input_text":
        text = raw.get("text")
        if not isinstance(text, str) or not text:
            return [], 0
        clipped = text[:2000]
        chunks = [clipped[i : i + 128] for i in range(0, len(clipped), 128)]
        return [
            {"type": "input_keyboard", "eventType": "char", "text": chunk}
            for chunk in chunks
        ], delay_ms

    raise ValueError(f"不支持的交互事件类型: {ev_type}")


class StreamConnectBody(BaseModel):
    max_fps: int = Field(default=_DEFAULT_MAX_FPS, ge=1, le=20)


class StreamDisconnectBody(BaseModel):
    disable_daemon_stream: bool = False


class InputBatchBody(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=_MAX_INPUT_BATCH)


class BrowserActionBody(BaseModel):
    action: str
    url: str | None = None
    index: int | None = None


class BrowserStreamBridge:
    """管理本机 agent-browser stream WebSocket 中继与视口状态。"""

    def __init__(
        self,
        *,
        cfg: dict[str, Any],
        run_cli_fn: Callable[[dict[str, Any], dict[str, Any] | None], str | dict[str, Any]],
        emit_ui_event_fn: Callable[[str, Any], Any],
        ws_connect_fn: Callable[..., Any] | None = None,
        idle_timeout: float = _IDLE_TIMEOUT_SECONDS,
    ):
        self._cfg = dict(cfg or {})
        self._run_cli = run_cli_fn
        self._emit_ui_event = emit_ui_event_fn
        self._ws_connect = ws_connect_fn
        self._idle_timeout = float(idle_timeout)

        self._lock = asyncio.Lock()
        self._ws: Any = None
        self._reader_task: asyncio.Task[Any] | None = None
        self._watchdog_task: asyncio.Task[Any] | None = None

        self._connected = False
        self._port: int | None = None
        self._enabled_by_us = False
        self._max_fps = _DEFAULT_MAX_FPS
        self._last_heartbeat = 0.0
        self._last_error: str | None = None

        self._url = ""
        self._title = ""
        self._tabs: list[dict[str, Any]] = []
        self._viewport_width = 1280
        self._viewport_height = 720
        self._latest_frame: dict[str, Any] | None = None

    def _mode_info(self) -> tuple[str, bool]:
        remote = bool((self._cfg.get("cdp_discovery_url") or "").strip())
        if remote:
            return "remote", False
        return "local", bool(self._cfg.get("headed", False))

    def snapshot(self, *, include_frame: bool = False) -> dict[str, Any]:
        mode, headed = self._mode_info()
        data: dict[str, Any] = {
            "connected": self._connected,
            "port": self._port,
            "session": (self._cfg.get("session") or "tentacle").strip(),
            "mode": mode,
            "headed": headed,
            "max_fps": self._max_fps,
            "url": self._url,
            "title": self._title,
            "tabs": list(self._tabs),
            "viewport": {
                "width": self._viewport_width,
                "height": self._viewport_height,
            },
            "has_frame": self._latest_frame is not None,
            "error": self._last_error,
        }
        if include_frame and self._latest_frame is not None:
            data["frame"] = self._latest_frame
        return data

    async def _run_browser(self, command: str) -> str:
        res = await asyncio.to_thread(self._run_cli, {"command": command}, self._cfg)
        return res if isinstance(res, str) else str(res)

    async def _safe_emit(self, event: str, payload: Any) -> None:
        try:
            await self._emit_ui_event(event, payload)
        except Exception as exc:
            logger.debug(f"[browser-stream] 推送 UI 事件失败 ({event}): {exc}")

    async def _ensure_stream_port(self) -> int:
        status_out = await self._run_browser("stream status --json")
        data, _err = _parse_cli_json(status_out)
        enabled, port = _extract_stream_port(data)
        if enabled and port:
            return port

        enable_out = await self._run_browser("stream enable --json")
        enable_data, enable_err = _parse_cli_json(enable_out)
        enabled2, port2 = _extract_stream_port(enable_data)
        if enabled2 and port2:
            self._enabled_by_us = True
            return port2

        if enable_err:
            raise RuntimeError(f"无法开启 agent-browser 实时流：{enable_err}")

        # 某些版本 enable 成功但端口需由 status 读取
        status_out2 = await self._run_browser("stream status --json")
        data2, err2 = _parse_cli_json(status_out2)
        enabled3, port3 = _extract_stream_port(data2)
        if enabled3 and port3:
            self._enabled_by_us = True
            return port3

        detail = err2 or status_out2.strip() or "未返回有效流端口"
        raise RuntimeError(f"无法获取 agent-browser 实时流端口：{detail}")

    async def _open_ws(self, port: int, max_fps: int) -> Any:
        ws_url = f"ws://127.0.0.1:{port}/?pacing=ack&maxFps={max_fps}"
        if self._ws_connect is not None:
            return await self._ws_connect(ws_url)
        import websockets

        return await websockets.connect(
            ws_url,
            origin="http://127.0.0.1",
            proxy=None,
            open_timeout=8,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        )

    async def connect(self, *, max_fps: int = _DEFAULT_MAX_FPS) -> dict[str, Any]:
        fps = _clamp_int(max_fps, 1, 20, _DEFAULT_MAX_FPS)
        stale: tuple[Any, Any, Any] = (None, None, None)
        async with self._lock:
            self._last_heartbeat = time.monotonic()
            if self._connected and self._ws is not None:
                self._max_fps = fps
                try:
                    await self._ws.send(json.dumps({"type": "config", "maxFps": fps}))
                except Exception:
                    pass
                return self.snapshot(include_frame=True)
            stale = self._detach_ws_locked()

        await self._finalize_detached(*stale)

        async with self._lock:
            self._last_error = None
            try:
                port = await self._ensure_stream_port()
                ws = await self._open_ws(port, fps)
            except Exception as exc:
                self._last_error = str(exc)
                raise RuntimeError(self._last_error) from exc

            self._ws = ws
            self._port = port
            self._max_fps = fps
            self._connected = True
            self._last_heartbeat = time.monotonic()
            self._reader_task = asyncio.create_task(self._reader_loop(ws), name="browser-stream-reader")
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="browser-stream-watchdog")

        await self.refresh_metadata()
        snap = self.snapshot(include_frame=True)
        await self._safe_emit("stream.state", self.snapshot(include_frame=False))
        return snap

    async def disconnect(self, *, disable_daemon_stream: bool = False) -> dict[str, Any]:
        should_disable = False
        async with self._lock:
            should_disable = bool(disable_daemon_stream or self._enabled_by_us)
            self._enabled_by_us = False
            detached = self._detach_ws_locked()

        await self._finalize_detached(*detached)

        if should_disable:
            try:
                await self._run_browser("stream disable")
            except Exception as exc:
                logger.debug(f"[browser-stream] 关闭 daemon stream 失败: {exc}")

        snap = self.snapshot(include_frame=False)
        await self._safe_emit("stream.state", snap)
        return snap

    async def shutdown(self) -> None:
        """插件卸载或服务停止时关闭连接与后台任务。"""
        async with self._lock:
            self._enabled_by_us = False
            detached = self._detach_ws_locked()
        await self._finalize_detached(*detached)

    def _detach_ws_locked(self) -> tuple[Any, asyncio.Task[Any] | None, asyncio.Task[Any] | None]:
        ws = self._ws
        reader = self._reader_task
        watchdog = self._watchdog_task
        self._ws = None
        self._reader_task = None
        self._watchdog_task = None
        self._connected = False
        self._port = None
        return ws, reader, watchdog

    async def _finalize_detached(
        self,
        ws: Any,
        reader: asyncio.Task[Any] | None,
        watchdog: asyncio.Task[Any] | None,
    ) -> None:
        current = asyncio.current_task()
        for task in (watchdog, reader):
            if task is not None and task is not current and not task.done():
                task.cancel()
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        for task in (watchdog, reader):
            if task is not None and task is not current:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    def touch_heartbeat(self) -> dict[str, Any]:
        self._last_heartbeat = time.monotonic()
        return self.snapshot(include_frame=False)

    async def _watchdog_loop(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)
                if not self._connected:
                    return
                if time.monotonic() - self._last_heartbeat >= self._idle_timeout:
                    logger.info("[browser-stream] 前端空闲超时，自动断开实时视口流")
                    await self.disconnect(disable_daemon_stream=False)
                    return
        except asyncio.CancelledError:
            raise

    async def _reader_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if not isinstance(raw, str):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue

                msg_type = msg.get("type")
                if msg_type == "frame":
                    data = msg.get("data")
                    if not isinstance(data, str) or not data:
                        continue
                    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
                    dw = _clamp_int(meta.get("deviceWidth", self._viewport_width), 1, 8192, self._viewport_width)
                    dh = _clamp_int(meta.get("deviceHeight", self._viewport_height), 1, 8192, self._viewport_height)
                    self._viewport_width = dw
                    self._viewport_height = dh
                    seq = msg.get("seq")
                    frame_payload = {
                        "data": data,
                        "seq": seq,
                        "metadata": {
                            "deviceWidth": dw,
                            "deviceHeight": dh,
                            "pageScaleFactor": meta.get("pageScaleFactor", 1),
                            "timestamp": meta.get("timestamp"),
                        },
                    }
                    self._latest_frame = frame_payload
                    if seq is not None:
                        try:
                            await ws.send(json.dumps({"type": "ack", "seq": seq}))
                        except Exception:
                            pass
                    await self._safe_emit("stream.frame", frame_payload)
                elif msg_type == "url":
                    url = msg.get("url")
                    if isinstance(url, str):
                        self._url = url
                    title = msg.get("title")
                    if isinstance(title, str):
                        self._title = title
                    await self._safe_emit("stream.meta", self._meta_snapshot())
                elif msg_type == "tabs":
                    self._tabs = _extract_tabs(msg)
                    for tab in self._tabs:
                        if tab.get("active"):
                            if tab.get("url"):
                                self._url = tab["url"]
                            if tab.get("title"):
                                self._title = tab["title"]
                    await self._safe_emit("stream.meta", self._meta_snapshot())
                elif msg_type == "status":
                    if isinstance(msg.get("url"), str):
                        self._url = msg["url"]
                    if isinstance(msg.get("title"), str):
                        self._title = msg["title"]
                    await self._safe_emit("stream.meta", self._meta_snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"实时流连接已中断：{exc}"
            logger.warning(f"[browser-stream] 读取循环异常: {exc}")
        finally:
            cleaned = False
            async with self._lock:
                if self._ws is ws:
                    self._ws = None
                    self._connected = False
                    self._port = None
                    self._reader_task = None
                    watchdog = self._watchdog_task
                    self._watchdog_task = None
                    if watchdog is not None and not watchdog.done():
                        watchdog.cancel()
                    cleaned = True
            if cleaned:
                await self._safe_emit("stream.state", self.snapshot(include_frame=False))

    def _meta_snapshot(self) -> dict[str, Any]:
        return {
            "url": self._url,
            "title": self._title,
            "tabs": list(self._tabs),
            "viewport": {
                "width": self._viewport_width,
                "height": self._viewport_height,
            },
        }

    async def send_inputs(self, events: list[dict[str, Any]]) -> int:
        if not self._connected or self._ws is None:
            raise RuntimeError("实时视口尚未连接")
        if not isinstance(events, list) or not events:
            return 0
        if len(events) > _MAX_INPUT_BATCH:
            raise ValueError(f"单次最多发送 {_MAX_INPUT_BATCH} 条交互事件")

        self._last_heartbeat = time.monotonic()
        prepared: list[tuple[list[dict[str, Any]], int]] = []
        for raw in events:
            prepared.append(sanitize_input_event(raw))

        ws = self._ws
        if ws is None or not self._connected:
            raise RuntimeError("实时视口尚未连接")

        sent = 0
        total_delay = 0
        for payloads, delay_ms in prepared:
            if delay_ms > 0 and total_delay < _MAX_BATCH_DELAY_MS:
                step = min(delay_ms, _MAX_BATCH_DELAY_MS - total_delay)
                total_delay += step
                await asyncio.sleep(step / 1000.0)
            for payload in payloads:
                try:
                    await ws.send(json.dumps(payload))
                    sent += 1
                except Exception as exc:
                    raise RuntimeError(f"发送交互事件失败: {exc.__class__.__name__}") from exc
        return sent

    async def refresh_metadata(self) -> dict[str, Any]:
        """尽力通过 agent-browser 查询当前标签页与地址信息。"""
        try:
            tabs_out = await self._run_browser("tab --json")
            tabs_data, err = _parse_cli_json(tabs_out)
            if err is None:
                tabs = _extract_tabs(tabs_data)
                if tabs:
                    self._tabs = tabs
                    active_tab = next((t for t in tabs if t.get("active")), tabs[0])
                    if active_tab.get("url"):
                        self._url = active_tab["url"]
                    if active_tab.get("title"):
                        self._title = active_tab["title"]
                    return self._meta_snapshot()
        except Exception:
            pass

        try:
            url_out = await self._run_browser("get url --json")
            url_data, err = _parse_cli_json(url_out)
            if err is None and isinstance(url_data, dict) and isinstance(url_data.get("url"), str):
                self._url = url_data["url"]
        except Exception:
            pass
        return self._meta_snapshot()

    async def perform_action(self, body: BrowserActionBody) -> dict[str, Any]:
        self._last_heartbeat = time.monotonic()
        act = str(body.action or "").strip().lower()
        if act == "open":
            url = normalize_navigation_url(body.url or "")
            output = await self._run_browser(f"open {shlex.quote(url)}")
        elif act == "back":
            output = await self._run_browser("back")
        elif act == "forward":
            output = await self._run_browser("forward")
        elif act == "reload":
            output = await self._run_browser("reload")
        elif act == "tab_new":
            if body.url and body.url.strip():
                url = normalize_navigation_url(body.url)
                output = await self._run_browser(f"tab new {shlex.quote(url)}")
            else:
                output = await self._run_browser("tab new")
        elif act == "tab_select":
            if body.index is None or body.index < 0 or body.index > 200:
                raise ValueError("标签页索引无效")
            output = await self._run_browser(f"tab {int(body.index)}")
        elif act == "tab_close":
            if body.index is None:
                output = await self._run_browser("tab close")
            else:
                if body.index < 0 or body.index > 200:
                    raise ValueError("标签页索引无效")
                output = await self._run_browser(f"tab close {int(body.index)}")
        elif act == "refresh_tabs":
            output = "ok"
        else:
            raise ValueError(f"不支持的浏览器操作: {act}")

        if output.startswith(("错误：", "[退出码:", "[安全审查]", "[状态: 超时]")):
            raise RuntimeError(output)

        await self.refresh_metadata()
        snap = self.snapshot(include_frame=False)
        await self._safe_emit("stream.meta", self._meta_snapshot())
        return snap


def create_browser_router(bridge: BrowserStreamBridge) -> APIRouter:
    """构造挂载在 `/api/plugins/ext/browser/*` 下的浏览器实时视口路由。"""
    router = APIRouter()

    @router.get("/status")
    async def get_status(include_frame: bool = False):
        return bridge.snapshot(include_frame=include_frame)

    @router.post("/stream/connect")
    async def connect_stream(body: StreamConnectBody | None = None):
        req = body or StreamConnectBody()
        try:
            return await bridge.connect(max_fps=req.max_fps)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/stream/disconnect")
    async def disconnect_stream(body: StreamDisconnectBody | None = None):
        req = body or StreamDisconnectBody()
        return await bridge.disconnect(disable_daemon_stream=req.disable_daemon_stream)

    @router.post("/stream/heartbeat")
    async def heartbeat_stream():
        return bridge.touch_heartbeat()

    @router.post("/input")
    async def forward_inputs(body: InputBatchBody):
        try:
            sent = await bridge.send_inputs(body.events)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "sent": sent}

    @router.post("/action")
    async def perform_action(body: BrowserActionBody):
        try:
            return await bridge.perform_action(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
