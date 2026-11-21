# 插件与多端扩展开发参考

> 本文面向维护 Tentacle 的 Agent 与开发者，概述插件系统与通用多端接入机制的核心契约；实时行为仍以源码与测试为准。最小插件目录样板见 [plugin-example](plugin-example/README.md)。

---

## 1. 目录职责与发现机制

- `plugins/`：插件基础设施（`manifest.py`、`permissions.py`、`context.py`、`manager.py`、`hooks.py`、`services.py`、`mcp_client.py`、`installer.py`）。
- `plugins/bundled/<id>/`：随程序版本打包分发的内置插件。保留 ID `RESERVED_PLUGIN_IDS = {"bundled"}`，不允许安装器或用户目录覆盖占用同名保留目录。
- `$TENTACLE_HOME/plugins/<id>/`：用户本地插件目录；当用户目录与 `bundled/` 存在同名 `<id>` 时，由用户目录版本覆盖内置版本（`discover_plugin_dirs()`）。
- 打包与清理白名单（[scripts/deploy.py](../scripts/deploy.py)）：仅 `plugins/bundled/` 下的代码及 `.yaml`、`.md` 文件纳入受管发布包；`config.yaml`、私人 `prompt/`、用户 `plugins/` 不入库。

---

## 2. 安全边界与配置不变量

1. **默认未批准禁止执行代码**：未同时满足 `plugins.items.<id>.enabled == true` 与 `trusted == true` 的本地插件，仅解析清单与权限说明供 WebUI `/plugins` 审核，绝不导入入口模块。
2. **必填与可选环境变量隔离**：
   - `requires_env`：缺失任意一项即阻断激活（`status = "pending"`）。
   - `optional_env`：允许从 `ctx.get_env()` 读取并可在 WebUI `/plugins` 写入 `.env`，缺失时不阻断启动。
   - 未在 `requires_env`、`optional_env` 或 `env` 类型 `config_schema` 字段中声明的变量，调用 `ctx.get_env()` 会抛出 `PermissionDeniedError`。
   - 凭据值仅写入应用目录 `.env`，API 响应、配置快照、日志与降级错误栈必须脱敏（[web/plugins.py](../web/plugins.py)）。
3. **动态配置注入**：[config/store.py](../config/store.py) 通过 `fields_for()` 动态合并各插件 `manifest.yaml` 中的 `config_schema`（支持 `string`、`integer`、`number`、`boolean`、`env`、`proxy`、`catalog_url`、`string_list`）到 `plugins.items.<id>.config.*`。
4. **工具命名空间与角色暴露**：
   - 本地插件工具注册为 `p_<plugin_id>__<raw_name>`；外部 MCP 工具注册为 `mcp_<server_id>__<raw_name>`（总长 ≤ 64，超长带 hash 截断，见 `qualify_tool_name()`）。
   - 角色工具列表中未显式禁止且已启用/信任的插件工具、`auto_expose=True` 时，通过 `ToolRegistry.resolve_tool_names()` 自动追加给对应角色；禁用列表支持填原始名或限定名（`disabled_tools`）。

---

## 3. 清单与 `PluginContext` 能力契约

### `manifest.yaml` 关键字段
- 必填：`id`（与目录名一致，`^[a-z][a-z0-9_-]{0,47}$`）、`name`、`version`
- 可选：`description`、`author`、`entrypoint`（如 `plugin.py:setup`）、`permissions`、`requires_env`、`optional_env`、`skills_dir`（需 `skills:provide`）、`prompt_file`（需 `prompts:append`）、`ui`（需 `web:ui`，包含 `dir`、`entry`、`title`、`description`、`placement: sidebar | plugins_only`，省略时若存在 `ui/index.html` 自动识别）、`config_schema`

### 权限与受控接口（[permissions.py](../plugins/permissions.py)、[context.py](../plugins/context.py)、[services.py](../plugins/services.py)）

| 权限 | 对应能力与门控入口 |
|---|---|
| `tools:register` | `ctx.register_tool(name, description, parameters, func, *, is_async=None, auto_expose=True)` |
| `commands:register` | `ctx.register_command(name, description, handler)`（不可覆盖内置命令） |
| `skills:provide` | `skills_dir` 下的 `<skill>/SKILL.md` 挂载至 `<available_skills>` 与 `skills` 工具 |
| `prompts:append` | `prompt_file` 注入 `<guild>` 区块（注意按需克制使用，工具说明优先写进工具 schema） |
| `hooks:observe` / `hooks:mutate` | `ctx.on(event, handler, *, priority=100)`；支持事件：`before_round`、`after_round`、`before_tool_call`、`after_tool_result`、`before_heartbeat`、`on_session_switch`、`on_daily_compress`。仅 `hooks:mutate` 允许返回 `extra_context`、`block`、`arguments`、`result`、`skip`、`briefing` 修改主链路 |
| `fs:plugin_data` | `ctx.data_dir` 与 `ctx.data_path(rel)`，限定在 `TENTACLE_HOME/data/plugins/<id>/` |
| `fs:workspace_read` / `fs:workspace_write` | `ctx.files`：`read_text`、`read_bytes`、`write_text`、`write_bytes`、`file_marker`、`store_image`、`upload_image`（经过敏感文件门检查） |
| `memory:read` / `memory:write` | `ctx.memory`：`search`、`get_summaries`、`save_memory` |
| `goals:read` / `goals:write` | `ctx.goals`：`list_active`、`get_summary`、`update` |
| `scheduler:read` / `scheduler:write` | `ctx.scheduler`：`list_active`、`get_detail`、`update` |
| `situation:write` | `ctx.situation`：`read_situation`、`write_situation`、`add_note`、`remove_note` |
| `channels:register` | `ctx.register_channel(platform, *, sender, event_callback=None, description="", supports_target=False, target_description="")` + `ctx.emit_user_message(..., platform=..., user_id=None, images=None)`，动态挂载 `send_message` 与 `EventFanout`（仅在至少注册一个外部通道时向模型暴露 `send_message`） |
| `web:routes` | `ctx.register_router(router)`（挂载 `/api/plugins/ext/<id>/*`，强制走 `DeviceAccessGate` 与 `auth_dep`）；`ctx.web`：`session_owner(request)`、`make_session_guard(request)`、`broadcast(event)`、`emit_ui_event(event, payload)`、`retain_task(coro)` |
| `web:ui` | 在 WebUI 工作台以独立源沙箱（`<iframe sandbox="allow-scripts allow-forms allow-downloads">` + `connect-src 'none'`）加载插件 `ui/` 目录；页面可通过宿主注入的 `window.tentacle` 桥接调用 `fetch(path, init)`（仅限 `/api/plugins/ext/<id>/*`）、`asset(relPath, opts)`、`toast(msg, variant)`、`askChat(prompt)`、`openChat()`、`onThemeChange(cb)`、`onEvent(cb)`，后端可通过 `ctx.web.emit_ui_event(event, payload)` 推送实时事件 |
| `web:routes` + `hooks:mutate` | `ctx.register_http_chat_adapter(adapter)`：拦截 `POST /api/chat/send`，可验证客户端头与通用 `client_meta`，提供每轮 `enter()` / `finish(status)` / `exit()` 上下文绑定与 `shield_disconnect=True` 断线保活 |
| `background:task` | `ctx.create_background_task(coro_or_factory, *, name="")` 随插件启动与卸载统一回收 |
| `mcp:connect` | 供外部 MCP 客户端能力标识（MCP 服务配置统一存于 `plugins.mcp.*`） |

### 模块隔离加载与卸载
- `PluginManager._load_and_setup()` 会注册 `tentacle_plugins.<id>` 虚拟包并写入 `__path__ = [str(plugin_dir)]`。**多文件插件一律使用包内相对导入（如 `from .push import HarmonyPush`）**，避免顶层模块名污染；卸载/重载时 `_remove_plugin_modules()` 会按 `tentacle_plugins.<id>` 前缀统一回收。
- 插件模块还可定义可选 `teardown(ctx)`（同步或异步），在 `PluginManager.shutdown()` 时先于资源清理逆序调用。

---

## 4. 新增内置插件核对清单

1. 在 `plugins/bundled/<id>/` 下创建 `manifest.yaml` 与入口代码，避免对核心做专项硬编码；若缺少通用宿主能力，应抽象为 `services.py` / `context.py` 的权限门控原语。
2. 若在 `config.defaults.yaml` 默认启用，在 `plugins.items.<id>` 填妥 `enabled`、`trusted` 与 `config`，并同步清理不再需要的 `roles.*.tools.enabled` 静态项。
3. 核对 `scripts/deploy.py` 打包白名单与 `dev/plugins_preview.py`（若需在合成 WebUI 预览展示）。
4. 修改 Web schema 后运行 `scripts/export_openapi.py` 与 `npm --prefix frontend run gen:api`。
5. 常用定向校验命令：

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest tests/test_plugins_core.py tests/test_plugins_mcp.py tests/test_plugins_hooks_services.py tests/test_plugins_channels_web.py tests/test_plugins_staging_and_api.py tests/test_browser.py tests/test_browser_neko.py tests/test_phone.py -q
npm --prefix frontend run typecheck && npm --prefix frontend run test && npm --prefix frontend run build
```
