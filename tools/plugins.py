"""主 Agent 插件/MCP 查看与暂存工具（绝不直接启用代码或输出明文密钥）。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from config.store import ConfigStore
from plugins.installer import stage_local_plugin, stage_mcp_server
from plugins.manifest import bundled_plugins_dir, discover_plugin_dirs, parse_manifest
from plugins.mcp_client import parse_mcp_config, resolve_mcp_secrets
from plugins.permissions import describe_permissions
from runtime.paths import get_paths, state_path


logger = logging.getLogger(__name__)

_plugin_manager = None
_config_store: ConfigStore | None = None
_plugins_dir: Path | None = None


def set_plugin_manager(manager) -> None:
    """由启动加载器绑定运行中的 PluginManager。"""
    global _plugin_manager
    _plugin_manager = manager


def set_installer_deps(store: ConfigStore | None = None, plugins_dir: Path | None = None) -> None:
    """仅用于测试或显式注入配置存储与插件目录。"""
    global _config_store, _plugins_dir
    _config_store = store
    _plugins_dir = plugins_dir


TOOL_BINDINGS = {"plugin_manager": set_plugin_manager}


def plugins_list(args: dict) -> str:
    """列出已发现本地与内置插件、已配置 MCP 服务、运行状态及缺失环境变量名（绝不输出密钥值）。"""
    store = _config_store or ConfigStore()
    _, _, raw = store.read()
    plugins_dir = _plugins_dir or (_plugin_manager.plugins_dir if _plugin_manager is not None else state_path("plugins"))
    if _plugins_dir is not None:
        bundled_dir = _plugins_dir / "__disabled_bundled__"
    elif _plugin_manager is not None:
        bundled_dir = _plugin_manager.bundled_dir or (_plugins_dir or plugins_dir / "__disabled_bundled__")
    else:
        bundled_dir = bundled_plugins_dir()

    live_local = dict(getattr(_plugin_manager, "states", {}) or {})
    live_mcp = dict(getattr(_plugin_manager, "mcp_states", {}) or {})
    local_cfg = ((raw.get("plugins") or {}).get("items") or {})
    mcp_cfg = ((raw.get("plugins") or {}).get("mcp") or {})

    locals_out: list[dict[str, Any]] = []
    for pid, entry, is_bundled in discover_plugin_dirs(plugins_dir=plugins_dir, bundled_dir=bundled_dir):
        if pid in live_local:
            item = live_local[pid].to_dict()
            # 从不暴露具体密钥值，config 仅保留非秘密字段概要
            locals_out.append({
                "id": item["id"],
                "bundled": is_bundled,
                "status": item["status"],
                "enabled": item["enabled"],
                "trusted": item["trusted"],
                "tools": [t["name"] for t in item["tools"] if t.get("enabled")],
                "skills": item["skills"],
                "commands": item["commands"],
                "channels": item.get("channels", []),
                "missing_env": item["missing_env"],
                "permissions": (item.get("manifest") or {}).get("permissions", []),
                "error": item["error"],
            })
        else:
            try:
                manifest = parse_manifest(entry)
                cfg_item = local_cfg.get(pid) if isinstance(local_cfg.get(pid), dict) else {}
                missing = [v for v in manifest.requires_env if not os.getenv(v)]
                locals_out.append({
                    "id": pid,
                    "bundled": is_bundled,
                    "status": "pending",
                    "enabled": bool(cfg_item.get("enabled", False)),
                    "trusted": bool(cfg_item.get("trusted", False)),
                    "permissions": list(manifest.permissions),
                    "permission_details": describe_permissions(manifest.permissions),
                    "missing_env": missing,
                })
            except Exception as exc:
                locals_out.append({"id": pid, "bundled": is_bundled, "status": "degraded", "error": str(exc)})

    mcp_out: list[dict[str, Any]] = []
    for sid in sorted(set(live_mcp) | set(mcp_cfg)):
        if sid in live_mcp:
            item = live_mcp[sid].to_dict()
            mcp_out.append({
                "id": item["id"],
                "status": item["status"],
                "enabled": item["enabled"],
                "trusted": item["trusted"],
                "transport": item.get("transport"),
                "tools": [t["name"] for t in item["tools"] if t.get("enabled")],
                "missing_env": item["missing_env"],
                "error": item["error"],
            })
        else:
            try:
                parsed = parse_mcp_config(sid, mcp_cfg[sid])
                _, _, _, missing = resolve_mcp_secrets(parsed)
                mcp_out.append({
                    "id": sid,
                    "status": "pending" if not parsed.trusted else "disabled",
                    "enabled": parsed.enabled,
                    "trusted": parsed.trusted,
                    "transport": parsed.transport,
                    "missing_env": missing,
                })
            except Exception as exc:
                mcp_out.append({"id": sid, "status": "degraded", "error": str(exc)})

    return json.dumps({"local_plugins": locals_out, "mcp_servers": mcp_out}, ensure_ascii=False)


def plugin_install_local(args: dict) -> str:
    """校验并暂存本地插件（始终处于 pending / disabled 状态，需在 WebUI /plugins 审核启用）。"""
    plugin_id = args.get("plugin_id")
    files = args.get("files")
    source_path = args.get("source_path")
    overwrite = bool(args.get("overwrite", False))

    if not isinstance(plugin_id, str) or not plugin_id.strip():
        return json.dumps({"error": "必须提供 plugin_id"}, ensure_ascii=False)

    resolved_source: Path | None = None
    if source_path is not None:
        if not isinstance(source_path, str) or not source_path.strip():
            return json.dumps({"error": "source_path 必须为非空路径"}, ensure_ascii=False)
        workspace = get_paths().workspace.resolve()
        candidate = (workspace / source_path.strip()).resolve() if not Path(source_path).is_absolute() else Path(source_path).resolve()
        if not candidate.is_relative_to(workspace):
            return json.dumps({"error": "source_path 必须位于工作区 workspace 内"}, ensure_ascii=False)
        resolved_source = candidate

    try:
        result = stage_local_plugin(
            plugin_id=plugin_id.strip(),
            files=files,
            source_dir=resolved_source,
            overwrite=overwrite,
            plugins_dir=_plugins_dir,
            config_store=_config_store,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def mcp_stage_server(args: dict) -> str:
    """校验并暂存 MCP 服务器配置（始终以 enabled=false 写入，绝不写入明文密钥）。"""
    server_id = args.get("server_id")
    if not isinstance(server_id, str) or not server_id.strip():
        return json.dumps({"error": "必须提供 server_id"}, ensure_ascii=False)

    spec = {
        "transport": args.get("transport", "stdio"),
        "command": args.get("command", ""),
        "args": args.get("args") or [],
        "cwd": args.get("cwd"),
        "env": args.get("env") or {},
        "env_secrets": args.get("env_secrets") or {},
        "url": args.get("url", ""),
        "headers": args.get("headers") or {},
        "header_secrets": args.get("header_secrets") or {},
        "auth_bearer_env": args.get("auth_bearer_env"),
        "timeout_seconds": args.get("timeout_seconds", 30),
        "disabled_tools": args.get("disabled_tools") or [],
    }
    try:
        result = stage_mcp_server(
            server_id=server_id.strip(),
            spec=spec,
            overwrite=bool(args.get("overwrite", False)),
            config_store=_config_store,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="plugins_list",
        description="列出本地插件与 MCP 服务状态、申请权限、缺失环境变量及错误信息。",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        func=plugins_list,
        is_async=False,
    )
    registry.register(
        name="plugin_install_local",
        description="将工作区目录或内联文件暂存为待审核本地插件（总是写入 enabled:false）。",
        parameters={
            "type": "object",
            "properties": {
                "plugin_id": {"type": "string", "description": "插件唯一标识，与 manifest.id 及目录名一致。"},
                "source_path": {"type": "string", "description": "工作区内包含 manifest.yaml 的相对目录路径。"},
                "files": {
                    "type": "object",
                    "description": "直接脚手架生成的文件映射 {相对路径: UTF-8 文本内容}，与 source_path 二选一。",
                    "additionalProperties": {"type": "string"},
                },
                "overwrite": {"type": "boolean", "description": "是否允许覆盖同名已暂存插件（默认 false）。"},
            },
            "required": ["plugin_id"],
            "additionalProperties": False,
        },
        func=plugin_install_local,
        is_async=False,
    )
    registry.register(
        name="mcp_stage_server",
        description="暂存 MCP 服务配置到 config.yaml（仅引用 .env 变量名，禁填明文密钥；初始为待启用状态）。",
        parameters={
            "type": "object",
            "properties": {
                "server_id": {"type": "string", "description": "MCP 服务标识（小写字母/数字/_/-）。"},
                "transport": {"type": "string", "enum": ["stdio", "streamable_http"], "description": "传输协议。"},
                "command": {"type": "string", "description": "stdio 启动命令（如 npx / uvx / python）。"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "stdio 命令行参数列表。"},
                "cwd": {"type": "string", "description": "可选工作目录。"},
                "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "非敏感静态环境变量。"},
                "env_secrets": {
                    "type": "object",
                    "description": "子进程密钥环境变量映射 {子进程变量名: 宿主.env变量名}。",
                    "additionalProperties": {"type": "string"},
                },
                "url": {"type": "string", "description": "streamable_http 端点地址（http/https）。"},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}, "description": "非敏感 HTTP 头。"},
                "header_secrets": {"type": "object", "additionalProperties": {"type": "string"}, "description": "敏感头环境变量映射。"},
                "auth_bearer_env": {"type": "string", "description": "Bearer Token 对应的 .env 变量名。"},
                "timeout_seconds": {"type": "number", "description": "单次连接/工具调用超时秒数（默认 30）。"},
                "disabled_tools": {"type": "array", "items": {"type": "string"}, "description": "要禁用的工具名列表。"},
                "overwrite": {"type": "boolean", "description": "是否允许覆盖同名 MCP 配置（默认 false）。"},
            },
            "required": ["server_id", "transport"],
            "additionalProperties": False,
        },
        func=mcp_stage_server,
        is_async=False,
    )
