"""从有序模型事件收集可见消息，供实时展示与逐条归档共用。"""

import json

from agent.protocol import Event, EventType


class VisibleMessages:
    def __init__(self, platform: str):
        self.platform = platform
        self.messages: list[dict] = []
        self._stream: list[str] = []
        self._pending_sends: dict[str, dict] = {}

    def _append(self, text: str, platform: str):
        if text.strip():
            self.messages.append({"role": "assistant", "content": text, "platform": platform})

    def finish(self):
        """收尾已显示的增量；没有发送结果的工具调用不生成外发记录。"""
        if self._stream:
            self._append("".join(self._stream), self.platform)
            self._stream.clear()

    def consume(self, event: Event) -> list[dict]:
        """按事件顺序返回本次闭合的消息；流式传输本身不依赖这些记录。"""
        start = len(self.messages)
        kind, data = event.type, event.data or {}
        if kind == EventType.TEXT_DELTA:
            self._stream.append(data.get("content", ""))
        elif kind == EventType.TEXT:
            self.finish()
            self._append(data.get("content", ""), self.platform)
        elif kind == EventType.TOOL_CALL_START:
            self.finish()
            if data.get("name") == "send_message" and data.get("tool_call_id"):
                try:
                    args = json.loads(data.get("arguments", "{}"))
                except (TypeError, ValueError):
                    args = {}
                if isinstance(args, dict):
                    platform = args.get("platform", self.platform)
                    text = args.get("text", "")
                    if platform != self.platform and isinstance(text, str) and text.strip():
                        self._pending_sends[data["tool_call_id"]] = {
                            "content": text, "platform": platform,
                        }
        elif kind == EventType.TOOL_RESULT and data.get("name") == "send_message":
            pending = self._pending_sends.pop(data.get("tool_call_id"), None)
            result = data.get("result")
            if pending and result is not None:
                try:
                    result = json.loads(result)
                except (TypeError, ValueError):
                    pass
                if not (isinstance(result, dict) and result.get("error")):
                    self._append(pending["content"], pending["platform"])
        elif kind in (EventType.FINAL_ANSWER, EventType.ERROR):
            self.finish()
            self._pending_sends.clear()
        return self.messages[start:]
