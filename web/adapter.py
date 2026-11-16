"""
Web 平台适配器

职责：
1. ConnectionManager：管理活跃的 WebSocket 连接，向所有连接广播事件
2. WebAdapter：把 AgentSession 派发的事件转成 JSON，经 ConnectionManager 推送到前端

事件映射：
- THINKING / THINKING_DELTA  -> thinking / thinking_delta（思考过程）
- TEXT / TEXT_DELTA          -> message（正式消息气泡，platform=web）
- TEXT_DELTA（分段开启时）   -> message_delta / message_seg_end 交错流
- TOOL_CALL_START            -> tool_call（send_message 非 web 平台除外，等确认后发 message）
- TOOL_RESULT                -> tool_result（send_message 非 web 平台成功时发 message 气泡）
- FINAL_ANSWER               -> final
- NOTICE                     -> message（宿主提醒气泡，platform=system）
- ERROR / USAGE -> error / usage

分段显示（split_messages 开关，默认开）：agent 输出按空行拆成多条气泡，仅影响
Web 展示层，模型上下文与归档不变。流式路径下后端在段边界处补发
message_seg_end（前端关闭当前流式气泡，下一段增量自动开新气泡），段内
打字机效果完整保留；增量文本按边界切分透传，保证前后段内容不串气泡。

内部草稿机制已移除：模型直接输出（TEXT）就是 web 平台可见的正式消息。
send_message 降级为外部平台（qq 等）出口；web 平台不再消费 send_message。
"""

import asyncio
import json
import logging

from runtime.paths import state_path
import socket
import time
from collections.abc import Callable
from contextvars import ContextVar

from agent.protocol import Event, EventType
from agent.segments import find_confirmed_boundaries, split_segments
from agent.visible import VisibleMessages
from web.settings import get_web_settings
from web.chat_state import ChatState

logger = logging.getLogger(__name__)


def get_lan_ip() -> str:
    """获取本机局域网 IP，用于打印访问地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class ConnectionManager:
    """WebSocket 连接管理（广播事件到所有连接）"""

    def __init__(self):
        self._connections: dict = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws, authorize: Callable[[], bool]):
        async with self._lock:
            self._connections[ws] = authorize

    async def disconnect(self, ws):
        async with self._lock:
            self._connections.pop(ws, None)

    def count(self) -> int:
        return len(self._connections)

    @staticmethod
    def _authorized(authorize) -> bool:
        try:
            return bool(authorize())
        except Exception:
            logger.exception("WS 凭证复查失败，拒绝继续推送")
            return False

    async def _reject(self, ws):
        await self.disconnect(ws)
        try:
            await asyncio.wait_for(ws.close(code=4401), timeout=10)
        except Exception:
            logger.debug("关闭失效 WS 连接失败", exc_info=True)

    async def revalidate(self):
        """密码、设备或开关变更后立刻断开失效连接。"""
        async with self._lock:
            conns = list(self._connections.items())
        for ws, authorize in conns:
            if not self._authorized(authorize):
                await self._reject(ws)

    async def _send_authorized(self, ws, authorize, text):
        # 在实际发送协程里复查，避免 wait_for 调度前后被吊销时仍使用旧判定。
        if not self._authorized(authorize):
            await self._reject(ws)
            return
        await ws.send_text(text)

    async def broadcast(self, payload: dict):
        if not self._connections:
            return
        text = json.dumps(payload, ensure_ascii=False)
        async with self._lock:
            conns = list(self._connections.items())
        dead = []
        for ws, authorize in conns:
            # 每次发包前校验，包括 CLI 带外冻结和会话 TTL 到期。
            try:
                await asyncio.wait_for(self._send_authorized(ws, authorize, text), timeout=10)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.pop(ws, None)


class WebAdapter:
    """
    Web 适配器

    只负责把 AgentSession 的事件推送到 WebSocket，业务逻辑全在 AgentSession。
    模型直接输出（TEXT）作为 web 平台正式消息推送；
    send_message 仅消费非 web 平台（如 qq），以 message 事件灰显气泡。
    """

    def __init__(self, session, conn_manager: ConnectionManager, heartbeat=None, platform: str = "web",
                 settings=None):
        self.session = session
        self.conn_manager = conn_manager
        self.heartbeat = heartbeat
        self.platform = platform
        # WebSettings 引用（默认取全局单例；测试可注入 stub）
        self._settings = settings or get_web_settings()
        self._visible = VisibleMessages(platform)
        self._active_round_id = None
        self._event_context = ContextVar("web_event_context", default=None)
        self.chat_state = ChatState()
        self._broadcast_lock = asyncio.Lock()
        # 流式分段状态（每轮重置：TOOL_CALL_START / FINAL_ANSWER / ERROR）
        self._stream_buf = ""        # 本轮累计流式文本（含已透传部分，供边界全量重扫）
        self._sent_pos = 0           # 已透传给前端的位置
        self._last_sent_bound = -1   # 已发过 message_seg_end 的最大边界位置
        # 主事件循环引用：终端输出 hook 在 to_thread 线程调用，需跨线程调度广播
        self._loop = None
        # 终端输出历史（环形缓冲，供前端独立终端页回放）
        self._terminal_history: list[dict] = []
        self._terminal_history_max = 2000
        # Token 用量累计（自启动以来；USAGE 事件累加，仅作用量统计）
        self._usage_stats = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
        }
        # 最近一次 API 请求的缓存命中情况（真正的缓存命中率视角：
        # 前缀未被破坏时每轮应接近满命中，骤降说明前缀破坏或缓存过期）
        self._last_usage: dict | None = None

    def set_event_loop(self, loop):
        self._loop = loop

    def _on_terminal_output(self, data: str):
        """终端实时输出回调：桥接到 WebSocket 广播，前端 xterm.js 实时渲染"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._broadcast_terminal, data)
        except RuntimeError:
            pass

    def _broadcast_terminal(self, data: str):
        # 记录历史（带时间戳），供前端回放
        self._terminal_history.append({
            "ts": time.time(),
            "content": data,
        })
        if len(self._terminal_history) > self._terminal_history_max:
            del self._terminal_history[: len(self._terminal_history) - self._terminal_history_max]

        asyncio.create_task(
            self.conn_manager.broadcast(
                {"type": "terminal_data", "data": {"content": data}}
            )
        )

    def get_terminal_history(self, since_ts: float = 0) -> list[dict]:
        """返回终端历史（可选按时间戳过滤，供前端进入终端页时回放）。"""
        if not since_ts:
            return list(self._terminal_history)
        return [h for h in self._terminal_history if h["ts"] >= since_ts]

    async def _callback(self, event: Event):
        token = self._event_context.set(event.context)
        try:
            await self._dispatch(event)
        finally:
            self._event_context.reset(token)

    async def _broadcast(self, payload: dict):
        context = self._event_context.get()
        if context is not None and context.round_id:
            payload = {**payload, "round": {
                "round_id": context.round_id, "request_id": context.request_id,
                "source": context.source, "platform": context.platform,
            }}
        async with self._broadcast_lock:
            await self.conn_manager.broadcast(self.chat_state.record(payload))

    async def _dispatch(self, event: Event):
        t = event.type
        data = event.data or {}
        visible = []
        if t in (EventType.TEXT, EventType.TEXT_DELTA, EventType.TOOL_CALL_START,
                 EventType.TOOL_RESULT, EventType.FINAL_ANSWER, EventType.ERROR):
            self._active_round_id = getattr(event.context, "round_id", None)
            visible = self._visible.consume(event)
            self._visible.messages.clear()  # IO 只消费新消息，不持有整轮归档。

        if t == EventType.THINKING:
            await self._broadcast(
                {"type": "thinking", "data": {"content": data.get("content", "")}}
            )
        elif t == EventType.THINKING_DELTA:
            await self._broadcast(
                {"type": "thinking_delta", "data": {"content": data.get("content", "")}}
            )
        elif t == EventType.TEXT:
            await self._broadcast_messages(visible)
        elif t == EventType.TEXT_DELTA:
            # 流式：分段模式下后端按段边界切分增量透传，边界处发
            # message_seg_end；段内打字机效果完整保留
            await self._handle_text_delta(data.get("content", ""))
        elif t == EventType.TOOL_CALL_START:
            # 工具调用打断文本流：前端 tool_call 会关闭流式气泡，后端同步重置分段状态
            if data.get("name") == "send_message" and self._stream_buf:
                # send_message 不展示 tool_call，仍需用已有收尾事件闭合当前文字，
                # 否则发送失败后继续生成的文字会粘到上一条气泡。
                await self._broadcast({"type": "message_seg_end"})
            self._reset_stream_state()
            name = data.get("name", "")
            if name == "send_message":
                # 外发消息由公共收集器在发送确认后生成。
                return
            await self._broadcast(
                {"type": "tool_call", "data": {"name": name, "arguments": data.get("arguments", "")}}
            )
        elif t == EventType.TOOL_RESULT:
            name = data.get("name", "")
            if visible:
                await self._broadcast_messages(visible)
                return
            await self._broadcast(
                {"type": "tool_result", "data": {
                    "name": name,
                    "result": data.get("result", ""),
                }}
            )
        elif t == EventType.FINAL_ANSWER:
            # 仅模型输出结束；宿主完成另由 ROUND_FINISHED 通知。
            self._reset_stream_state()
            await self._broadcast(
                {"type": "final", "data": {"content": data.get("content", "")}}
            )
        elif t == EventType.ROUND_FINISHED:
            if data["round_id"] == self._active_round_id:
                self._reset_stream_state()
                self._visible = VisibleMessages(self.platform)
                self._active_round_id = None
            await self._broadcast({"type": "round_finished", "data": data})
        elif t == EventType.ERROR:
            self._reset_stream_state()
            await self._broadcast(
                {"type": "error", "data": {"error": data.get("error", "")}}
            )
        elif t == EventType.NOTICE:
            await self._broadcast(
                {"type": "message", "data": {"text": data.get("content", ""), "platform": "system"}}
            )
        elif t == EventType.WEB_SEARCH:
            await self._broadcast(
                {"type": "web_search", "data": {"query": data.get("query", ""), "status": data.get("status", "")}}
            )
        elif t == EventType.USAGE:
            # 累计用量（仅统计视角）
            for key in self._usage_stats:
                self._usage_stats[key] += data.get(key, 0) or 0
            # 最近一次请求的缓存命中（命中率以单轮为准，非累计比值）
            prompt = data.get("prompt_tokens", 0) or 0
            cached = data.get("cached_tokens", 0) or 0
            self._last_usage = {
                "prompt_tokens": prompt,
                "cached_tokens": cached,
                "cache_hit_rate": round(cached / prompt * 100, 1) if prompt > 0 else None,
            }
            await self._broadcast({"type": "usage", "data": dict(data)})

    async def _broadcast_messages(self, messages: list[dict]):
        for message in messages:
            segments = (split_segments(message["content"]) if self._split_enabled()
                        else [message["content"]])
            for text in segments:
                await self._broadcast({"type": "message", "data": {
                    "text": text, "platform": message["platform"],
                }})

    async def _handle_text_delta(self, content: str):
        """
        流式增量透传 + 段边界检测。

        分段开启时：对累计缓冲全量重扫确认边界（幂等），把上次之后的
        新边界之前的尾部增量透传、发 message_seg_end、再透传新段已有内容——
        保证新旧段内容不混进同一个前端气泡。末尾未确认的空行序列
        （后续可能继续延长）不触发，留待下个增量或 final 收尾。
        """
        self._stream_buf += content
        buf = self._stream_buf
        if not self._split_enabled():
            if content:
                await self._broadcast_delta(content)
            return

        bounds = [b for b in find_confirmed_boundaries(buf) if b > self._last_sent_bound]
        prev = self._sent_pos
        for b in bounds:
            seg = buf[prev:b]
            if seg:
                await self._broadcast_delta(seg)
            await self._broadcast({"type": "message_seg_end"})
            self._last_sent_bound = b
            prev = b
        tail = buf[prev:]
        if tail:
            await self._broadcast_delta(tail)
        self._sent_pos = len(buf)

    async def _broadcast_delta(self, text: str):
        await self._broadcast(
            {"type": "message_delta", "data": {"text": text, "platform": self.platform}}
        )

    def _split_enabled(self) -> bool:
        try:
            return bool(self._settings.split_messages)
        except AttributeError:
            return True

    def _reset_stream_state(self):
        """重置流式分段状态（工具调用打断 / 本轮结束 / 出错时调用）"""
        self._stream_buf = ""
        self._sent_pos = 0
        self._last_sent_bound = -1

    def get_usage_stats(self) -> dict:
        """
        返回用量统计（重启清零）

        - last: 最近一次请求的缓存命中（命中率 = 单轮 cached/prompt，
          反映缓存前缀健康度）
        - total: 自启动累计用量（只做消耗统计，不给命中率——累计比值
          混合了各轮上下文大小，既非命中率也非准确折扣率）
        """
        return {
            "last": self._last_usage,
            "total": dict(self._usage_stats),
        }

    async def run(self, app, host: str, port: int):
        """启动 uvicorn 服务（与心跳模块一起被 gather）"""
        import signal
        import threading

        import uvicorn

        import os

        cert_dir = state_path("certs")
        cert_file = os.path.join(cert_dir, "tentacle2u.xyz.pem")
        key_file = os.path.join(cert_dir, "tentacle2u.xyz.key")
        ssl_enabled = os.path.isfile(cert_file) and os.path.isfile(key_file)

        if ssl_enabled:
            config = uvicorn.Config(
                app, host=host, port=port,
                ssl_certfile=cert_file, ssl_keyfile=key_file,
                log_level="warning", access_log=False,
            )
        else:
            config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        self._server = server
        scheme = "https" if ssl_enabled else "http"
        logger.info(f"WebUI 启动: {scheme}://{host}:{port}")
        print(f"[WebUI] 本机访问:  {scheme}://127.0.0.1:{port}")
        print(f"[WebUI] 局域网访问: {scheme}://{get_lan_ip()}:{port}")
        # Uvicorn re-raises SIGTERM after its own shutdown. Let the entry point
        # finish closing the scheduler, session and database before the process exits.
        restore_sigterm = (threading.current_thread() is threading.main_thread()
                           and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL)
        if restore_sigterm:
            signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            await server.serve()
        finally:
            if restore_sigterm:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def request_stop(self):
        """让 HTTP/WS 服务按 uvicorn 的正常关闭流程退出。"""
        server = getattr(self, "_server", None)
        if server is not None:
            server.should_exit = True
