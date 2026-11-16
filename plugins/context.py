"""插件运行时上下文（PluginContext）：按 manifest 权限门控宿主能力。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import logging
import os
from pathlib import Path
import re
from typing import Any, Callable

from plugins.manifest import PluginManifest, qualify_tool_name
from plugins.permissions import PermissionDeniedError, require_permission
from tools.send_message import callable_accepts_target, invoke_sender
from plugins.services import (
    GoalsProxy,
    MemoryProxy,
    SchedulerProxy,
    SituationProxy,
    WebHostProxy,
    WorkspaceFilesProxy,
)


class PluginContext:
    """注入给每个本地插件 `setup(ctx)` 的受控上下文对象。"""

    def __init__(
        self,
        *,
        manifest: PluginManifest,
        plugin_dir: Path,
        data_dir: Path,
        config: dict[str, Any],
        disabled_tools: set[str],
        tool_registry,
        command_registry,
        hook_bus=None,
        services: dict[str, Any] | None = None,
        on_tool_recorded: Callable[[dict[str, Any]], None] | None = None,
        on_command_recorded: Callable[[str], None] | None = None,
        on_hook_recorded: Callable[[str], None] | None = None,
        on_channel_registered: Callable[[str, Callable, Callable | None], None] | None = None,
        on_router_registered: Callable[[Any], None] | None = None,
        on_http_chat_adapter_registered: Callable[[Any], None] | None = None,
        on_task_created: Callable[[asyncio.Task], None] | None = None,
        on_task_failed: Callable[[Exception], None] | None = None,
    ):
        self.plugin_id = manifest.id
        self.manifest = manifest
        self.plugin_dir = plugin_dir
        self._data_dir = data_dir
        self._config = deepcopy(config)
        self._permissions = frozenset(manifest.permissions)
        self._disabled_tools = frozenset(disabled_tools)
        self._tool_registry = tool_registry
        self._command_registry = command_registry
        self._hook_bus = hook_bus
        self._services = dict(services or {})
        self._on_tool_recorded = on_tool_recorded
        self._on_command_recorded = on_command_recorded
        self._on_hook_recorded = on_hook_recorded
        self._on_channel_registered = on_channel_registered
        self._on_router_registered = on_router_registered
        self._on_http_chat_adapter_registered = on_http_chat_adapter_registered
        self._on_task_created = on_task_created
        self._on_task_failed = on_task_failed
        self._owned_channels: set[str] = set()
        self.logger = logging.getLogger(f"tentacle.plugin.{manifest.id}")

        self.files = WorkspaceFilesProxy(
            plugin_id=self.plugin_id,
            permissions=self._permissions,
            workspace_root=self._services.get("workspace"),
            file_manager=self._services.get("file_manager"),
        )
        self.memory = MemoryProxy(
            plugin_id=self.plugin_id,
            permissions=self._permissions,
            memory_store=self._services.get("memory_store"),
            chronicle=self._services.get("chronicle"),
        )
        self.goals = GoalsProxy(
            plugin_id=self.plugin_id,
            permissions=self._permissions,
            goal_manager=self._services.get("goal_manager"),
        )
        self.scheduler = SchedulerProxy(
            plugin_id=self.plugin_id,
            permissions=self._permissions,
            scheduler=self._services.get("scheduler"),
        )
        self.situation = SituationProxy(
            plugin_id=self.plugin_id,
            permissions=self._permissions,
            builder=self._services.get("builder"),
        )
        self.web = WebHostProxy(
            plugin_id=self.plugin_id,
            permissions=self._permissions,
            web_runtime=self._services.get("web_runtime"),
        )

    @property
    def config(self) -> dict[str, Any]:
        """返回插件配置副本，避免误修改全局配置快照。"""
        return deepcopy(self._config)

    @property
    def data_dir(self) -> Path:
        """插件私有数据目录 `data/plugins/<id>/`（需 fs:plugin_data）。"""
        self.require("fs:plugin_data")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir

    def has_permission(self, permission: str) -> bool:
        return permission in self._permissions

    def require(self, permission: str) -> None:
        require_permission(self._permissions, permission, self.plugin_id)

    def data_path(self, relative: str = "") -> Path:
        """在插件私有数据目录内解析子路径，阻止目录穿越。"""
        base = self.data_dir.resolve()
        if not relative or not relative.strip():
            return base
        rel = relative.strip().replace("\\", "/")
        if rel.startswith("/") or "\x00" in rel:
            raise PermissionDeniedError(f"插件 {self.plugin_id} 数据路径非法: {relative!r}")
        target = (base / rel).resolve()
        if not target.is_relative_to(base):
            raise PermissionDeniedError(f"插件 {self.plugin_id} 数据路径越界: {relative!r}")
        return target

    def get_env(self, var_name: str, default: str | None = None) -> str | None:
        """读取插件 manifest 声明过的环境变量（禁止越权读取宿主其他凭据）。"""
        allowed = set(self.manifest.requires_env) | set(self.manifest.optional_env)
        for key, spec in self.manifest.config_schema.items():
            if spec.type == "env":
                val = self._config.get(key)
                if isinstance(val, str) and val.strip():
                    allowed.add(val.strip())
        if var_name not in allowed:
            raise PermissionDeniedError(
                f"插件 {self.plugin_id} 未在 manifest.yaml 中声明环境变量 {var_name}"
            )
        value = os.getenv(var_name)
        return value if value not in (None, "") else default

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable,
        *,
        is_async: bool | None = None,
        auto_expose: bool = True,
    ) -> str:
        """注册插件工具，自动施加 `p_<id>__<name>` 命名空间并遵守 disabled_tools。"""
        self.require("tools:register")
        qualified = qualify_tool_name("p", self.plugin_id, name)
        disabled = name in self._disabled_tools or qualified in self._disabled_tools
        if not disabled:
            self._tool_registry.register(
                qualified,
                description,
                parameters,
                func,
                is_async=is_async,
                source=f"plugin:{self.plugin_id}",
                auto_expose=auto_expose,
            )
        if self._on_tool_recorded is not None:
            self._on_tool_recorded({
                "raw_name": name,
                "name": qualified,
                "description": description,
                "enabled": not disabled,
                "auto_expose": bool(auto_expose and not disabled),
            })
        return qualified

    def register_command(
        self,
        name: str,
        description: str,
        handler: Callable,
    ) -> str:
        """注册自定义斜杠命令（不可覆盖内置命令）。"""
        self.require("commands:register")
        registered_name = self._command_registry.register(
            name,
            description,
            handler,
            source=f"plugin:{self.plugin_id}",
        )
        if self._on_command_recorded is not None:
            self._on_command_recorded(registered_name)
        return registered_name

    def on(self, event: str, handler: Callable[[dict[str, Any]], Any], *, priority: int = 100) -> None:
        """订阅生命周期钩子（需 hooks:observe 或 hooks:mutate；仅 hooks:mutate 可注入/拦截/改写）。"""
        if not (self.has_permission("hooks:observe") or self.has_permission("hooks:mutate")):
            raise PermissionDeniedError(
                f"插件 {self.plugin_id} 缺少权限 hooks:observe 或 hooks:mutate"
            )
        if self._hook_bus is None:
            raise RuntimeError("HookBus 尚未初始化")
        self._hook_bus.register(
            event,
            handler,
            source=f"plugin:{self.plugin_id}",
            can_mutate=self.has_permission("hooks:mutate"),
            priority=priority,
        )
        if self._on_hook_recorded is not None:
            self._on_hook_recorded(event)

    def register_channel(
        self,
        platform: str,
        *,
        sender: Callable[..., Any],
        event_callback: Callable[[Any], Any] | None = None,
        description: str = "",
        supports_target: bool = False,
        target_description: str = "",
    ) -> str:
        """注册新的双向消息平台通道（写入 send_message 工具表并可选订阅事件扇出）。"""
        self.require("channels:register")
        cleaned = str(platform or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", cleaned):
            raise ValueError(f"消息平台标识无效: {platform!r}")
        if cleaned in {"web", "system"}:
            raise ValueError(f"禁止覆盖保留平台: {cleaned!r}")
        if not callable(sender):
            raise ValueError("sender 必须为可调用对象")
        if event_callback is not None and not callable(event_callback):
            raise ValueError("event_callback 必须为可调用对象")
        if self._on_channel_registered is None:
            raise RuntimeError("通道注册回调未绑定")

        accepts_target = callable_accepts_target(sender)
        if supports_target and not accepts_target:
            raise ValueError("声明 supports_target=True 时，sender 必须接受 target 参数")
        resolved_supports_target = bool(supports_target) or accepts_target

        if resolved_supports_target:
            async def _wrapped_sender(text: str, target: str | None = None) -> dict[str, Any]:
                return await invoke_sender(
                    sender,
                    text,
                    target=target,
                    accepts_target=True,
                )
        else:
            async def _wrapped_sender(text: str) -> dict[str, Any]:
                return await invoke_sender(
                    sender,
                    text,
                    target=None,
                    accepts_target=False,
                )

        self._on_channel_registered(
            cleaned,
            _wrapped_sender,
            event_callback,
            {
                "description": str(description or "").strip(),
                "supports_target": resolved_supports_target,
                "target_description": str(target_description or "").strip(),
            },
        )
        self._owned_channels.add(cleaned)
        return cleaned

    async def emit_user_message(
        self,
        user_input: str,
        *,
        platform: str,
        user_id: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> Any:
        """把插件通道收到的用户输入转发给唯一 AgentSession 执行一轮对话。"""
        self.require("channels:register")
        cleaned = str(platform or "").strip().lower()
        if cleaned not in self._owned_channels:
            raise PermissionDeniedError(f"插件 {self.plugin_id} 未注册消息平台 {cleaned!r}")
        agent_session = self._services.get("agent_session")
        if agent_session is None:
            raise RuntimeError("AgentSession 尚未就绪")
        kwargs: dict[str, Any] = {"platform": cleaned}
        if user_id is not None:
            kwargs["user_id"] = user_id
        if images is not None:
            kwargs["images"] = images
        return await agent_session.run_round(user_input, **kwargs)

    def register_router(self, router: Any) -> None:
        """挂载受 DeviceAccessGate 与登录鉴权保护的后端 API 子路由。"""
        self.require("web:routes")
        from fastapi import APIRouter

        if not isinstance(router, APIRouter):
            raise ValueError("router 必须为 fastapi.APIRouter 实例")
        if self._on_router_registered is None:
            raise RuntimeError("Web 路由注册器未绑定")
        self._on_router_registered(router)

    def register_http_chat_adapter(self, adapter: Any) -> None:
        """注册 HTTP /api/chat/send 多端回合适配器（需同时声明 web:routes 与 hooks:mutate）。"""
        self.require("web:routes")
        self.require("hooks:mutate")
        if not callable(getattr(adapter, "prepare", None)):
            raise ValueError("adapter 必须提供可调用的 prepare 方法")
        if self._on_http_chat_adapter_registered is None:
            raise RuntimeError("HTTP 聊天适配器注册器未绑定")
        self._on_http_chat_adapter_registered(adapter)

    def create_background_task(
        self,
        coro_or_factory: Any,
        *,
        name: str = "",
    ) -> asyncio.Task:
        """在宿主受监管下创建后台长任务（停机或卸载时统一取消回收）。"""
        self.require("background:task")
        if callable(coro_or_factory):
            coro = coro_or_factory()
        else:
            coro = coro_or_factory
        if not inspect.iscoroutine(coro):
            raise ValueError("create_background_task 需要协程或无参协程工厂")

        async def _supervised():
            try:
                return await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error(f"插件后台任务异常 [{self.plugin_id}]: {exc}", exc_info=True)
                if self._on_task_failed is not None:
                    self._on_task_failed(exc)
                return None

        task_name = f"plugin-{self.plugin_id}:{name}" if name else f"plugin-{self.plugin_id}:task"
        task = asyncio.create_task(_supervised(), name=task_name)
        if self._on_task_created is not None:
            self._on_task_created(task)
        return task
