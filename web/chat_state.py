"""只保留在途回合的展示事件，供 Web 重连恢复；不保存模型上下文。"""

from collections import deque
from copy import deepcopy
import uuid


class ChatState:
    def __init__(self):
        self.instance_id = uuid.uuid4().hex
        self.seq = 0
        self._events = {}
        self._finished = deque(maxlen=128)

    def record(self, payload):
        meta = payload.get("round")
        if not meta:
            return payload
        self.seq += 1
        payload = {**payload, "chat_seq": self.seq, "chat_instance": self.instance_id}
        round_id = meta["round_id"]
        if payload["type"] == "round_finished":
            self._events.pop(round_id, None)
            self._finished.append(deepcopy(payload["data"]))
        else:
            events = self._events.setdefault(round_id, [])
            field = {"message_delta": "text", "thinking_delta": "content"}.get(payload["type"])
            # 合并连续增量，避免按 Token 长期保留大量小事件。
            if field and events and events[-1]["type"] == payload["type"] and events[-1]["chat_seq"] == self.seq - 1:
                events[-1]["data"][field] += payload["data"][field]
                events[-1]["chat_seq"] = self.seq
            else:
                events.append(deepcopy(payload))
        return payload

    def snapshot(self, rounds):
        ids = {item["round_id"] for item in rounds}
        events = sorted((event for key, items in self._events.items() if key in ids for event in items),
                        key=lambda event: event["chat_seq"])
        return deepcopy({"instance_id": self.instance_id, "seq": self.seq, "rounds": rounds,
                         "events": events, "finished": list(self._finished)})
