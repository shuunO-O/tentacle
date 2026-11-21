"""Synthetic plugins & MCP management state for dev preview; never reads .env or TENTACLE_HOME."""

import base64
from copy import deepcopy
import io
import random
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw

from plugins.manifest import bundled_plugins_dir, parse_manifest
from plugins.permissions import PERMISSION_CATALOG, describe_permissions
from plugins.ui_bundle import SANDBOX_CSP, build_plugin_ui_document


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
            "ui": manifest.ui.to_dict() if manifest.ui else None,
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
        routes=[
            "/api/plugins/ext/browser/status",
            "/api/plugins/ext/browser/stream/connect",
            "/api/plugins/ext/browser/stream/disconnect",
            "/api/plugins/ext/browser/stream/heartbeat",
            "/api/plugins/ext/browser/input",
            "/api/plugins/ext/browser/action",
        ],
    )
    harmony_item, harmony_envs = _make_bundled_item("harmony", tools=[])
    harmony_item.update({
        "enabled": False,
        "saved_enabled": False,
        "status": "disabled",
        "routes": [],
    })
    proton_item, proton_envs = _make_bundled_item("proton_mail", tools=[])
    proton_item.update({
        "enabled": False,
        "saved_enabled": False,
        "status": "disabled",
    })
    qqbot_item, qqbot_envs = _make_bundled_item("qqbot", tools=[])
    qqbot_item.update({
        "enabled": False,
        "saved_enabled": False,
        "status": "disabled",
        "channels": [],
    })
    env_values = {name: False for name in ["WEATHER_API_KEY", *browser_envs, *harmony_envs, *proton_envs, *qqbot_envs]}

    state = {
        "revision": "preview-plugins-rev-1",
        "restart_required": False,
        "restart": {"available": True, "state": "idle"},
        "env_values": env_values,
        "plugins": [
            {
                "id": "demo-viz",
                "kind": "local",
                "bundled": False,
                "enabled": True,
                "trusted": True,
                "saved_enabled": True,
                "saved_trusted": True,
                "status": "active",
                "error": None,
                "manifest": {
                    "id": "demo-viz",
                    "name": "交互看板示例",
                    "version": "1.0.0",
                    "description": "合成演示插件：展示独立源沙箱 iframe、主题同步、受限 API 代理与 WS 实时推送。",
                    "permissions": ["web:ui", "web:routes"],
                    "requires_env": [],
                    "ui": {
                        "dir": "ui",
                        "entry": "index.html",
                        "title": "交互看板示例",
                        "description": "点击节点可预填对话，支持暗/浅主题切换与实时事件推送",
                        "placement": "sidebar",
                    },
                    "config_schema": {},
                },
                "permission_details": describe_permissions(["web:ui", "web:routes"]),
                "config": {},
                "saved_config": {},
                "disabled_tools": [],
                "saved_disabled_tools": [],
                "tools": [],
                "commands": [],
                "hooks": [],
                "skills": [],
                "channels": [],
                "routes": [
                    "/api/plugins/ext/demo-viz/series",
                    "/api/plugins/ext/demo-viz/push-event",
                ],
                "ui": {
                    "dir": "ui",
                    "entry": "index.html",
                    "title": "交互看板示例",
                    "description": "点击节点可预填对话，支持暗/浅主题切换与实时事件推送",
                    "placement": "sidebar",
                },
                "background_tasks": 0,
                "missing_env": [],
                "env_vars": [],
                "env_status": {},
                "has_prompt": False,
                "pending_restart": False,
            },
            browser_item,
            harmony_item,
            proton_item,
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

    demo_series = [
        {"label": "09:00", "value": 42, "note": "早间平稳"},
        {"label": "10:00", "value": 68, "note": "检索活跃"},
        {"label": "11:00", "value": 54, "note": "任务回落"},
        {"label": "12:00", "value": 81, "note": "峰值波动"},
        {"label": "13:00", "value": 63, "note": "午后常态"},
        {"label": "14:00", "value": 92, "note": "高负载样本"},
    ]

    demo_ui_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="{SANDBOX_CSP}">
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 20px;
      background: var(--t-bg);
      color: var(--t-fg);
      font-family: var(--t-font-sans);
    }}
    .wrap {{ max-width: 920px; margin: 0 auto; display: grid; gap: 16px; }}
    .card {{
      background: var(--t-bg-surface);
      border: 1px solid var(--t-border);
      border-radius: 16px;
      padding: 16px 18px;
    }}
    .row {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }}
    h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
    p {{ margin: 4px 0 0; font-size: 12px; color: var(--t-fg-dim); }}
    .btns {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    button {{
      cursor: pointer;
      border: 1px solid var(--t-border);
      background: var(--t-bg-elevated);
      color: var(--t-fg);
      border-radius: 10px;
      padding: 6px 12px;
      font-size: 12px;
      font-weight: 500;
    }}
    button:hover {{ background: var(--t-bg-hover); }}
    button.primary {{
      background: var(--t-accent);
      border-color: var(--t-accent);
      color: var(--t-accent-fg);
    }}
    button.primary:hover {{ background: var(--t-accent-hover); }}
    svg.chart {{ width: 100%; height: 240px; display: block; }}
    .node {{ cursor: pointer; transition: transform 0.12s ease; }}
    .node:hover {{ filter: brightness(1.15); }}
    .grid-2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
    .kv {{ font-family: var(--t-font-mono); font-size: 11px; line-height: 1.7; color: var(--t-fg-dim); }}
    .pill {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      background: var(--t-accent-soft);
      color: var(--t-accent);
    }}
    .ok {{ background: var(--t-ok-soft); color: var(--t-ok); }}
    .log {{
      max-height: 120px;
      overflow: auto;
      font-family: var(--t-font-mono);
      font-size: 11px;
      color: var(--t-fg-dim);
      background: var(--t-code-bg);
      border: 1px solid var(--t-border-soft);
      border-radius: 10px;
      padding: 10px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="row">
        <div>
          <h1>插件自定义交互看板 <span class="pill" id="theme-pill">theme</span></h1>
          <p>所有数据请求经 <code>window.tentacle.fetch()</code> 走宿主代理至 <code>/api/plugins/ext/demo-viz/*</code>；点击任意数据点可向主对话预填提问。</p>
        </div>
        <div class="btns">
          <button class="primary" id="btn-add">+ 新增数据点</button>
          <button id="btn-push">模拟 WS 实时推送</button>
          <button id="btn-asset">读取静态资源</button>
          <button id="btn-probe">验证沙箱阻断直连</button>
        </div>
      </div>
    </div>

    <div class="card">
      <svg class="chart" id="chart" viewBox="0 0 760 240" preserveAspectRatio="none"></svg>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="row" style="margin-bottom:8px;">
          <strong style="font-size:13px;">沙箱安全自检</strong>
          <span class="pill ok">独立源隔离</span>
        </div>
        <div class="kv" id="sandbox-info">检测中…</div>
      </div>
      <div class="card">
        <div class="row" style="margin-bottom:8px;">
          <strong style="font-size:13px;">交互与实时事件日志</strong>
        </div>
        <div class="log" id="event-log">等待交互…</div>
      </div>
    </div>
  </div>

  <script>
    const logEl = document.getElementById("event-log");
    const themePill = document.getElementById("theme-pill");
    const chartEl = document.getElementById("chart");
    const sandboxInfo = document.getElementById("sandbox-info");
    const logs = [];

    function pushLog(text) {{
      logs.unshift("[" + new Date().toLocaleTimeString() + "] " + text);
      logEl.textContent = logs.slice(0, 12).join("\\n");
    }}

    function updateThemeBadge(theme) {{
      themePill.textContent = "主题: " + theme;
    }}
    updateThemeBadge(window.tentacle.theme);
    window.tentacle.onThemeChange((theme) => {{
      updateThemeBadge(theme);
      pushLog("宿主切换主题 -> " + theme);
    }});

    window.tentacle.onEvent((event, payload) => {{
      pushLog("收到 WS 推送 " + event + ": " + JSON.stringify(payload));
      window.tentacle.toast("插件收到实时事件: " + event, "ok");
      loadSeries();
    }});

    (function checkSandbox() {{
      let storageBlocked = false;
      try {{ void window.localStorage.getItem("tentacle_token"); }} catch (_e) {{ storageBlocked = true; }}
      let parentBlocked = false;
      try {{ void window.parent.document.title; }} catch (_e) {{ parentBlocked = true; }}
      sandboxInfo.innerHTML =
        "pluginId: " + window.tentacle.pluginId + "<br>" +
        "window.origin: " + String(window.origin) + "<br>" +
        "localStorage 隔离: " + (storageBlocked ? "已阻断 ✓" : "未阻断") + "<br>" +
        "parent DOM 隔离: " + (parentBlocked ? "已阻断 ✓" : "未阻断");
    }})();

    function renderChart(points) {{
      const W = 760, H = 240, padX = 42, padY = 26;
      const maxV = Math.max(100, ...points.map((p) => p.value));
      const stepX = points.length > 1 ? (W - padX * 2) / (points.length - 1) : 0;
      const coords = points.map((p, i) => {{
        const x = padX + i * stepX;
        const y = H - padY - (p.value / maxV) * (H - padY * 2);
        return {{ ...p, x, y }};
      }});
      const pathD = coords.map((c, i) => (i === 0 ? "M" : "L") + c.x.toFixed(1) + "," + c.y.toFixed(1)).join(" ");
      const areaD = coords.length
        ? pathD + " L" + coords[coords.length - 1].x.toFixed(1) + "," + (H - padY) + " L" + coords[0].x.toFixed(1) + "," + (H - padY) + " Z"
        : "";

      let html = "";
      for (let g = 0; g <= 4; g++) {{
        const y = padY + (g * (H - padY * 2)) / 4;
        html += `<line x1="${{padX}}" y1="${{y}}" x2="${{W - padX}}" y2="${{y}}" stroke="var(--t-border-soft)" stroke-dasharray="4 4" />`;
      }}
      if (areaD) {{
        html += `<path d="${{areaD}}" fill="var(--t-accent-soft)" />`;
        html += `<path d="${{pathD}}" fill="none" stroke="var(--t-accent)" stroke-width="2.5" stroke-linejoin="round" />`;
      }}
      coords.forEach((c, idx) => {{
        html += `<g class="node" data-idx="${{idx}}">` +
          `<circle cx="${{c.x}}" cy="${{c.y}}" r="6" fill="var(--t-accent)" stroke="var(--t-bg-surface)" stroke-width="2" />` +
          `<text x="${{c.x}}" y="${{c.y - 12}}" text-anchor="middle" font-size="11" fill="var(--t-fg)">${{c.value}}</text>` +
          `<text x="${{c.x}}" y="${{H - 6}}" text-anchor="middle" font-size="11" fill="var(--t-fg-dim)">${{c.label}}</text>` +
          `</g>`;
      }});
      chartEl.innerHTML = html;

      chartEl.querySelectorAll(".node").forEach((el) => {{
        el.addEventListener("click", () => {{
          const pt = coords[Number(el.getAttribute("data-idx"))];
          if (!pt) return;
          const prompt = `请分析交互看板中 ${{pt.label}} 的指标值 ${{pt.value}}（备注：${{pt.note}}）是否存在异常？`;
          window.tentacle.askChat(prompt);
          window.tentacle.toast("已将节点分析提问预填到主对话输入框", "info");
          pushLog("点击节点 " + pt.label + " (" + pt.value + ") -> askChat");
        }});
      }});
    }}

    async function loadSeries() {{
      const res = await window.tentacle.fetch("/series");
      if (res.ok && res.data && Array.isArray(res.data.points)) {{
        renderChart(res.data.points);
        pushLog("拉取 /series 成功，共 " + res.data.points.length + " 个点");
      }}
    }}

    document.getElementById("btn-add").addEventListener("click", async () => {{
      const res = await window.tentacle.fetch("/series", {{ method: "POST", body: {{ action: "append" }} }});
      if (res.ok) {{
        renderChart(res.data.points);
        window.tentacle.toast("已通过插件 API 新增数据点", "ok");
        pushLog("POST /series 新增数据点");
      }}
    }});

    document.getElementById("btn-push").addEventListener("click", async () => {{
      await window.tentacle.fetch("/push-event", {{ method: "POST", body: {{}} }});
    }});

    document.getElementById("btn-asset").addEventListener("click", async () => {{
      const meta = await window.tentacle.asset("meta.json", {{ responseType: "json" }});
      pushLog("读取 ui/meta.json -> " + JSON.stringify(meta));
      window.tentacle.toast("静态资源读取成功: " + meta.author, "ok");
    }});

    document.getElementById("btn-probe").addEventListener("click", async () => {{
      try {{
        await fetch("/api/settings");
        pushLog("意外：直连未被阻断");
      }} catch (err) {{
        pushLog("直连 /api/settings 已被 CSP connect-src 'none' 阻断 ✓");
        window.tentacle.toast("沙箱成功拦截直连请求", "ok");
      }}
    }});

    loadSeries();
  </script>
</body>
</html>
"""

    @app.get("/api/plugins/ui", dependencies=[Depends(auth)])
    async def plugins_ui_list():
        if app.state.scenario == "empty":
            return {"items": []}
        items = []
        for p in state["plugins"]:
            if p.get("status") == "active" and p.get("ui"):
                ui = p["ui"]
                items.append({
                    "plugin_id": p["id"],
                    "name": (p.get("manifest") or {}).get("name", p["id"]),
                    "version": (p.get("manifest") or {}).get("version", ""),
                    "title": ui.get("title") or p["id"],
                    "description": ui.get("description") or "",
                    "placement": ui.get("placement") or "sidebar",
                    "has_routes": bool(p.get("routes")),
                })
        return {"items": items}

    @app.get("/api/plugins/{plugin_id}/ui/document", dependencies=[Depends(auth)])
    async def plugins_ui_document(plugin_id: str):
        item = next((p for p in state["plugins"] if p["id"] == plugin_id and p.get("status") == "active" and p.get("ui")), None)
        if item is None:
            raise HTTPException(404, f"插件 {plugin_id} 未启用或未提供自定义页面")
        ui = item["ui"]
        if plugin_id == "browser":
            manifest = parse_manifest(bundled_plugins_dir() / "browser")
            assert manifest.ui is not None
            html_doc = build_plugin_ui_document(
                bundled_plugins_dir() / "browser" / manifest.ui.dir,
                manifest.ui.entry,
            )
        else:
            html_doc = demo_ui_html
        return {
            "plugin_id": plugin_id,
            "name": (item.get("manifest") or {}).get("name", plugin_id),
            "version": (item.get("manifest") or {}).get("version", ""),
            "title": ui.get("title") or plugin_id,
            "description": ui.get("description") or "",
            "placement": ui.get("placement") or "sidebar",
            "has_routes": bool(item.get("routes")),
            "html": html_doc,
        }

    @app.get("/api/plugins/{plugin_id}/ui/assets/{asset_path:path}", dependencies=[Depends(auth)])
    async def plugins_ui_asset(plugin_id: str, asset_path: str):
        if plugin_id == "demo-viz" and asset_path == "meta.json":
            return JSONResponse(
                {"plugin": "demo-viz", "author": "Tentacle Preview", "mode": "sandbox"},
                headers={"X-Content-Type-Options": "nosniff"},
            )
        raise HTTPException(404, f"资源不存在: {asset_path}")

    @app.get("/api/plugins/ext/demo-viz/series", dependencies=[Depends(auth)])
    async def demo_viz_series():
        return {"points": deepcopy(demo_series)}

    @app.post("/api/plugins/ext/demo-viz/series", dependencies=[Depends(auth)])
    async def demo_viz_append():
        next_hour = 15 + max(0, len(demo_series) - 6)
        demo_series.append({
            "label": f"{next_hour % 24:02d}:{int(time.time()) % 60:02d}",
            "value": random.randint(28, 98),
            "note": "实时采样",
        })
        if len(demo_series) > 10:
            del demo_series[0]
        return {"points": deepcopy(demo_series)}

    @app.post("/api/plugins/ext/demo-viz/push-event", dependencies=[Depends(auth)])
    async def demo_viz_push():
        demo_series.append({
            "label": f"WS-{int(time.time()) % 100:02d}",
            "value": random.randint(35, 96),
            "note": "WS 推送",
        })
        if len(demo_series) > 10:
            del demo_series[0]
        payload = {
            "type": "plugin.ui_event",
            "data": {
                "plugin_id": "demo-viz",
                "event": "metrics.tick",
                "payload": {"latest": demo_series[-1]},
            },
        }
        app.state.events.append(payload)
        for socket in tuple(app.state.sockets):
            try:
                await socket.send_json(payload)
            except Exception:
                app.state.sockets.discard(socket)
        return {"ok": True}

    # 合成 browser 实时视口预览状态（模拟无头 Chrome 画面流与交互）
    browser_sim = {
        "connected": False,
        "seq": 0,
        "width": 960,
        "height": 540,
        "url": "https://portal.example.test/verify",
        "title": "Demo Verification Portal",
        "tabs": [
            {"tab_id": "t1", "label": None, "index": 0, "title": "Demo Verification Portal", "url": "https://portal.example.test/verify", "active": True},
            {"tab_id": "t2", "label": None, "index": 1, "title": "Example Docs", "url": "https://docs.example.test/guide", "active": False},
        ],
        "next_tab_num": 3,
        "active_tab": 0,
        "input_focused": False,
        "input_text": "tentacle-user",
        "slider_x": 0.0,  # 0..1
        "dragging_slider": False,
        "verified": False,
        "scroll_y": 0,
        "last_click": None,
        "clicks": 0,
    }

    async def _broadcast_plugin_event(plugin_id: str, event: str, payload: dict):
        msg = {
            "type": "plugin.ui_event",
            "data": {
                "plugin_id": plugin_id,
                "event": event,
                "payload": payload,
            },
        }
        app.state.events.append(msg)
        for socket in tuple(app.state.sockets):
            try:
                await socket.send_json(msg)
            except Exception:
                app.state.sockets.discard(socket)

    def _render_browser_sim_frame() -> dict:
        w, h = browser_sim["width"], browser_sim["height"]
        img = Image.new("RGB", (w, h), (245, 243, 239))
        draw = ImageDraw.Draw(img)

        # 模拟网页顶栏
        draw.rectangle([0, 0, w, 54], fill=(34, 31, 28))
        draw.text((24, 20), f"HEADLESS CHROME PREVIEW  |  {browser_sim['url']}", fill=(237, 232, 224))
        draw.text((w - 190, 20), f"scrollY: {browser_sim['scroll_y']}px  clicks: {browser_sim['clicks']}", fill=(175, 168, 158))

        # 居中卡片
        cy = 96 - min(60, max(-40, browser_sim["scroll_y"] // 4))
        card = [180, cy, w - 180, cy + 360]
        draw.rounded_rectangle(card, radius=14, fill=(255, 255, 255), outline=(214, 207, 198), width=2)
        draw.text((216, cy + 26), "Interactive Headless Browser Viewport (Synthetic Preview)", fill=(34, 31, 28))
        draw.text((216, cy + 50), "Click input box to type, drag the slider right, or scroll wheel.", fill=(112, 105, 97))

        # 输入框 [216, cy + 92, w - 216, cy + 136]
        inp_box = [216, cy + 92, w - 216, cy + 136]
        inp_outline = (201, 122, 74) if browser_sim["input_focused"] else (196, 189, 180)
        draw.rounded_rectangle(inp_box, radius=8, fill=(250, 248, 245), outline=inp_outline, width=2)
        cursor = "|" if browser_sim["input_focused"] else ""
        shown_text = (browser_sim["input_text"] or "")[-48:]
        draw.text((232, cy + 108), f"Account / Note: {shown_text}{cursor}", fill=(34, 31, 28))

        # 滑块验证轨道 [216, cy + 174, w - 216, cy + 224]
        track_x0, track_y0, track_x1, track_y1 = 216, cy + 174, w - 216, cy + 224
        knob_w = 64
        max_travel = (track_x1 - track_x0) - knob_w
        knob_x = int(track_x0 + browser_sim["slider_x"] * max_travel)
        fill_color = (220, 252, 231) if browser_sim["verified"] else (243, 236, 228)
        border_color = (22, 163, 74) if browser_sim["verified"] else (204, 196, 186)
        draw.rounded_rectangle([track_x0, track_y0, track_x1, track_y1], radius=10, fill=fill_color, outline=border_color, width=2)
        if knob_x > track_x0:
            prog_color = (134, 239, 172) if browser_sim["verified"] else (238, 206, 184)
            draw.rounded_rectangle([track_x0 + 2, track_y0 + 2, knob_x + knob_w // 2, track_y1 - 2], radius=8, fill=prog_color)

        label = "VERIFIED OK - Human drag completed" if browser_sim["verified"] else "Drag slider -> to complete check"
        draw.text((track_x0 + 130, track_y0 + 19), label, fill=(21, 128, 61) if browser_sim["verified"] else (110, 102, 94))

        knob_color = (22, 163, 74) if browser_sim["verified"] else (201, 122, 74)
        draw.rounded_rectangle([knob_x, track_y0 + 3, knob_x + knob_w, track_y1 - 3], radius=8, fill=knob_color)
        draw.text((knob_x + 22, track_y0 + 19), "OK" if browser_sim["verified"] else ">>", fill=(255, 255, 255))

        # 重置按钮 [216, cy + 256, 360, cy + 296]
        draw.rounded_rectangle([216, cy + 256, 360, cy + 296], radius=8, fill=(44, 40, 36))
        draw.text((252, cy + 270), "Reset Check", fill=(255, 255, 255))

        # 最近一次点击指示圈
        if browser_sim["last_click"] is not None:
            lx, ly = browser_sim["last_click"]
            draw.ellipse([lx - 10, ly - 10, lx + 10, ly + 10], outline=(201, 122, 74), width=2)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78)
        browser_sim["seq"] += 1
        return {
            "data": base64.b64encode(buf.getvalue()).decode("ascii"),
            "seq": browser_sim["seq"],
            "metadata": {
                "deviceWidth": w,
                "deviceHeight": h,
                "pageScaleFactor": 1,
                "timestamp": int(time.time() * 1000),
            },
        }

    def _browser_sim_status(*, include_frame: bool = False) -> dict:
        out = {
            "connected": browser_sim["connected"],
            "port": 19223 if browser_sim["connected"] else None,
            "session": "tentacle-preview",
            "mode": "local",
            "headed": False,
            "max_fps": 10,
            "url": browser_sim["url"],
            "title": browser_sim["title"],
            "tabs": deepcopy(browser_sim["tabs"]),
            "viewport": {"width": browser_sim["width"], "height": browser_sim["height"]},
            "has_frame": browser_sim["connected"],
            "error": None,
        }
        if include_frame and browser_sim["connected"]:
            out["frame"] = _render_browser_sim_frame()
        return out

    @app.get("/api/plugins/ext/browser/status", dependencies=[Depends(auth)])
    async def browser_preview_status(include_frame: bool = False):
        return _browser_sim_status(include_frame=include_frame)

    @app.post("/api/plugins/ext/browser/stream/connect", dependencies=[Depends(auth)])
    async def browser_preview_connect():
        browser_sim["connected"] = True
        frame = _render_browser_sim_frame()
        await _broadcast_plugin_event("browser", "stream.frame", frame)
        snap = _browser_sim_status(include_frame=False)
        snap["frame"] = frame
        return snap

    @app.post("/api/plugins/ext/browser/stream/disconnect", dependencies=[Depends(auth)])
    async def browser_preview_disconnect():
        browser_sim["connected"] = False
        snap = _browser_sim_status(include_frame=False)
        await _broadcast_plugin_event("browser", "stream.state", snap)
        return snap

    @app.post("/api/plugins/ext/browser/stream/heartbeat", dependencies=[Depends(auth)])
    async def browser_preview_heartbeat():
        return _browser_sim_status(include_frame=False)

    @app.post("/api/plugins/ext/browser/input", dependencies=[Depends(auth)])
    async def browser_preview_input(body: dict):
        if not browser_sim["connected"]:
            raise HTTPException(409, "实时视口尚未连接")
        events = body.get("events") if isinstance(body, dict) else []
        if not isinstance(events, list):
            events = []
        cy = 96 - min(60, max(-40, browser_sim["scroll_y"] // 4))
        track_x0, track_y0, track_x1, track_y1 = 216, cy + 174, browser_sim["width"] - 216, cy + 224
        max_travel = max(1, (track_x1 - track_x0) - 64)

        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_type = ev.get("type")
            if ev_type == "input_mouse":
                et = ev.get("eventType")
                x = float(ev.get("x", 0))
                y = float(ev.get("y", 0))
                if et == "mousePressed":
                    browser_sim["last_click"] = (x, y)
                    browser_sim["clicks"] += 1
                    browser_sim["input_focused"] = (216 <= x <= browser_sim["width"] - 216 and cy + 92 <= y <= cy + 136)
                    if track_x0 <= x <= track_x1 and track_y0 <= y <= track_y1:
                        browser_sim["dragging_slider"] = True
                        browser_sim["slider_x"] = max(0.0, min(1.0, (x - track_x0 - 32) / max_travel))
                        if browser_sim["slider_x"] >= 0.92:
                            browser_sim["slider_x"] = 1.0
                            browser_sim["verified"] = True
                    elif 216 <= x <= 360 and cy + 256 <= y <= cy + 296:
                        browser_sim["slider_x"] = 0.0
                        browser_sim["verified"] = False
                elif et == "mouseMoved" and browser_sim["dragging_slider"]:
                    browser_sim["slider_x"] = max(0.0, min(1.0, (x - track_x0 - 32) / max_travel))
                    if browser_sim["slider_x"] >= 0.92:
                        browser_sim["slider_x"] = 1.0
                        browser_sim["verified"] = True
                elif et == "mouseReleased":
                    browser_sim["dragging_slider"] = False
                elif et == "mouseWheel":
                    dy = int(float(ev.get("deltaY", 0)))
                    browser_sim["scroll_y"] = max(0, min(600, browser_sim["scroll_y"] + dy))
            elif ev_type == "input_keyboard":
                if ev.get("eventType") == "keyDown":
                    key = str(ev.get("key") or "")
                    text = str(ev.get("text") or "")
                    if key == "Backspace":
                        browser_sim["input_text"] = browser_sim["input_text"][:-1]
                    elif text:
                        browser_sim["input_text"] = (browser_sim["input_text"] + text)[-64:]
            elif ev_type == "input_text":
                text = str(ev.get("text") or "")
                if text:
                    browser_sim["input_text"] = (browser_sim["input_text"] + text)[-64:]

        frame = _render_browser_sim_frame()
        await _broadcast_plugin_event("browser", "stream.frame", frame)
        return {"ok": True, "sent": len(events)}

    @app.post("/api/plugins/ext/browser/action", dependencies=[Depends(auth)])
    async def browser_preview_action(body: dict):
        act = str((body or {}).get("action") or "").strip().lower()
        if act == "open":
            url = str((body or {}).get("url") or "").strip()
            if url and not url.startswith(("http://", "https://", "about:")):
                url = "https://" + url
            if url:
                browser_sim["url"] = url
                browser_sim["tabs"][browser_sim["active_tab"]]["url"] = url
                browser_sim["tabs"][browser_sim["active_tab"]]["title"] = url.split("://", 1)[-1][:28]
        elif act == "tab_new":
            idx = len(browser_sim["tabs"])
            tab_num = browser_sim["next_tab_num"]
            browser_sim["next_tab_num"] = tab_num + 1
            for t in browser_sim["tabs"]:
                t["active"] = False
            browser_sim["tabs"].append({
                "tab_id": f"t{tab_num}",
                "label": None,
                "index": idx,
                "title": f"New Tab {tab_num}",
                "url": "about:blank",
                "active": True,
            })
            browser_sim["active_tab"] = idx
            browser_sim["url"] = "about:blank"
        elif act == "tab_select":
            tab_id = str((body or {}).get("tab_id") or "").strip()
            idx = next((i for i, t in enumerate(browser_sim["tabs"]) if tab_id and t.get("tab_id") == tab_id), None)
            if idx is None:
                idx = int((body or {}).get("index", 0))
            if 0 <= idx < len(browser_sim["tabs"]):
                for i, t in enumerate(browser_sim["tabs"]):
                    t["active"] = (i == idx)
                browser_sim["active_tab"] = idx
                browser_sim["url"] = browser_sim["tabs"][idx]["url"]
                browser_sim["title"] = browser_sim["tabs"][idx]["title"]
        elif act == "tab_close" and len(browser_sim["tabs"]) > 1:
            tab_id = str((body or {}).get("tab_id") or "").strip()
            idx = next((i for i, t in enumerate(browser_sim["tabs"]) if tab_id and t.get("tab_id") == tab_id), None)
            if idx is None:
                idx = int((body or {}).get("index", browser_sim["active_tab"]))
            if 0 <= idx < len(browser_sim["tabs"]):
                browser_sim["tabs"].pop(idx)
                for i, t in enumerate(browser_sim["tabs"]):
                    t["index"] = i
                    t["active"] = (i == 0)
                browser_sim["active_tab"] = 0
                browser_sim["url"] = browser_sim["tabs"][0]["url"]
                browser_sim["title"] = browser_sim["tabs"][0]["title"]
        elif act == "sync_viewport":
            pass
        if browser_sim["connected"]:
            frame = _render_browser_sim_frame()
            await _broadcast_plugin_event("browser", "stream.frame", frame)
        return _browser_sim_status(include_frame=False)

    @app.get("/api/plugins/ext/{_rest:path}", dependencies=[Depends(auth)])
    async def plugins_ext_preview():
        return {"preview": True}
