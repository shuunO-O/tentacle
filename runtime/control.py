"""重启协调：等待唯一叙事流完成，由入口清理后替换当前进程。"""

import asyncio
import uuid


REEMBED_REVISION_ENV = "TENTACLE_REEMBED_REVISION"


class RestartController:
    def __init__(self, session, last_reembed=None):
        self.session = session
        self.instance_id = uuid.uuid4().hex
        self.ready = asyncio.Event()
        self.state = "idle"
        self._task = None
        self.reembed_revision = None
        self.last_reembed = last_reembed

    def snapshot(self):
        return {"available": True, "instance_id": self.instance_id, "state": self.state,
                "reembedding": self.reembed_revision is not None, "last_reembed": self.last_reembed}

    def reserve(self, reembed_revision=None):
        if self.state != "idle" or getattr(self.session, "_restarting", False):
            return False
        self.state = "waiting"
        self.reembed_revision = reembed_revision
        self.session._restarting = True
        return True

    async def request(self):
        # HTTP 响应发送后才开始等待回合结束。
        if self.state == "idle":
            self.reserve()
        if self.state != "waiting" or self._task is not None:
            return
        self._task = asyncio.create_task(self._drain())

    async def _drain(self):
        maintenance = getattr(self.session, "maintenance", None)
        if maintenance is not None:
            await maintenance.wait_for_rounds()
        async with self.session._lock:
            self.state = "restarting"
            self.ready.set()

    async def close(self):
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
