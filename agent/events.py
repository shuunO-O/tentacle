"""多适配器事件扇出器：独立于具体平台通道，向已挂接端回调分发主会话事件。"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class EventFanout:
    """多适配器事件扇出器：把 AgentSession 派发的每个事件依次喂给各平台回调。

    各目标独立 try/except，任一平台异常不影响其余平台。
    """

    def __init__(self, *targets):
        self._targets = [t for t in targets if callable(t)]

    def add(self, callback) -> None:
        if callable(callback) and callback not in self._targets:
            self._targets.append(callback)

    def remove(self, callback) -> None:
        self._targets = [t for t in self._targets if t is not callback]

    async def __call__(self, event):
        for cb in list(self._targets):
            try:
                await cb(event)
            except Exception:
                logger.exception("事件回调异常: %s", getattr(cb, "__qualname__", repr(cb)))
