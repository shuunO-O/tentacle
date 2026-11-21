"""Tentacle 插件管理器：发现、配置解析、隔离加载、失败降级与卸载。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import importlib.util
import inspect
import logging
import os
from pathlib import Path
import sys
from typing import Any

from agent.commands import command_registry as global_command_registry
from plugins.context import PluginContext
from plugins.hooks import hook_bus as global_hook_bus
from plugins.manifest import (
    PluginManifest,
    bundled_plugins_dir,
    discover_plugin_dirs,
    parse_manifest,
    qualify_tool_name,
    resolve_plugin_subpath,
)
from plugins.mcp_client import (
    MCPConnection,
    MCPServerConfig,
    _normalize_tool_schema,
    parse_mcp_config,
    resolve_mcp_secrets,
)
from plugins.permissions import describe_permissions
from runtime.paths import state_path
from runtime.tool_registry import registry as global_tool_registry
from tools import send_message as send_message_tool
from tools import skills as skills_tool


logger = logging.getLogger(__name__)


def _sanitize_error(exc: Exception, secret_keys: set[str] | tuple[str, ...] = ()) -> str:
    """格式化异常信息并脱敏任何已知凭据值。"""
    msg = f"{type(exc).__name__}: {exc}".strip()
    for key in secret_keys:
        val = os.getenv(key)
        if val and len(val) >= 4:
            msg = msg.replace(val, "***")
    return msg


def _merge_plugin_config(manifest: PluginManifest, raw_config: dict[str, Any]) -> dict[str, Any]:
    """合并 manifest.config_schema 默认值与 plugins.items.<id>.config 用户覆盖。"""
    items = (raw_config.get("plugins") or {}).get("items") or {}
    entry = items.get(manifest.id)
    user_cfg = (entry.get("config") or {}) if isinstance(entry, dict) else {}
    merged: dict[str, Any] = {}
    for key, spec in manifest.config_schema.items():
        if key in user_cfg:
            merged[key] = deepcopy(user_cfg[key])
        else:
            merged[key] = deepcopy(spec.default)
    for key, value in user_cfg.items():
        if key not in merged:
            merged[key] = deepcopy(value)
    return merged


def _collect_declared_env_vars(manifest: PluginManifest, config: dict[str, Any]) -> tuple[list[str], list[str]]:
    """返回 `(所有声明环境变量名, 所有必填且当前缺失的变量名)`。"""
    all_vars: list[str] = list(manifest.requires_env)
    for name in manifest.optional_env:
        if name not in all_vars:
            all_vars.append(name)
    required_vars: list[str] = list(manifest.requires_env)
    for key, spec in manifest.config_schema.items():
        if spec.type == "env":
            val = config.get(key)
            if isinstance(val, str) and val.strip():
                env_name = val.strip()
                if env_name not in all_vars:
                    all_vars.append(env_name)
                if spec.required and env_name not in required_vars:
                    required_vars.append(env_name)
    missing = [name for name in required_vars if not os.getenv(name)]
    return all_vars, missing


@dataclass
class PluginState:
    """单个本地插件或 MCP 服务的运行期与审核状态快照。"""

    id: str
    kind: str = "local"
    enabled: bool = False
    trusted: bool = False
    status: str = "pending"  # pending | disabled | active | degraded
    error: str | None = None
    manifest: PluginManifest | None = None
    config: dict[str, Any] = field(default_factory=dict)
    disabled_tools: list[str] = field(default_factory=list)
    tool_records: list[dict[str, Any]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    ui: dict[str, Any] | None = None
    background_tasks: int = 0
    missing_env: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    has_prompt: bool = False
    bundled: bool = False
    transport: str | None = None
    mcp_config: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "enabled": self.enabled,
            "trusted": self.trusted,
            "bundled": self.bundled,
            "status": self.status,
            "error": self.error,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "permission_details": describe_permissions(self.manifest.permissions) if self.manifest else [],
            "config": deepcopy(self.config),
            "disabled_tools": list(self.disabled_tools),
            "tools": deepcopy(self.tool_records),
            "commands": list(self.commands),
            "hooks": list(self.hooks),
            "skills": list(self.skills),
            "channels": list(self.channels),
            "routes": list(self.routes),
            "ui": deepcopy(self.ui) if self.ui is not None else None,
            "background_tasks": self.background_tasks,
            "missing_env": list(self.missing_env),
            "env_vars": list(self.env_vars),
            "has_prompt": self.has_prompt,
            "transport": self.transport,
            "mcp_config": deepcopy(self.mcp_config) if self.mcp_config is not None else None,
        }


class PluginManager:
    """统一管理本地插件与 MCP 服务：发现、权限门控、隔离启动与卸载。"""

    def __init__(
        self,
        *,
        plugins_dir: Path | None = None,
        bundled_dir: Path | None = None,
        data_dir: Path | None = None,
        tool_registry=None,
        command_registry=None,
        hook_bus=None,
        services: dict[str, Any] | None = None,
        mcp_transport_factory=None,
    ):
        self.plugins_dir = plugins_dir or state_path("plugins")
        if bundled_dir is not None:
            self.bundled_dir: Path | None = bundled_dir
        elif plugins_dir is None:
            self.bundled_dir = bundled_plugins_dir()
        else:
            # 显式传入测试隔离 plugins_dir 时默认不混入真实仓库内置插件，除非显式传入 bundled_dir
            self.bundled_dir = None
        self.data_dir = data_dir or (state_path("data") / "plugins")
        self.tool_registry = tool_registry or global_tool_registry
        self.command_registry = command_registry or global_command_registry
        self.hook_bus = hook_bus or global_hook_bus
        self._services = dict(services or {})
        self._services.setdefault("web_runtime", {})
        self._mcp_transport_factory = mcp_transport_factory
        self.states: dict[str, PluginState] = {}
        self.mcp_states: dict[str, PluginState] = {}
        self._active_runtimes: dict[str, dict[str, Any]] = {}
        self._active_channels: dict[str, tuple[str, Any, Any]] = {}  # platform -> (plugin_id, sender, event_cb)
        self._http_chat_adapters: list[tuple[str, Any]] = []
        self._mcp_connections: dict[str, MCPConnection] = {}
        self._prompt_builder = self._services.get("builder")
        self._event_fanout = self._services.get("event_fanout")

    async def startup(self, raw_config: dict[str, Any] | None = None) -> dict[str, PluginState]:
        """扫描并启动所有已启用插件与 MCP 服务；单个失败仅降级自身。"""
        if raw_config is None:
            from config.manager import ConfigManager

            raw_config = ConfigManager().snapshot()
        await self.shutdown()

        plugins_section = (raw_config or {}).get("plugins") or {}
        items_cfg = plugins_section.get("items") or {}
        missing_bundled = self.bundled_dir if self.bundled_dir is not None else self.plugins_dir / "__disabled_bundled__"
        for _pid, entry_dir, is_bundled in discover_plugin_dirs(plugins_dir=self.plugins_dir, bundled_dir=missing_bundled):
            await self._startup_local_entry(entry_dir, items_cfg, raw_config, bundled=is_bundled)

        mcp_cfg_map = plugins_section.get("mcp") or {}
        if isinstance(mcp_cfg_map, dict):
            for server_id in sorted(mcp_cfg_map):
                await self._startup_mcp_entry(server_id, mcp_cfg_map[server_id])

        self.bind_extensions(self._prompt_builder)
        return self.states

    async def _startup_local_entry(
        self,
        entry_dir: Path,
        items_cfg: dict[str, Any],
        raw_config: dict[str, Any],
        *,
        bundled: bool = False,
    ) -> None:
        item_cfg = items_cfg.get(entry_dir.name)
        enabled = bool(item_cfg.get("enabled", False)) if isinstance(item_cfg, dict) else False
        trusted = bool(item_cfg.get("trusted", False)) if isinstance(item_cfg, dict) else False
        disabled_tools = (
            [str(x) for x in (item_cfg.get("disabled_tools") or []) if isinstance(x, str)]
            if isinstance(item_cfg, dict)
            else []
        )

        try:
            manifest = parse_manifest(entry_dir)
        except Exception as exc:
            self.states[entry_dir.name] = PluginState(
                id=entry_dir.name,
                kind="local",
                enabled=enabled,
                trusted=trusted,
                bundled=bundled,
                status="degraded",
                error=_sanitize_error(exc),
                disabled_tools=disabled_tools,
            )
            logger.warning(f"插件清单校验失败 [{entry_dir.name}]: {exc}")
            return

        config = _merge_plugin_config(manifest, raw_config)
        env_vars, missing_env = _collect_declared_env_vars(manifest, config)
        state = PluginState(
            id=manifest.id,
            kind="local",
            enabled=enabled,
            trusted=trusted,
            bundled=bundled,
            status="pending" if not enabled and not trusted else ("disabled" if not enabled else "active"),
            manifest=manifest,
            config=config,
            disabled_tools=disabled_tools,
            missing_env=missing_env,
            env_vars=env_vars,
        )
        self.states[manifest.id] = state

        if not enabled:
            return

        if missing_env:
            state.status = "degraded"
            state.error = f"缺少必要环境变量: {', '.join(missing_env)}"
            logger.warning(f"插件启动降级 [{manifest.id}]: {state.error}")
            return

        await self._activate_local_plugin(state, manifest, entry_dir)

    async def _startup_mcp_entry(self, server_id: str, raw_entry: object) -> PluginState:
        raw_copy = deepcopy(raw_entry) if isinstance(raw_entry, dict) else None
        enabled = bool(raw_entry.get("enabled", False)) if isinstance(raw_entry, dict) else False
        trusted = bool(raw_entry.get("trusted", False)) if isinstance(raw_entry, dict) else False
        try:
            cfg = parse_mcp_config(server_id, raw_entry)
        except Exception as exc:
            state = PluginState(
                id=str(server_id),
                kind="mcp",
                enabled=enabled,
                trusted=trusted,
                status="degraded",
                error=_sanitize_error(exc),
                mcp_config=raw_copy,
            )
            self.mcp_states[str(server_id)] = state
            logger.warning(f"MCP 配置校验失败 [{server_id}]: {exc}")
            return state

        extra_env, extra_headers, env_vars, missing_env = resolve_mcp_secrets(cfg)
        state = PluginState(
            id=cfg.id,
            kind="mcp",
            enabled=cfg.enabled,
            trusted=cfg.trusted,
            status="pending" if not cfg.enabled and not cfg.trusted else ("disabled" if not cfg.enabled else "active"),
            disabled_tools=list(cfg.disabled_tools),
            missing_env=missing_env,
            env_vars=env_vars,
            transport=cfg.transport,
            mcp_config=raw_copy,
        )
        self.mcp_states[cfg.id] = state

        if not cfg.enabled:
            return state

        if missing_env:
            state.status = "degraded"
            state.error = f"缺少必要环境变量: {', '.join(missing_env)}"
            logger.warning(f"MCP 启动降级 [{cfg.id}]: {state.error}")
            return state

        await self._connect_mcp_server(state, cfg, extra_env, extra_headers)
        return state

    async def _connect_mcp_server(
        self,
        state: PluginState,
        cfg: MCPServerConfig,
        extra_env: dict[str, str],
        extra_headers: dict[str, str],
    ) -> None:
        source_tag = f"mcp:{cfg.id}"
        conn = MCPConnection(
            cfg,
            extra_env=extra_env,
            extra_headers=extra_headers,
            transport_factory=self._mcp_transport_factory,
        )
        disabled_set = set(cfg.disabled_tools)
        try:
            tools = await conn.connect()
            for mcp_tool in tools:
                qualified = qualify_tool_name("mcp", cfg.id, mcp_tool.name)
                is_disabled = mcp_tool.name in disabled_set or qualified in disabled_set
                description = str(getattr(mcp_tool, "description", None) or mcp_tool.name)
                schema = _normalize_tool_schema(getattr(mcp_tool, "inputSchema", None))
                if not is_disabled:
                    raw_tool_name = mcp_tool.name

                    async def _executor(args: dict[str, Any], _conn=conn, _name=raw_tool_name):
                        return await _conn.call_tool(_name, args)

                    self.tool_registry.register(
                        qualified,
                        description,
                        schema,
                        _executor,
                        is_async=True,
                        source=source_tag,
                        auto_expose=True,
                    )
                state.tool_records.append({
                    "raw_name": mcp_tool.name,
                    "name": qualified,
                    "description": description,
                    "enabled": not is_disabled,
                    "auto_expose": not is_disabled,
                })
            self._mcp_connections[cfg.id] = conn
            state.status = "active"
            state.error = None
            logger.info(
                f"MCP 服务已连接 [{cfg.id} via {cfg.transport}]: "
                f"tools={sum(1 for t in state.tool_records if t['enabled'])}/{len(state.tool_records)}"
            )
        except Exception as exc:
            self.tool_registry.unregister_by_source(source_tag)
            await conn.close()
            state.tool_records.clear()
            state.status = "degraded"
            state.error = _sanitize_error(exc, state.env_vars)
            logger.error(f"MCP 服务连接失败已降级 [{cfg.id}]: {state.error}", exc_info=True)

    async def reconnect_mcp(self, server_id: str, raw_config: dict[str, Any] | None = None) -> PluginState | None:
        """热重连单个 MCP 服务器（断开旧连接、注销旧工具后按最新配置重新挂载）。"""
        if raw_config is None:
            from config.manager import ConfigManager

            raw_config = ConfigManager().snapshot()
        old_conn = self._mcp_connections.pop(server_id, None)
        self.tool_registry.unregister_by_source(f"mcp:{server_id}")
        if old_conn is not None:
            await old_conn.close()

        mcp_map = ((raw_config or {}).get("plugins") or {}).get("mcp") or {}
        if server_id not in mcp_map:
            self.mcp_states.pop(server_id, None)
            return None
        return await self._startup_mcp_entry(server_id, mcp_map[server_id])

    async def _activate_local_plugin(
        self,
        state: PluginState,
        manifest: PluginManifest,
        plugin_dir: Path,
    ) -> None:
        source_tag = f"plugin:{manifest.id}"
        plugin_channels: list[tuple[str, Any, Any]] = []
        plugin_routers: list[Any] = []
        plugin_adapters: list[Any] = []
        plugin_tasks: list[Any] = []

        def _register_channel(platform: str, sender, event_cb, meta: dict[str, Any] | None = None) -> None:
            if platform in self._active_channels:
                raise ValueError(f"消息平台 {platform!r} 已被插件 {self._active_channels[platform][0]} 占用")
            meta = meta or {}
            send_message_tool.register_sender(
                platform,
                sender,
                description=str(meta.get("description") or ""),
                supports_target=bool(meta.get("supports_target", False)),
                target_description=str(meta.get("target_description") or ""),
                registry=self.tool_registry,
            )
            if self._event_fanout is not None and event_cb is not None and hasattr(self._event_fanout, "add"):
                self._event_fanout.add(event_cb)
            self._active_channels[platform] = (manifest.id, sender, event_cb)
            plugin_channels.append((platform, sender, event_cb))
            state.channels.append(platform)

        def _register_router(router) -> None:
            plugin_routers.append(router)
            for r in getattr(router, "routes", ()):
                path = getattr(r, "path", "")
                if path:
                    state.routes.append(f"/api/plugins/ext/{manifest.id}{path}")

        def _register_http_chat_adapter(adapter) -> None:
            plugin_adapters.append(adapter)
            self._http_chat_adapters.append((manifest.id, adapter))

        def _track_task(task) -> None:
            plugin_tasks.append(task)
            state.background_tasks = len(plugin_tasks)

        def _on_task_failed(exc: Exception) -> None:
            state.status = "degraded"
            state.error = _sanitize_error(exc, state.env_vars)

        ctx = PluginContext(
            manifest=manifest,
            plugin_dir=plugin_dir,
            data_dir=self.data_dir / manifest.id,
            config=state.config,
            disabled_tools=set(state.disabled_tools),
            tool_registry=self.tool_registry,
            command_registry=self.command_registry,
            hook_bus=self.hook_bus,
            services=self._services,
            on_tool_recorded=state.tool_records.append,
            on_command_recorded=state.commands.append,
            on_hook_recorded=state.hooks.append,
            on_channel_registered=_register_channel,
            on_router_registered=_register_router,
            on_http_chat_adapter_registered=_register_http_chat_adapter,
            on_task_created=_track_task,
            on_task_failed=_on_task_failed,
        )
        module = None
        skill_dir: Path | None = None
        prompt_fragment: str | None = None
        ui_dir: Path | None = None

        try:
            if manifest.skills_dir:
                skill_dir = resolve_plugin_subpath(plugin_dir, manifest.skills_dir, allow_dir=True, allow_file=False)
                for item in sorted(skill_dir.iterdir()):
                    if item.is_dir() and (item / "SKILL.md").is_file():
                        state.skills.append(item.name)

            if manifest.prompt_file:
                prompt_path = resolve_plugin_subpath(plugin_dir, manifest.prompt_file, allow_dir=False, allow_file=True)
                fragment = prompt_path.read_text(encoding="utf-8").strip()
                if fragment:
                    prompt_fragment = fragment
                    state.has_prompt = True

            if manifest.ui is not None and "web:ui" in manifest.permissions:
                ui_dir = resolve_plugin_subpath(plugin_dir, manifest.ui.dir, allow_dir=True, allow_file=False)
                resolve_plugin_subpath(ui_dir, manifest.ui.entry, allow_dir=False, allow_file=True)
                state.ui = manifest.ui.to_dict()

            if manifest.entrypoint:
                module, teardown_fn = await self._load_and_setup(plugin_dir, manifest, ctx)
            else:
                teardown_fn = None

            self._active_runtimes[manifest.id] = {
                "ctx": ctx,
                "module": module,
                "teardown": teardown_fn,
                "skill_dir": skill_dir,
                "prompt_fragment": prompt_fragment,
                "ui_dir": ui_dir,
                "ui_spec": manifest.ui if ui_dir is not None else None,
                "channels": plugin_channels,
                "routers": plugin_routers,
                "http_chat_adapters": plugin_adapters,
                "tasks": plugin_tasks,
            }
            state.status = "active"
            logger.info(
                f"插件已启用 [{manifest.id} v{manifest.version}]: "
                f"tools={sum(1 for t in state.tool_records if t['enabled'])}, "
                f"commands={len(state.commands)}, skills={len(state.skills)}"
            )
        except Exception as exc:
            await self._cleanup_plugin_runtime(manifest.id, plugin_channels, plugin_tasks)
            self._http_chat_adapters = [item for item in self._http_chat_adapters if item[0] != manifest.id]
            self.tool_registry.unregister_by_source(source_tag)
            self.command_registry.unregister_by_source(source_tag)
            self.hook_bus.unregister_by_source(source_tag)
            self._remove_plugin_modules(manifest.id)
            state.tool_records.clear()
            state.commands.clear()
            state.hooks.clear()
            state.skills.clear()
            state.channels.clear()
            state.routes.clear()
            state.ui = None
            state.background_tasks = 0
            state.has_prompt = False
            state.status = "degraded"
            state.error = _sanitize_error(exc, state.env_vars)
            logger.error(f"插件启动失败已降级 [{manifest.id}]: {state.error}", exc_info=True)

    async def _cleanup_plugin_runtime(
        self,
        plugin_id: str,
        channels: list[tuple[str, Any, Any]],
        tasks: list[Any],
    ) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            import asyncio

            await asyncio.gather(*tasks, return_exceptions=True)
        for platform, sender, event_cb in channels:
            if self._active_channels.get(platform, (None,))[0] == plugin_id:
                self._active_channels.pop(platform, None)
            send_message_tool.unregister_sender(platform, sender, registry=self.tool_registry)
            if self._event_fanout is not None and event_cb is not None and hasattr(self._event_fanout, "remove"):
                self._event_fanout.remove(event_cb)

    async def _load_and_setup(
        self,
        plugin_dir: Path,
        manifest: PluginManifest,
        ctx: PluginContext,
    ) -> tuple[Any, Any]:
        assert manifest.entrypoint is not None
        if ":" in manifest.entrypoint:
            rel_path, func_name = manifest.entrypoint.split(":", 1)
        else:
            rel_path, func_name = manifest.entrypoint, "setup"
        entry_path = resolve_plugin_subpath(plugin_dir, rel_path, allow_dir=False, allow_file=True)
        import types

        root_pkg_name = "tentacle_plugins"
        if root_pkg_name not in sys.modules:
            root_pkg = types.ModuleType(root_pkg_name)
            root_pkg.__path__ = []
            sys.modules[root_pkg_name] = root_pkg

        plugin_pkg_name = f"{root_pkg_name}.{manifest.id}"
        plugin_pkg = types.ModuleType(plugin_pkg_name)
        plugin_pkg.__path__ = [str(plugin_dir)]
        plugin_pkg.__package__ = plugin_pkg_name
        sys.modules[plugin_pkg_name] = plugin_pkg

        module_name = f"{plugin_pkg_name}.{entry_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件入口: {manifest.entrypoint}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = plugin_pkg_name
        sys.modules[module_name] = module

        plugin_root_str = str(plugin_dir)
        inserted = False
        if plugin_root_str not in sys.path:
            sys.path.insert(0, plugin_root_str)
            inserted = True
        try:
            spec.loader.exec_module(module)
            setup_fn = getattr(module, func_name, None)
            if not callable(setup_fn):
                raise AttributeError(f"插件入口缺少可调用函数 {func_name}(ctx)")
            result = setup_fn(ctx)
            if inspect.isawaitable(result):
                await result
            teardown_fn = getattr(module, "teardown", None)
            return module, teardown_fn if callable(teardown_fn) else None
        finally:
            if inserted and plugin_root_str in sys.path:
                sys.path.remove(plugin_root_str)

    def _remove_plugin_modules(self, plugin_id: str) -> None:
        prefix = f"tentacle_plugins.{plugin_id}."
        for mod_name in [k for k in sys.modules if k == f"tentacle_plugins.{plugin_id}" or k.startswith(prefix)]:
            sys.modules.pop(mod_name, None)

    def bind_extensions(self, builder=None) -> None:
        """将当前已激活插件的 skills 与 prompt 片段绑定到系统提示词和 skills 工具。"""
        if builder is not None:
            self._prompt_builder = builder
        skill_dirs: list[Path] = []
        guild_fragments: list[tuple[str, str]] = []
        for plugin_id, rt in self._active_runtimes.items():
            if rt.get("skill_dir") is not None:
                skill_dirs.append(rt["skill_dir"])
            if rt.get("prompt_fragment"):
                guild_fragments.append((plugin_id, rt["prompt_fragment"]))
        skills_tool.set_plugin_skills_dirs(skill_dirs)
        if self._prompt_builder is not None and hasattr(self._prompt_builder, "set_plugin_extensions"):
            self._prompt_builder.set_plugin_extensions(
                skill_dirs=skill_dirs,
                guild_fragments=guild_fragments,
            )

    async def shutdown(self) -> None:
        """停止并卸载所有已激活插件与 MCP 连接，释放工具、命令、Skills 与 Prompt 注入。"""
        for server_id in list(reversed(list(self._mcp_connections))):
            conn = self._mcp_connections.pop(server_id)
            self.tool_registry.unregister_by_source(f"mcp:{server_id}")
            try:
                await conn.close()
            except Exception as exc:
                logger.warning(f"MCP 关闭异常 [{server_id}]: {exc}", exc_info=True)

        for plugin_id in list(reversed(list(self._active_runtimes))):
            rt = self._active_runtimes.pop(plugin_id)
            teardown = rt.get("teardown")
            ctx = rt.get("ctx")
            if callable(teardown):
                try:
                    result = teardown(ctx)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    logger.warning(f"插件卸载异常 [{plugin_id}]: {exc}", exc_info=True)
            await self._cleanup_plugin_runtime(plugin_id, rt.get("channels") or [], rt.get("tasks") or [])
            source_tag = f"plugin:{plugin_id}"
            self.tool_registry.unregister_by_source(source_tag)
            self.command_registry.unregister_by_source(source_tag)
            self.hook_bus.unregister_by_source(source_tag)
            self._remove_plugin_modules(plugin_id)

        skills_tool.set_plugin_skills_dirs([])
        if self._prompt_builder is not None and hasattr(self._prompt_builder, "clear_plugin_extensions"):
            self._prompt_builder.clear_plugin_extensions()
        self._http_chat_adapters.clear()
        self.states.clear()
        self.mcp_states.clear()

    def bind_web_runtime(self, *, settings=None, conn_manager=None, retain_task=None) -> None:
        """在 FastAPI 创建后把实时鉴权设置、WS 广播与受监管后台任务槽注入 WebHostProxy。"""
        runtime = self._services.setdefault("web_runtime", {})
        if settings is not None:
            runtime["settings"] = settings
        if conn_manager is not None:
            runtime["conn_manager"] = conn_manager
        if retain_task is not None:
            runtime["retain_task"] = retain_task

    def get_http_chat_adapters(self) -> list[Any]:
        """返回已激活插件注册的 HTTP /api/chat/send 多端回合适配器列表。"""
        return [adapter for _pid, adapter in self._http_chat_adapters]

    def bind_event_fanout(self, fanout) -> None:
        """把当前已注册的插件事件回调挂载到事件扇出器。"""
        self._event_fanout = fanout
        if fanout is None or not hasattr(fanout, "add"):
            return
        for _platform, (_pid, _sender, event_cb) in self._active_channels.items():
            if event_cb is not None:
                fanout.add(event_cb)

    def get_extension_routers(self) -> list[tuple[str, Any]]:
        """返回已激活插件注册的 `(plugin_id, APIRouter)` 列表，供 server 挂载受保护路由。"""
        routers: list[tuple[str, Any]] = []
        for plugin_id, rt in self._active_runtimes.items():
            for router in rt.get("routers") or ():
                routers.append((plugin_id, router))
        return routers

    def get_active_ui(self, plugin_id: str) -> tuple[Path, Any, PluginState] | None:
        """返回已激活且具备 web:ui 权限的插件 `(ui_dir, ui_spec, state)`；未启用/未信任返回 None。"""
        rt = self._active_runtimes.get(plugin_id)
        state = self.states.get(plugin_id)
        if rt is None or state is None or state.status != "active":
            return None
        ui_dir = rt.get("ui_dir")
        ui_spec = rt.get("ui_spec")
        if ui_dir is None or ui_spec is None:
            return None
        return ui_dir, ui_spec, state

    def list_active_uis(self) -> list[dict[str, Any]]:
        """返回所有已激活且提供自定义沙箱页面的插件清单。"""
        items: list[dict[str, Any]] = []
        for plugin_id in sorted(self._active_runtimes):
            found = self.get_active_ui(plugin_id)
            if found is None:
                continue
            _ui_dir, ui_spec, state = found
            manifest = state.manifest
            items.append({
                "plugin_id": plugin_id,
                "name": manifest.name if manifest else plugin_id,
                "version": manifest.version if manifest else "",
                "title": ui_spec.title or (manifest.name if manifest else plugin_id),
                "description": ui_spec.description,
                "placement": ui_spec.placement,
                "has_routes": bool(state.routes),
            })
        return items

    def list_plugins(self) -> list[dict[str, Any]]:
        return [state.to_dict() for state in self.states.values()]

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        return [state.to_dict() for state in self.mcp_states.values()]
