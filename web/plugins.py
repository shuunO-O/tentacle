"""WebUI 插件与 MCP 管理接口（/api/plugins/*，均受 DeviceAccessGate 与 auth_dep 保护）。"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from typing import Any

from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse

from config.environment import dotenv_update, updated_env_keys
from config.store import ConfigStore, ConfigurationConflict, ConfigurationError, Field, atomic_write, validate_value
from plugins.installer import remove_local_plugin
from plugins.manifest import PLUGIN_ID_RE, bundled_plugins_dir, discover_plugin_dirs, parse_manifest
from plugins.mcp_client import parse_mcp_config, resolve_mcp_secrets
from plugins.permissions import PERMISSION_CATALOG, PluginManifestError, describe_permissions
from plugins.ui_bundle import build_plugin_ui_document, resolve_ui_asset_file
from runtime.paths import state_path
from web.schemas import (
    BrowserLiveViewSummaryDto,
    ConfigurationRestartResponse,
    McpServerUpsertBody,
    PluginDecisionBody,
    PluginEnvWriteResponse,
    PluginRestartBody,
    PluginUiDocumentResponse,
    PluginUiListResponse,
    PluginsSnapshotResponse,
)


ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_local_dirs(plugin_manager) -> tuple[Path, Path]:
    if plugin_manager is not None:
        pdir = plugin_manager.plugins_dir
        bdir = plugin_manager.bundled_dir if plugin_manager.bundled_dir is not None else (pdir / "__disabled_bundled__")
        return pdir, bdir
    return state_path("plugins"), bundled_plugins_dir()


def _declared_env_vars(plugin_manager, saved_raw: dict[str, Any]) -> set[str]:
    allowed: set[str] = set()
    plugins_dir, bundled_dir = _resolve_local_dirs(plugin_manager)
    items_cfg = ((saved_raw.get("plugins") or {}).get("items") or {})
    for pid, entry, _is_bundled in discover_plugin_dirs(plugins_dir=plugins_dir, bundled_dir=bundled_dir):
        try:
            manifest = parse_manifest(entry)
        except Exception:
            continue
        allowed.update(manifest.requires_env)
        allowed.update(manifest.optional_env)
        saved_item = items_cfg.get(manifest.id) if isinstance(items_cfg.get(manifest.id), dict) else {}
        saved_cfg = saved_item.get("config") if isinstance(saved_item.get("config"), dict) else {}
        for key, spec in manifest.config_schema.items():
            if spec.type == "env":
                val = saved_cfg.get(key) if key in saved_cfg else spec.default
                if isinstance(val, str) and val.strip():
                    allowed.add(val.strip())

    mcp_cfg = ((saved_raw.get("plugins") or {}).get("mcp") or {})
    if isinstance(mcp_cfg, dict):
        for sid, entry in mcp_cfg.items():
            try:
                parsed = parse_mcp_config(sid, entry)
                allowed.update(parsed.referenced_env_vars())
            except Exception:
                continue
    return allowed


def _validate_plugin_config_input(manifest, incoming_config: dict[str, Any] | None) -> dict[str, Any]:
    if incoming_config is None:
        return {}
    if not isinstance(incoming_config, dict):
        raise ConfigurationError("插件 config 必须为对象")
    result: dict[str, Any] = {}
    for key, value in incoming_config.items():
        spec = manifest.config_schema.get(key)
        if spec is None:
            raise ConfigurationError(f"插件 {manifest.id} 不包含配置字段: {key}")
        field = Field(
            path=f"plugins.items.{manifest.id}.config.{key}",
            label=spec.label,
            kind=spec.type,
            default=spec.default,
            effect="restart",
            group=manifest.name,
            minimum=spec.minimum,
            maximum=spec.maximum,
            choices=spec.choices or (),
            nullable=spec.nullable,
            multiline=spec.multiline,
            help=spec.help,
        )
        validate_value(field, value)
        result[key] = value
    return result


def _merge_config_with_schema(manifest, cfg_dict: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, spec in manifest.config_schema.items():
        merged[key] = deepcopy(cfg_dict[key]) if key in cfg_dict else deepcopy(spec.default)
    for key, value in cfg_dict.items():
        if key not in merged:
            merged[key] = deepcopy(value)
    return merged


def _build_snapshot(
    *,
    plugin_manager,
    store: ConfigStore,
    config_service=None,
    env_changed: bool = False,
) -> dict[str, Any]:
    revision, _, saved = store.read()
    plugins_section = saved.get("plugins") or {}
    saved_items = plugins_section.get("items") or {}
    saved_mcp = plugins_section.get("mcp") or {}

    live_local = dict(getattr(plugin_manager, "states", {}) or {})
    live_mcp = dict(getattr(plugin_manager, "mcp_states", {}) or {})
    plugins_dir, bundled_dir = _resolve_local_dirs(plugin_manager)

    plugins_out: list[dict[str, Any]] = []
    for pid, entry, is_bundled in discover_plugin_dirs(plugins_dir=plugins_dir, bundled_dir=bundled_dir):
        saved_entry = saved_items.get(pid) if isinstance(saved_items.get(pid), dict) else {}
        saved_enabled = bool(saved_entry.get("enabled", False))
        saved_trusted = bool(saved_entry.get("trusted", False))
        saved_disabled = [str(x) for x in (saved_entry.get("disabled_tools") or []) if isinstance(x, str)]
        saved_cfg = deepcopy(saved_entry.get("config") or {}) if isinstance(saved_entry.get("config"), dict) else {}

        live_state = live_local.get(pid)
        try:
            manifest = parse_manifest(entry)
            merged_cfg = _merge_config_with_schema(manifest, saved_cfg)
            env_vars = list(dict.fromkeys([*manifest.requires_env, *manifest.optional_env]))
            required_vars = list(manifest.requires_env)
            for key, spec in manifest.config_schema.items():
                if spec.type == "env":
                    val = merged_cfg.get(key)
                    if isinstance(val, str) and val.strip():
                        vname = val.strip()
                        if vname not in env_vars:
                            env_vars.append(vname)
                        if spec.required and vname not in required_vars:
                            required_vars.append(vname)
            missing_env = [v for v in required_vars if not os.getenv(v)]

            if live_state is not None:
                base = live_state.to_dict()
                pending_restart = (
                    bool(live_state.enabled) != saved_enabled
                    or list(live_state.disabled_tools) != saved_disabled
                    or (live_state.config != merged_cfg if live_state.enabled or saved_enabled else False)
                )
                base.update({
                    "bundled": is_bundled,
                    "saved_enabled": saved_enabled,
                    "saved_trusted": saved_trusted,
                    "saved_config": merged_cfg,
                    "saved_disabled_tools": saved_disabled,
                    "missing_env": missing_env,
                    "env_vars": env_vars,
                    "env_status": {v: bool(os.getenv(v)) for v in env_vars},
                    "pending_restart": pending_restart,
                })
            else:
                base = {
                    "id": pid,
                    "kind": "local",
                    "enabled": saved_enabled,
                    "trusted": saved_trusted,
                    "bundled": is_bundled,
                    "status": "pending" if not saved_enabled and not saved_trusted else ("disabled" if not saved_enabled else "pending"),
                    "error": None,
                    "manifest": manifest.to_dict(),
                    "permission_details": describe_permissions(manifest.permissions),
                    "config": merged_cfg,
                    "disabled_tools": saved_disabled,
                    "saved_enabled": saved_enabled,
                    "saved_trusted": saved_trusted,
                    "saved_config": merged_cfg,
                    "saved_disabled_tools": saved_disabled,
                    "tools": [],
                    "commands": [],
                    "hooks": [],
                    "skills": [],
                    "channels": [],
                    "routes": [],
                    "ui": None,
                    "background_tasks": 0,
                    "missing_env": missing_env,
                    "env_vars": env_vars,
                    "env_status": {v: bool(os.getenv(v)) for v in env_vars},
                    "has_prompt": bool(manifest.prompt_file),
                    "pending_restart": saved_enabled,
                }
            plugins_out.append(base)
        except Exception as exc:
            plugins_out.append({
                "id": pid,
                "kind": "local",
                "enabled": saved_enabled,
                "trusted": saved_trusted,
                "bundled": is_bundled,
                "status": "degraded",
                "error": live_state.error if live_state and live_state.error else str(exc),
                "manifest": None,
                "permission_details": [],
                "config": saved_cfg,
                "disabled_tools": saved_disabled,
                "saved_enabled": saved_enabled,
                "saved_trusted": saved_trusted,
                "saved_config": saved_cfg,
                "saved_disabled_tools": saved_disabled,
                "tools": [],
                "commands": [],
                "hooks": [],
                "skills": [],
                "channels": [],
                "routes": [],
                "ui": None,
                "background_tasks": 0,
                "missing_env": [],
                "env_vars": [],
                "env_status": {},
                "has_prompt": False,
                "pending_restart": False,
            })

    mcp_out: list[dict[str, Any]] = []
    for sid in sorted(set(live_mcp) | set(saved_mcp)):
        saved_entry = saved_mcp.get(sid) if isinstance(saved_mcp.get(sid), dict) else None
        live_state = live_mcp.get(sid)
        if saved_entry is not None:
            try:
                parsed = parse_mcp_config(sid, saved_entry)
                _, _, env_vars, missing_env = resolve_mcp_secrets(parsed)
                if live_state is not None:
                    item = live_state.to_dict()
                    item.update({
                        "saved_config": deepcopy(saved_entry),
                        "missing_env": missing_env,
                        "env_vars": env_vars,
                        "env_status": {v: bool(os.getenv(v)) for v in env_vars},
                        "pending_restart": live_state.mcp_config != saved_entry,
                    })
                else:
                    item = {
                        "id": sid,
                        "kind": "mcp",
                        "enabled": parsed.enabled,
                        "trusted": parsed.trusted,
                        "status": "pending" if not parsed.trusted else "disabled",
                        "error": None,
                        "transport": parsed.transport,
                        "tools": [],
                        "disabled_tools": list(parsed.disabled_tools),
                        "missing_env": missing_env,
                        "env_vars": env_vars,
                        "env_status": {v: bool(os.getenv(v)) for v in env_vars},
                        "mcp_config": deepcopy(saved_entry),
                        "saved_config": deepcopy(saved_entry),
                        "pending_restart": parsed.enabled,
                    }
            except Exception as exc:
                item = {
                    "id": sid,
                    "kind": "mcp",
                    "enabled": bool(saved_entry.get("enabled", False)),
                    "trusted": bool(saved_entry.get("trusted", False)),
                    "status": "degraded",
                    "error": str(exc),
                    "transport": saved_entry.get("transport"),
                    "tools": [],
                    "disabled_tools": [],
                    "missing_env": [],
                    "env_vars": [],
                    "env_status": {},
                    "mcp_config": deepcopy(saved_entry),
                    "saved_config": deepcopy(saved_entry),
                    "pending_restart": False,
                }
            mcp_out.append(item)
        elif live_state is not None:
            item = live_state.to_dict()
            item["saved_config"] = None
            item["pending_restart"] = True
            item["env_status"] = {v: bool(os.getenv(v)) for v in item.get("env_vars", [])}
            mcp_out.append(item)

    env_dirty = bool(env_changed or getattr(config_service, "_environment_changed", False))
    restart_ctrl = getattr(config_service, "restart", None)
    restart_req = bool(
        env_dirty
        or any(item.get("pending_restart") for item in plugins_out)
        or any(item.get("pending_restart") for item in mcp_out)
    )
    return {
        "revision": revision,
        "plugins": plugins_out,
        "mcp_servers": mcp_out,
        "permission_catalog": PERMISSION_CATALOG,
        "restart_required": restart_req,
        "restart": restart_ctrl.snapshot() if restart_ctrl is not None else {"available": False, "state": "unavailable"},
    }


def plugins_router(plugin_manager, auth_dep, *, config_service=None, store: ConfigStore | None = None):
    """构建 /api/plugins/* 管理路由。"""
    router = APIRouter(prefix="/api/plugins", dependencies=[auth_dep])
    state_flags = {"env_changed": False}

    def _store() -> ConfigStore:
        return store or getattr(config_service, "store", None) or ConfigStore()

    def snapshot_payload() -> dict[str, Any]:
        return _build_snapshot(
            plugin_manager=plugin_manager,
            store=_store(),
            config_service=config_service,
            env_changed=state_flags["env_changed"],
        )

    def _map_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ConfigurationConflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, (ConfigurationError, PluginManifestError, ValueError)):
            return HTTPException(422, str(exc))
        if isinstance(exc, OSError):
            return HTTPException(503, "配置或插件目录操作失败，请稍后重试")
        raise exc

    @router.get("", response_model=PluginsSnapshotResponse, response_model_exclude_unset=True)
    async def get_plugins():
        return snapshot_payload()

    def _active_browser_live_view() -> BrowserLiveViewSummaryDto | None:
        if plugin_manager is None:
            return None
        states = getattr(plugin_manager, "states", None)
        if not isinstance(states, dict):
            return None
        browser_state = states.get("browser")
        if browser_state is None or getattr(browser_state, "status", None) != "active":
            return None
        cfg = getattr(browser_state, "config", None)
        if not isinstance(cfg, dict):
            return None
        raw_url = str(cfg.get("live_view_url") or "").strip()
        if not raw_url or any(ch in raw_url for ch in ("\r", "\n", "\x00")):
            return None
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        cdp_configured = bool(str(cfg.get("cdp_discovery_url") or "").strip()) or bool(cfg.get("neko_enabled", False))
        return BrowserLiveViewSummaryDto(
            live_view_url=raw_url,
            cdp_configured=cdp_configured,
            mode="cdp" if cdp_configured else "local",
        )

    @router.get("/ui", response_model=PluginUiListResponse)
    async def list_plugin_uis():
        if plugin_manager is None or not hasattr(plugin_manager, "list_active_uis"):
            return PluginUiListResponse(items=[], browser_live_view=_active_browser_live_view())
        return PluginUiListResponse(
            items=plugin_manager.list_active_uis(),
            browser_live_view=_active_browser_live_view(),
        )

    def _require_active_ui(plugin_id: str):
        if not PLUGIN_ID_RE.fullmatch(plugin_id):
            raise HTTPException(404, f"插件 {plugin_id} 不存在")
        if plugin_manager is None or not hasattr(plugin_manager, "get_active_ui"):
            raise HTTPException(404, f"插件 {plugin_id} 未提供自定义页面")
        found = plugin_manager.get_active_ui(plugin_id)
        if found is None:
            raise HTTPException(404, f"插件 {plugin_id} 未启用或未提供自定义页面")
        return found

    @router.get("/{plugin_id}/ui/document", response_model=PluginUiDocumentResponse)
    async def get_plugin_ui_document(plugin_id: str):
        ui_dir, ui_spec, state = _require_active_ui(plugin_id)
        try:
            doc_html = build_plugin_ui_document(ui_dir, ui_spec.entry)
        except Exception as exc:
            raise _map_error(exc) from None
        manifest = state.manifest
        return PluginUiDocumentResponse(
            plugin_id=plugin_id,
            name=manifest.name if manifest else plugin_id,
            version=manifest.version if manifest else "",
            title=ui_spec.title or (manifest.name if manifest else plugin_id),
            description=ui_spec.description,
            placement=ui_spec.placement,
            has_routes=bool(state.routes),
            html=doc_html,
        )

    @router.get("/{plugin_id}/ui/assets/{asset_path:path}")
    async def get_plugin_ui_asset(plugin_id: str, asset_path: str):
        ui_dir, _ui_spec, _state = _require_active_ui(plugin_id)
        try:
            target = resolve_ui_asset_file(ui_dir, asset_path)
        except PluginManifestError as exc:
            msg = str(exc)
            if "不存在" in msg:
                raise HTTPException(404, msg) from None
            raise HTTPException(400, msg) from None
        except OSError:
            raise HTTPException(503, "插件静态资源读取失败，请稍后重试") from None
        return FileResponse(str(target), headers={"X-Content-Type-Options": "nosniff"})

    @router.post("/local/{plugin_id}/decision", response_model=PluginsSnapshotResponse, response_model_exclude_unset=True)
    async def decide_local_plugin(plugin_id: str, body: PluginDecisionBody):
        plugins_dir, bundled_dir = _resolve_local_dirs(plugin_manager)
        pdir = next((p for pid, p, _ in discover_plugin_dirs(plugins_dir=plugins_dir, bundled_dir=bundled_dir) if pid == plugin_id), None)
        if pdir is None or not pdir.is_dir():
            raise HTTPException(404, f"插件 {plugin_id} 不存在")
        try:
            manifest = parse_manifest(pdir)
            _, _, saved = _store().read()
            prev_item = ((saved.get("plugins") or {}).get("items") or {}).get(plugin_id)
            prev_item = prev_item if isinstance(prev_item, dict) else {}

            if body.config is not None:
                validated_cfg = _validate_plugin_config_input(manifest, body.config)
            else:
                validated_cfg = deepcopy(prev_item.get("config") or {})

            if body.disabled_tools is not None:
                if len(set(body.disabled_tools)) != len(body.disabled_tools):
                    raise ConfigurationError("disabled_tools 不能包含重复工具名")
                disabled_tools = list(body.disabled_tools)
            else:
                disabled_tools = [str(x) for x in (prev_item.get("disabled_tools") or [])]

            enabled = bool(body.enabled)
            trusted = True if enabled else (bool(body.trusted) if body.trusted is not None else bool(prev_item.get("trusted", False)))
            entry = {
                "enabled": enabled,
                "trusted": trusted,
                "disabled_tools": disabled_tools,
                "config": validated_cfg,
            }
            _store().upsert_plugin_entry("items", plugin_id, entry, revision=body.revision)
            return snapshot_payload()
        except Exception as exc:
            raise _map_error(exc) from None

    @router.delete("/local/{plugin_id}", response_model=PluginsSnapshotResponse, response_model_exclude_unset=True)
    async def delete_local_plugin_route(plugin_id: str, revision: str | None = None, delete_files: bool = False):
        try:
            plugins_dir, bundled_dir = _resolve_local_dirs(plugin_manager)
            user_dir = (plugins_dir / plugin_id).resolve()
            bundled_target = (bundled_dir / plugin_id).resolve()
            if delete_files and not user_dir.exists() and bundled_target.is_dir():
                raise ValueError(f"内置插件 {plugin_id} 不可删除文件，请直接停用")
            remove_local_plugin(
                plugin_id,
                delete_files=delete_files,
                plugins_dir=plugins_dir,
                config_store=_store(),
                revision=revision,
            )
            return snapshot_payload()
        except Exception as exc:
            raise _map_error(exc) from None

    @router.post("/mcp/{server_id}", response_model=PluginsSnapshotResponse, response_model_exclude_unset=True)
    async def upsert_mcp_server(server_id: str, body: McpServerUpsertBody):
        try:
            cfg = parse_mcp_config(server_id, body.config)
            normalized = {
                "enabled": cfg.enabled,
                "trusted": cfg.trusted or cfg.enabled,
                "transport": cfg.transport,
                "command": cfg.command,
                "args": list(cfg.args),
                "cwd": cfg.cwd,
                "env": dict(cfg.env),
                "env_secrets": dict(cfg.env_secrets),
                "url": cfg.url,
                "headers": dict(cfg.headers),
                "header_secrets": dict(cfg.header_secrets),
                "auth_bearer_env": cfg.auth_bearer_env,
                "timeout_seconds": cfg.timeout_seconds,
                "disabled_tools": list(cfg.disabled_tools),
            }
            if cfg.transport == "stdio":
                for k in ("url", "headers", "header_secrets", "auth_bearer_env"):
                    if not normalized[k]:
                        normalized.pop(k)
            else:
                for k in ("command", "args", "cwd", "env", "env_secrets"):
                    if not normalized[k]:
                        normalized.pop(k)
            _store().upsert_plugin_entry("mcp", server_id, normalized, revision=body.revision)
            return snapshot_payload()
        except Exception as exc:
            raise _map_error(exc) from None

    @router.delete("/mcp/{server_id}", response_model=PluginsSnapshotResponse, response_model_exclude_unset=True)
    async def delete_mcp_server(server_id: str, revision: str | None = None):
        try:
            _store().delete_plugin_entry("mcp", server_id, revision=revision)
            if plugin_manager is not None:
                _, _, saved = _store().read()
                await plugin_manager.reconnect_mcp(server_id, saved)
            return snapshot_payload()
        except Exception as exc:
            raise _map_error(exc) from None

    @router.post("/mcp/{server_id}/reconnect", response_model=PluginsSnapshotResponse, response_model_exclude_unset=True)
    async def reconnect_mcp_server(server_id: str):
        if plugin_manager is None:
            raise HTTPException(503, "当前实例未装配 PluginManager")
        try:
            _, _, saved = _store().read()
            mcp_map = ((saved.get("plugins") or {}).get("mcp") or {})
            if server_id not in mcp_map and server_id not in plugin_manager.mcp_states:
                raise HTTPException(404, f"MCP 服务 {server_id} 不存在")
            await plugin_manager.reconnect_mcp(server_id, saved)
            return snapshot_payload()
        except HTTPException:
            raise
        except Exception as exc:
            raise _map_error(exc) from None

    @router.post("/env", response_model=PluginEnvWriteResponse)
    async def write_plugin_env(request: Request):
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 65536:
                raise HTTPException(413, "凭据请求过大")
            payload.extend(chunk)
        try:
            body = json.loads(payload)
            if (
                not isinstance(body, dict)
                or set(body) != {"name", "value"}
                or not isinstance(body["name"], str)
                or not isinstance(body["value"], str)
                or not ENV_KEY_RE.fullmatch(body["name"].strip())
            ):
                raise ValueError
        except (ValueError, RecursionError):
            raise HTTPException(422, "环境变量写入请求格式无效") from None

        var_name = body["name"].strip()
        value = body["value"]
        active_store = _store()
        _, _, saved = active_store.read()
        allowed = _declared_env_vars(plugin_manager, saved)
        if var_name not in allowed:
            raise HTTPException(422, f"环境变量 {var_name} 未在任何已暂存或已配置的插件/MCP 清单中声明")

        dotenv_path = active_store.path.with_name(".env")
        try:
            content = dotenv_update(dotenv_path, var_name, value)
            atomic_write(dotenv_path, content)
        except Exception as exc:
            raise _map_error(exc) from None

        os.environ[var_name] = value
        updated_env_keys.add(var_name)
        state_flags["env_changed"] = True
        if config_service is not None and hasattr(config_service, "_environment_changed"):
            config_service._environment_changed = True
        # 绝不回显或记录 value
        return PluginEnvWriteResponse(ok=True, name=var_name, configured=bool(value))

    @router.post("/restart", response_model=ConfigurationRestartResponse, response_model_exclude_unset=True)
    async def restart_after_plugin_change(body: PluginRestartBody, background: BackgroundTasks):
        if config_service is None or getattr(config_service, "restart", None) is None:
            raise HTTPException(503, "当前实例未装配重启控制器")
        try:
            await config_service.prepare_restart(body.revision, False)
        except Exception as exc:
            raise _map_error(exc) from None
        if not config_service.restart.reserve(None):
            raise HTTPException(409, "应用正在准备重启")
        background.add_task(config_service.restart.request)
        return config_service.restart.snapshot()

    return router
