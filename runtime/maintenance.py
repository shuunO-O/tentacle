"""Coordinate delegated host operations with the session lifecycle."""

import asyncio
import json
import logging
import uuid

from runtime import maintenance_protocol as protocol

logger = logging.getLogger(__name__)


class MaintenanceUnavailable(ValueError):
    pass


class MaintenanceController:
    def __init__(self, session, status_root=protocol.STATUS_ROOT, socket_path=protocol.SOCKET):
        self.session = session
        self.status_root = status_root
        self.socket_path = socket_path
        self._rounds = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._owner = None
        self._local = None
        self._task = None
        self._observers = []

    def add_observer(self, observer):
        """注册同步状态通知回调 `(snapshot: dict) -> None`。"""
        if observer not in self._observers:
            self._observers.append(observer)

    def _emit(self):
        if not self._observers:
            return
        try:
            snap = self.snapshot()
        except Exception:
            logger.debug("生成维护快照失败", exc_info=True)
            return
        for observer in list(self._observers):
            try:
                observer(snap)
            except Exception:
                logger.debug("维护状态观察者回调失败", exc_info=True)

    def _read(self, name, default=None):
        try:
            return json.loads((self.status_root / name).read_bytes())
        except (OSError, ValueError):
            return default

    def _records(self):
        records = self._read(protocol.OPERATIONS, [])
        public = []
        for item in records:
            record = dict(item)
            pid = record.pop("worker_pid", None)
            identity = record.pop("worker_identity", None)
            if record["state"] == "running" and (not identity or protocol.worker_identity(pid) != identity):
                record.update(state="interrupted", error="主机维护进程已中断，请检查主机状态")
            public.append(record)
        return public

    def snapshot(self, operation_id=None):
        version = self._read("version.json")
        checked = self._read("update-check.json")
        records = self._records()
        local = self._local
        if local and not any(item["id"] == local["id"] for item in records):
            records.insert(0, local)
        operation = (next((item for item in records if item["id"] == operation_id), None)
                     if operation_id else next(iter(records), None))
        available = bool(version and self.socket_path.is_socket())
        can_update = bool(available and checked and checked.get("ok") and checked.get("available")
                          and checked.get("data_compatible") is True
                          and checked.get("updater_compatible") is not False
                          and checked.get("target")
                          and checked.get("current") == version.get("version"))
        return {"available": available, "version": version, "check": checked,
                "can_update": can_update, "operation": operation}

    def round_started(self, round_id):
        if not self.session._restarting:
            self._rounds.add(round_id)
            self._idle.clear()

    def round_finished(self, terminal, notified):
        round_id = terminal["round_id"]
        self._rounds.discard(round_id)
        if not self._rounds:
            self._idle.set()
        if round_id == self._owner:
            self._owner = None
            if notified and terminal["status"] == "completed" and terminal["stop_reason"] == "complete":
                self.start()
            else:
                self._finish_local("cancelled", "发起回合未正常完成，已取消维护预约")

    async def wait_for_rounds(self):
        await self._idle.wait()

    def reserve(self, action, version=None, *, from_round=False):
        protocol.validate_action(action, version)
        snapshot = self.snapshot()
        if not snapshot["available"]:
            raise MaintenanceUnavailable("当前实例未启用 Linux 主机维护入口")
        current = snapshot["operation"]
        if (self.session._restarting or (self._task and not self._task.done())
                or (current and current["state"] in {"waiting", "submitting", "running"})):
            raise ValueError("已有维护操作正在进行")
        if action == "update" and (not snapshot["can_update"] or version != snapshot["check"]["latest"]):
            raise ValueError("请先检查更新并选择数据兼容的正式版本")
        owner = None
        if action != "check" and from_round:
            owner = self.session.current_round_id()
            if owner not in self._rounds:
                raise ValueError("请在主会话回合中申请重启或更新")
        if action != "check":
            self.session._restarting = True
        self._owner = owner
        self._local = {"id": uuid.uuid4().hex, "action": action, "version": version, "state": "waiting"}
        self._emit()
        return dict(self._local)

    def start(self):
        if self._local and self._local["state"] == "waiting" and self._task is None and self._owner is None:
            self._task = asyncio.create_task(self._run())

    def _finish_local(self, state, error=None):
        if self._local["action"] != "check":
            self.session._restarting = False
        self._local = {**self._local, "state": state, **({"error": error} if error else {})}
        self._emit()

    async def _run(self):
        request = {key: self._local[key] for key in ("id", "action", "version")}
        try:
            if request["action"] != "check":
                await self.wait_for_rounds()
                # Other session operations also use this lock.
                async with self.session._lock:
                    pass
            self._local["state"] = "submitting"
            self._emit()
            async with asyncio.timeout(15):
                await self._submit(request)
            last_state = None
            while True:
                operation = next((record for record in self._records() if record["id"] == request["id"]), None)
                if operation is None:
                    raise ValueError("缺少主机维护操作记录")
                if operation["state"] != last_state:
                    last_state = operation["state"]
                    self._emit()
                if operation["state"] != "running":
                    break
                await asyncio.sleep(0.5)
            # The authoritative terminal result lives in the root-owned operation history.
            self._finish_local(operation["state"], operation.get("error"))
        except ValueError as error:
            self._finish_local("failed", str(error))
        except (OSError, TimeoutError, RuntimeError):
            logger.warning("主机维护请求通信失败 id=%s", request["id"])
            self._finish_local("failed", "无法与主机维护服务通信，请检查主机维护状态")
        finally:
            self._task = None

    async def _submit(self, request):
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            raw = await reader.readline()
            if not raw:
                raise ConnectionError("maintenance socket closed before acknowledgement")
            response = json.loads(raw)
            if response.get("accepted") is not True:
                raise ValueError(response.get("error", "主机维护服务未接收请求"))
            writer.write(b"1")
            await writer.drain()
        finally:
            writer.close()

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
