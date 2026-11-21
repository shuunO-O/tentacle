"""宿主能力代理：把 Memory / Goals / Scheduler / Situation / Workspace 权限门控后暴露给插件。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
from pathlib import Path
from typing import Any

from attachments.outgoing import check_file_access
from plugins.permissions import PermissionDeniedError, require_permission
from runtime.paths import get_paths


class WorkspaceFilesProxy:
    """工作区与附件访问代理（受 fs:workspace_read / fs:workspace_write 门控）。"""

    def __init__(
        self,
        *,
        plugin_id: str,
        permissions: frozenset[str],
        workspace_root: Path | None = None,
        file_manager=None,
    ):
        self._plugin_id = plugin_id
        self._permissions = permissions
        self._workspace_override = workspace_root
        self._file_manager = file_manager

    def _workspace(self) -> Path:
        root = self._workspace_override if self._workspace_override is not None else get_paths().workspace
        return Path(root).resolve()

    def _resolve_workspace_path(self, rel_path: str) -> tuple[Path, str]:
        if not isinstance(rel_path, str) or not rel_path.strip() or "\x00" in rel_path:
            raise ValueError(f"无效工作区相对路径: {rel_path!r}")
        normalized = rel_path.strip().replace("\\", "/")
        if normalized.startswith("/") or normalized in {".", ".."}:
            raise PermissionDeniedError(f"插件 {self._plugin_id} 仅可使用工作区相对路径: {rel_path!r}")
        root = self._workspace()
        target = (root / normalized).resolve()
        if not target.is_relative_to(root):
            raise PermissionDeniedError(f"插件 {self._plugin_id} 工作区路径越界: {rel_path!r}")
        check_file_access(target)
        return target, normalized

    def read_text(self, rel_path: str, *, encoding: str = "utf-8") -> str:
        require_permission(self._permissions, "fs:workspace_read", self._plugin_id)
        target, _ = self._resolve_workspace_path(rel_path)
        return target.read_text(encoding=encoding)

    def read_bytes(self, rel_path: str) -> bytes:
        require_permission(self._permissions, "fs:workspace_read", self._plugin_id)
        target, _ = self._resolve_workspace_path(rel_path)
        return target.read_bytes()

    def write_text(self, rel_path: str, content: str, *, encoding: str = "utf-8") -> Path:
        require_permission(self._permissions, "fs:workspace_write", self._plugin_id)
        target, _ = self._resolve_workspace_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        return target

    def write_bytes(self, rel_path: str, data: bytes) -> Path:
        require_permission(self._permissions, "fs:workspace_write", self._plugin_id)
        target, _ = self._resolve_workspace_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def file_marker(self, rel_path: str) -> str:
        """校验工作区文件合法并返回可直接附在工具/回复中的 `[file:...]` 标记。"""
        if "fs:workspace_write" not in self._permissions:
            require_permission(self._permissions, "fs:workspace_read", self._plugin_id)
        target, norm = self._resolve_workspace_path(rel_path)
        if not target.is_file():
            raise FileNotFoundError(f"工作区文件不存在: {norm}")
        return f"[file:{norm}]"

    async def store_image(self, data: bytes, filename: str = "") -> dict[str, Any]:
        """写入受管图片附件记录并返回 FileManager 公共元信息。"""
        require_permission(self._permissions, "fs:workspace_write", self._plugin_id)
        fm = self._file_manager
        if fm is None:
            from attachments.manager import FileManager

            fm = FileManager()
        record = await fm.store_image(data, filename=filename)
        return fm.public_record(record)

    async def upload_image(self, data: bytes, filename: str = "") -> dict[str, Any]:
        """写入受管图片附件（并按当前文件通道配置惰性处理远端引用），返回工具图片块所需元信息。"""
        require_permission(self._permissions, "fs:workspace_write", self._plugin_id)
        fm = self._file_manager
        if fm is None:
            from attachments.manager import get_file_manager

            fm = get_file_manager()
        record = await fm.upload_image(data, filename=filename)
        out = {"local_id": record["local_id"], "mime": record["mime"]}
        if record.get("file_id"):
            out["file_id"] = record["file_id"]
        return out


class MemoryProxy:
    """记忆检索与写入代理（受 memory:read / memory:write 门控）。"""

    def __init__(self, *, plugin_id: str, permissions: frozenset[str], memory_store=None, chronicle=None):
        self._plugin_id = plugin_id
        self._permissions = permissions
        self._memory_store = memory_store
        self._chronicle = chronicle

    def _store(self):
        if self._memory_store is not None:
            return self._memory_store
        from memory.ladybug_store import LadybugStore

        return LadybugStore()

    async def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        require_permission(self._permissions, "memory:read", self._plugin_id)
        return await self._store().search(query, **kwargs)

    def get_summaries(self, start_time: float, end_time: float) -> list[dict[str, Any]]:
        require_permission(self._permissions, "memory:read", self._plugin_id)
        if self._chronicle is None:
            raise RuntimeError("ChronicleManager 尚未注入")
        return self._chronicle.get_summaries(start_time, end_time, strict=True)

    async def save_memory(self, *args: Any, **kwargs: Any) -> Any:
        require_permission(self._permissions, "memory:write", self._plugin_id)
        return await self._store().save_memory(*args, **kwargs)


class GoalsProxy:
    """目标读写代理（受 goals:read / goals:write 门控）。"""

    def __init__(self, *, plugin_id: str, permissions: frozenset[str], goal_manager=None):
        self._plugin_id = plugin_id
        self._permissions = permissions
        self._goal_manager = goal_manager

    def _manager(self):
        if self._goal_manager is not None:
            return self._goal_manager
        from runtime.goals import get_goal_manager

        return get_goal_manager()

    async def list_active(self, goal_id: int | None = None) -> list[dict[str, Any]]:
        require_permission(self._permissions, "goals:read", self._plugin_id)
        return await self._manager().list_active_goals(goal_id=goal_id)

    async def get_summary(self, goal_id: int) -> dict[str, Any] | None:
        require_permission(self._permissions, "goals:read", self._plugin_id)
        return await self._manager().get_goal_summary(goal_id)

    async def update(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        require_permission(self._permissions, "goals:write", self._plugin_id)
        return await self._manager().update(operations)


class SchedulerProxy:
    """定时任务读写代理（受 scheduler:read / scheduler:write 门控）。"""

    def __init__(self, *, plugin_id: str, permissions: frozenset[str], scheduler=None):
        self._plugin_id = plugin_id
        self._permissions = permissions
        self._scheduler = scheduler

    def _manager(self):
        if self._scheduler is not None:
            return self._scheduler
        from runtime.scheduler import get_scheduler

        return get_scheduler()

    async def list_active(self) -> list[dict[str, Any]]:
        require_permission(self._permissions, "scheduler:read", self._plugin_id)
        return await self._manager().list_active_schedules()

    async def get_detail(self, schedule_id: int, *, run_limit: int = 10) -> dict[str, Any] | None:
        require_permission(self._permissions, "scheduler:read", self._plugin_id)
        return await self._manager().get_schedule_detail(schedule_id, run_limit=run_limit)

    async def update(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        require_permission(self._permissions, "scheduler:write", self._plugin_id)
        return await self._manager().update(operations)


class SituationProxy:
    """近况与随手记代理（受 situation:write 门控）。"""

    def __init__(self, *, plugin_id: str, permissions: frozenset[str], builder=None):
        self._plugin_id = plugin_id
        self._permissions = permissions
        self._builder = builder

    def _require_builder(self):
        if self._builder is None:
            raise RuntimeError("SystemPromptBuilder 尚未注入")
        return self._builder

    def read_situation(self) -> str:
        require_permission(self._permissions, "situation:write", self._plugin_id)
        return self._require_builder().read_situation()

    def write_situation(self, content: str) -> None:
        require_permission(self._permissions, "situation:write", self._plugin_id)
        self._require_builder().write_situation(content)

    def add_note(self, section: str, entry: str) -> bool:
        require_permission(self._permissions, "situation:write", self._plugin_id)
        if section not in ("master", "memory"):
            raise ValueError("section 必须为 master 或 memory")
        return self._require_builder().add_entry(section, entry)

    def remove_note(self, section: str, entry: str) -> bool:
        require_permission(self._permissions, "situation:write", self._plugin_id)
        if section not in ("master", "memory"):
            raise ValueError("section 必须为 master 或 memory")
        return self._require_builder().remove_entry(section, entry)


class WebHostProxy:
    """Web 宿主安全代理（受 web:routes 门控）：会话指纹、实时鉴权守卫、WS 广播与断线保活任务。"""

    def __init__(
        self,
        *,
        plugin_id: str,
        permissions: frozenset[str],
        web_runtime: dict[str, Any] | None = None,
    ):
        self._plugin_id = plugin_id
        self._permissions = permissions
        self._runtime = web_runtime if web_runtime is not None else {}
        self._local_tasks: set[asyncio.Task[Any]] = set()

    def _require_web(self) -> None:
        require_permission(self._permissions, "web:routes", self._plugin_id)

    def _require_settings(self):
        settings = self._runtime.get("settings")
        if settings is None:
            from web.settings import get_web_settings

            settings = get_web_settings()
        return settings

    def session_owner(self, request) -> str:
        """基于请求携带的 Bearer 与设备 Cookie 计算主会话指纹，隔离跨账号设备状态。"""
        self._require_web()
        from web.settings import DEVICE_COOKIE

        auth = request.headers.get("Authorization", "")
        cookie = request.cookies.get(DEVICE_COOKIE, "")
        return hashlib.sha256((auth + "\0" + cookie).encode("utf-8")).hexdigest()

    def make_session_guard(self, request) -> Callable[[], bool]:
        """构造实时守卫闭包，确保主密码或设备绑定失效后立即吊销端侧授权。"""
        self._require_web()
        from web.settings import DEVICE_COOKIE

        settings = self._require_settings()
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        cookie = request.cookies.get(DEVICE_COOKIE)
        password_enabled = settings.has_password

        def guard() -> bool:
            return (
                settings.has_password == password_enabled
                and (not settings.has_password or settings.valid_token(token))
                and (not settings.requires_device or settings.valid_device(cookie) is not None)
            )

        return guard

    async def broadcast(self, event: dict[str, Any]) -> None:
        """通过宿主 WebSocket 连接管理器推送事件（无监听器时静默跳过）。"""
        self._require_web()
        conn = self._runtime.get("conn_manager")
        send_fn = getattr(conn, "broadcast", None)
        if callable(send_fn):
            await send_fn(event)

    async def emit_ui_event(self, event: str, payload: Any = None) -> None:
        """向当前插件的沙箱 WebUI 推送实时事件（需 web:ui 或 web:routes 权限）。"""
        if "web:ui" not in self._permissions and "web:routes" not in self._permissions:
            require_permission(self._permissions, "web:ui", self._plugin_id)
        event_name = str(event or "").strip()
        if not event_name:
            raise ValueError("UI 事件名不能为空")
        conn = self._runtime.get("conn_manager")
        send_fn = getattr(conn, "broadcast", None)
        if callable(send_fn):
            await send_fn({
                "type": "plugin.ui_event",
                "data": {
                    "plugin_id": self._plugin_id,
                    "event": event_name,
                    "payload": payload,
                },
            })

    def retain_task(self, coro) -> asyncio.Task[Any]:
        """托管需要在 HTTP 连接关闭后继续完成的后台协程（随 Web 服务停机清理）。"""
        self._require_web()
        retain_fn = self._runtime.get("retain_task")
        if callable(retain_fn):
            return retain_fn(coro)
        task = asyncio.create_task(coro)
        self._local_tasks.add(task)
        task.add_done_callback(self._local_tasks.discard)
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task
