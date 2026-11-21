"""Synthetic plugins & MCP management state for dev preview; never reads .env or TENTACLE_HOME."""

from copy import deepcopy
import random
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from plugins.manifest import bundled_plugins_dir, parse_manifest
from plugins.permissions import PERMISSION_CATALOG, describe_permissions
from plugins.ui_bundle import SANDBOX_CSP


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
            return {"items": [], "browser_live_view": None}
        items = []
        browser_live_view = None
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
            if p.get("id") == "browser" and p.get("status") == "active":
                bcfg = p.get("config") or {}
                live_url = str(bcfg.get("live_view_url") or "").strip()
                if live_url.startswith(("http://", "https://")):
                    cdp_cfg = bool(str(bcfg.get("cdp_discovery_url") or "").strip())
                    browser_live_view = {
                        "live_view_url": live_url,
                        "cdp_configured": cdp_cfg,
                        "mode": "cdp" if cdp_cfg else "local",
                    }
        return {"items": items, "browser_live_view": browser_live_view}

    @app.get("/api/plugins/{plugin_id}/ui/document", dependencies=[Depends(auth)])
    async def plugins_ui_document(plugin_id: str):
        item = next((p for p in state["plugins"] if p["id"] == plugin_id and p.get("status") == "active" and p.get("ui")), None)
        if item is None:
            raise HTTPException(404, f"插件 {plugin_id} 未启用或未提供自定义页面")
        ui = item["ui"]
        return {
            "plugin_id": plugin_id,
            "name": (item.get("manifest") or {}).get("name", plugin_id),
            "version": (item.get("manifest") or {}).get("version", ""),
            "title": ui.get("title") or plugin_id,
            "description": ui.get("description") or "",
            "placement": ui.get("placement") or "sidebar",
            "has_routes": bool(item.get("routes")),
            "html": demo_ui_html,
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

    neko_preview_state = {"running": False}

    @app.get("/api/plugins/ext/browser/live-view", dependencies=[Depends(auth)])
    async def browser_live_view_preview():
        bp = next((p for p in state["plugins"] if p["id"] == "browser"), None)
        bcfg = (bp or {}).get("config") or {}
        live_url = str(bcfg.get("live_view_url") or "").strip()
        cdp_cfg = bool(str(bcfg.get("cdp_discovery_url") or "").strip()) or bool(bcfg.get("neko_enabled", False))
        return {
            "configured": bool(live_url),
            "live_view_url": live_url,
            "url_error": None,
            "cdp_configured": cdp_cfg,
            "cdp_reachable": cdp_cfg and neko_preview_state["running"],
            "browser_version": "Chromium (preview)" if (cdp_cfg and neko_preview_state["running"]) else "",
            "neko_enabled": bool(bcfg.get("neko_enabled", False)),
            "mode": "cdp" if cdp_cfg else "local",
        }

    @app.get("/api/plugins/ext/browser/neko/status", dependencies=[Depends(auth)])
    async def browser_neko_status_preview():
        bp = next((p for p in state["plugins"] if p["id"] == "browser"), None)
        bcfg = (bp or {}).get("config") or {}
        cdp_port = int(bcfg.get("neko_cdp_port") or 9222)
        http_port = int(bcfg.get("neko_http_port") or 8080)
        bind = str(bcfg.get("neko_bind") or "127.0.0.1")
        mux_port = int(bcfg.get("neko_mux_port") or 52000)
        running = neko_preview_state["running"]
        return {
            "ok": True,
            "available": True,
            "control_mode": "preview",
            "docker_available": True,
            "installed": True,
            "container_name": "tentacle-browser-neko",
            "container_state": "running" if running else "exited",
            "running": running,
            "cdp_port": cdp_port,
            "cdp_url": f"http://127.0.0.1:{cdp_port}/json/version",
            "cdp_reachable": running,
            "browser_version": "Chromium (preview)" if running else "",
            "http_listen": f"http://{bind}:{http_port}",
            "mux_port": mux_port,
            "nat_ip": str(bcfg.get("neko_nat_ip") or ""),
            "screen": str(bcfg.get("neko_screen") or "1280x720@24"),
            "install_dir": str(bcfg.get("neko_dir") or "/var/lib/tentacle-browser"),
            "live_view_url": str(bcfg.get("live_view_url") or ""),
            "proxy_config": f"# preview reverse_proxy 127.0.0.1:{http_port}",
        }

    @app.post("/api/plugins/ext/browser/neko/action", dependencies=[Depends(auth)])
    async def browser_neko_action_preview(body: dict):
        act = str((body or {}).get("action") or "").strip().lower()
        if act in {"start", "restart"}:
            neko_preview_state["running"] = True
        elif act == "stop":
            neko_preview_state["running"] = False
        return await browser_neko_status_preview()

    @app.get("/api/plugins/ext/{_rest:path}", dependencies=[Depends(auth)])
    async def plugins_ext_preview():
        return {"preview": True}
