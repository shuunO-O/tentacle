"""Synthetic plugins & MCP management state for dev preview; never reads .env or TENTACLE_HOME."""

from copy import deepcopy

from fastapi import Depends, HTTPException, Request

from plugins.manifest import bundled_plugins_dir, parse_manifest
from plugins.permissions import PERMISSION_CATALOG, describe_permissions


def _make_bundled_item(plugin_id: str, *, tools: list[dict], routes: list[str] | None = None) -> tuple[dict, list[str]]:
    manifest = parse_manifest(bundled_plugins_dir() / plugin_id)
    defaults = {k: deepcopy(spec.default) for k, spec in manifest.config_schema.items()}
    env_vars = list(dict.fromkeys([*manifest.requires_env, *manifest.optional_env]))
    return (
        {
            "id": manifest.id,
            "kind": "local",
            "bundled": True,
            "enabled": True,
            "trusted": True,
            "saved_enabled": True,
            "saved_trusted": True,
            "status": "active",
            "error": None,
            "manifest": manifest.to_dict(),
            "permission_details": describe_permissions(manifest.permissions),
            "config": deepcopy(defaults),
            "saved_config": deepcopy(defaults),
            "disabled_tools": [],
            "saved_disabled_tools": [],
            "tools": tools,
            "commands": [],
            "hooks": [],
            "skills": [],
            "channels": [],
            "routes": list(routes or []),
            "background_tasks": 0,
            "missing_env": [],
            "env_vars": env_vars,
            "env_status": {name: False for name in env_vars},
            "has_prompt": bool(manifest.prompt_file),
            "pending_restart": False,
        },
        env_vars,
    )


def attach_plugins(app, authenticated):
    async def auth(request: Request):
        if not authenticated(request):
            raise HTTPException(401, "预览身份验证")

    # 从内置清单构造内置插件示例（不读取任何 .env 或用户 config.yaml）
    browser_item, browser_envs = _make_bundled_item(
        "browser",
        tools=[{"raw_name": "browser", "name": "p_browser__browser", "enabled": True}],
    )
    harmony_item, harmony_envs = _make_bundled_item("harmony", tools=[])
    harmony_item.update({
        "enabled": False,
        "saved_enabled": False,
        "status": "disabled",
        "routes": [],
    })
    qqbot_item, qqbot_envs = _make_bundled_item("qqbot", tools=[])
    qqbot_item.update({
        "enabled": False,
        "saved_enabled": False,
        "status": "disabled",
        "channels": [],
    })
    env_values = {name: False for name in ["WEATHER_API_KEY", *browser_envs, *harmony_envs, *qqbot_envs]}

    state = {
        "revision": "preview-plugins-rev-1",
        "restart_required": False,
        "restart": {"available": True, "state": "idle"},
        "env_values": env_values,
        "plugins": [
            browser_item,
            harmony_item,
            qqbot_item,
            {
                "id": "weather-brief",
                "kind": "local",
                "bundled": False,
                "enabled": False,
                "trusted": False,
                "saved_enabled": False,
                "saved_trusted": False,
                "status": "pending",
                "error": None,
                "manifest": {
                    "id": "weather-brief",
                    "name": "天气简报示例（待审核）",
                    "version": "1.2.0",
                    "description": "合成演示插件：展示权限审核、环境变量录入与配置表单。",
                    "permissions": ["tools:register", "hooks:mutate", "network:outbound"],
                    "requires_env": ["WEATHER_API_KEY"],
                    "config_schema": {
                        "city": {"key": "city", "label": "默认城市", "type": "string", "default": "上海"},
                        "days": {"key": "days", "label": "预报天数", "type": "integer", "default": 3, "minimum": 1, "maximum": 7},
                    },
                },
                "permission_details": describe_permissions(["tools:register", "hooks:mutate", "network:outbound"]),
                "config": {"city": "上海", "days": 3},
                "saved_config": {"city": "上海", "days": 3},
                "disabled_tools": [],
                "saved_disabled_tools": [],
                "tools": [],
                "commands": [],
                "hooks": [],
                "skills": ["weather-forecast"],
                "channels": [],
                "routes": [],
                "background_tasks": 0,
                "missing_env": ["WEATHER_API_KEY"],
                "env_vars": ["WEATHER_API_KEY"],
                "env_status": {"WEATHER_API_KEY": False},
                "has_prompt": False,
                "pending_restart": False,
            },
        ],
        "mcp_servers": [
            {
                "id": "github",
                "kind": "mcp",
                "enabled": True,
                "trusted": True,
                "status": "active",
                "error": None,
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "tools": [
                    {"raw_name": "search_repositories", "name": "mcp_github__search_repositories", "enabled": True},
                    {"raw_name": "get_issue", "name": "mcp_github__get_issue", "enabled": True},
                ],
                "env_vars": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
                "missing_env": [],
                "env_status": {"GITHUB_PERSONAL_ACCESS_TOKEN": True},
                "mcp_config": {
                    "enabled": True,
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env_secrets": {"GITHUB_TOKEN": "GITHUB_PERSONAL_ACCESS_TOKEN"},
                    "timeout_seconds": 30,
                    "disabled_tools": [],
                },
                "saved_config": {
                    "enabled": True,
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env_secrets": {"GITHUB_TOKEN": "GITHUB_PERSONAL_ACCESS_TOKEN"},
                    "timeout_seconds": 30,
                    "disabled_tools": [],
                },
            },
        ],
    }

    def snapshot():
        if app.state.scenario == "error":
            raise HTTPException(503, "合成测试：插件状态不可用")
        if app.state.scenario == "empty":
            return {
                "revision": state["revision"],
                "plugins": [],
                "mcp_servers": [],
                "permission_catalog": PERMISSION_CATALOG,
                "restart_required": False,
                "restart": deepcopy(state["restart"]),
            }
        return {
            "revision": state["revision"],
            "plugins": deepcopy(state["plugins"]),
            "mcp_servers": deepcopy(state["mcp_servers"]),
            "permission_catalog": PERMISSION_CATALOG,
            "restart_required": state["restart_required"],
            "restart": deepcopy(state["restart"]),
        }

    def bump_revision():
        num = int(state["revision"].rsplit("-", 1)[-1]) + 1
        state["revision"] = f"preview-plugins-rev-{num}"

    @app.get("/api/plugins", dependencies=[Depends(auth)])
    async def plugins_list():
        return snapshot()

    @app.post("/api/plugins/env", dependencies=[Depends(auth)])
    async def plugins_set_env(body: dict):
        name = str(body.get("name", ""))
        if name not in state["env_values"]:
            raise HTTPException(422, f"未在插件或 MCP 清单中声明环境变量: {name}")
        state["env_values"][name] = True
        for p in state["plugins"]:
            if name in p["env_vars"]:
                p["env_status"][name] = True
                p["missing_env"] = [k for k in p["env_vars"] if not p["env_status"].get(k)]
        state["restart_required"] = True
        bump_revision()
        return {"ok": True, "name": name, "configured": True}

    @app.post("/api/plugins/local/{plugin_id}/decision", dependencies=[Depends(auth)])
    async def plugins_decision(plugin_id: str, body: dict):
        item = next((p for p in state["plugins"] if p["id"] == plugin_id), None)
        if item is None:
            raise HTTPException(404, "插件不存在")
        if "enabled" in body:
            item["saved_enabled"] = bool(body["enabled"])
        if "trusted" in body:
            item["saved_trusted"] = bool(body["trusted"])
        if isinstance(body.get("config"), dict):
            item["saved_config"] = {**item["saved_config"], **body["config"]}
            item["config"] = deepcopy(item["saved_config"])
        if isinstance(body.get("disabled_tools"), list):
            item["saved_disabled_tools"] = list(body["disabled_tools"])
            item["disabled_tools"] = list(body["disabled_tools"])
        item["pending_restart"] = True
        state["restart_required"] = True
        bump_revision()
        return snapshot()

    @app.delete("/api/plugins/local/{plugin_id}", dependencies=[Depends(auth)])
    async def plugins_delete(plugin_id: str, delete_files: bool = False):
        item = next((p for p in state["plugins"] if p["id"] == plugin_id), None)
        if item is None:
            raise HTTPException(404, "插件不存在")
        if delete_files and item.get("bundled"):
            raise HTTPException(422, "内置插件随程序打包分发，不能删除源码目录；如需停用请关闭启用开关")
        state["plugins"] = [p for p in state["plugins"] if p["id"] != plugin_id]
        state["restart_required"] = True
        bump_revision()
        return snapshot()

    @app.post("/api/plugins/mcp/{server_id}", dependencies=[Depends(auth)])
    async def plugins_mcp_upsert(server_id: str, body: dict):
        cfg = body.get("config") if isinstance(body.get("config"), dict) else {}
        existing = next((s for s in state["mcp_servers"] if s["id"] == server_id), None)
        entry = {
            "id": server_id,
            "kind": "mcp",
            "enabled": bool(cfg.get("enabled", True)),
            "trusted": True,
            "status": "active" if cfg.get("enabled", True) else "disabled",
            "error": None,
            "transport": cfg.get("transport", "stdio"),
            "command": cfg.get("command"),
            "args": list(cfg.get("args") or []),
            "tools": existing["tools"] if existing else [],
            "env_vars": [],
            "missing_env": [],
            "env_status": {},
            "mcp_config": deepcopy(cfg),
            "saved_config": deepcopy(cfg),
        }
        if existing is None:
            state["mcp_servers"].append(entry)
        else:
            existing.update(entry)
        bump_revision()
        return snapshot()

    @app.post("/api/plugins/mcp/{server_id}/reconnect", dependencies=[Depends(auth)])
    async def plugins_mcp_reconnect(server_id: str):
        existing = next((s for s in state["mcp_servers"] if s["id"] == server_id), None)
        if existing is None:
            raise HTTPException(404, "MCP 服务不存在")
        existing["status"] = "active"
        existing["error"] = None
        return snapshot()

    @app.delete("/api/plugins/mcp/{server_id}", dependencies=[Depends(auth)])
    async def plugins_mcp_delete(server_id: str):
        state["mcp_servers"] = [s for s in state["mcp_servers"] if s["id"] != server_id]
        bump_revision()
        return snapshot()

    @app.post("/api/plugins/restart", dependencies=[Depends(auth)])
    async def plugins_restart():
        state["restart_required"] = False
        for p in state["plugins"]:
            p["enabled"] = p["saved_enabled"]
            p["trusted"] = p["saved_trusted"]
            p["pending_restart"] = False
            p["status"] = "active" if p["enabled"] and not p["missing_env"] else ("disabled" if not p["enabled"] else "pending")
        return {"pending": False, "state": "idle"}

    @app.get("/api/plugins/ext/{_rest:path}", dependencies=[Depends(auth)])
    async def plugins_ext_preview():
        return {"preview": True}
