# Web 前端架构与开发验证

Tentacle 的 WebUI 前端位于 `frontend/`（React 19 + TypeScript + Vite 6 + Tailwind CSS v4），构建产物输出至 `web/static/`（已由 `.gitignore` 忽略，不入库）。CI 与 Release 工作流会自动构建前端并随包发布，因此使用发行包部署的运行主机无需安装 Node.js。

## 功能页面

后端在 [web/server.py](../web/server.py) 挂载了以下 SPA 路由，对应 `frontend/src/features/` 下的模块：

- **对话工作台**（`/`、`/chat`）：WebSocket 实时流式输出、推理与工具轨迹折叠、多端消息气泡区分、斜杠命令与附件上传。
- **记忆图谱与概念去重**（`/graph`、`/dedup`）：LadybugDB 图谱可视化浏览、分层骨架过滤、等价组审查与人工去重操作。
- **系统配置中心**（`/config`）：供应商连接 → 模型目录 → 角色策略三层配置、远端模型读取、差异预览与备份回滚、纯写入 `.env` 凭据弹窗。
- **插件与 MCP 管理**（`/plugins`、`/plugin/{plugin_id}`、`/browser`）：统一开关与配置内置插件（含 `browser` 的 Docker Neko 容器启停与反代配置查看）、审核用户本地插件权限、管理外部 `stdio` / `streamable_http` MCP 服务与凭据环境变量；配置了自定义 UI 的插件与填写了 `live_view_url` 的可视化浏览器页面统一归入左侧栏「插件页面」分组。
- **编程子代理与终端**（`/subagents`、`/terminal`）：可选 DSH 任务列表与关联入口、嵌入式 `xterm.js` 交互终端。
- **用量统计、运行状态、后台与设置**（`/usage`、`/status`、`/admin`、`/settings`）：Token 与供应商余额统计、心跳状态、数据库巡检、登录密码/设备授权管理及一次性加密迁移导出。

## 构建、契约同步与单元测试

修改前端代码或测试前先安装依赖：

```bash
npm --prefix frontend ci
```

日常校验与构建：

```bash
npm --prefix frontend run typecheck
npm --prefix frontend run test
npm --prefix frontend run build
```

若修改了后端 FastAPI / Pydantic 接口或 Schema（[web/schemas.py](../web/schemas.py)），同步刷新 OpenAPI 类型契约：

```bash
.venv/bin/python scripts/export_openapi.py
npm --prefix frontend run gen:api
```

## 合成预览与 Playwright 验收

仓库提供基于临时合成数据的预览服务 [dev/preview.py](../dev/preview.py)，启动时完全隔离 `TENTACLE_HOME`、`.env`、数据库与真实模型调用：

```bash
# 先构建前端产物至 web/static/
npm --prefix frontend run build

# 启动合成预览（仅监听回环地址 127.0.0.1:8421）
.venv/bin/python dev/preview.py
```

如需验证真实无头浏览器渲染、响应式布局与交互流程，可在准备好 Playwright 与浏览器路径后运行 [dev/](../dev/) 下的 `.cjs` 验收脚本（例如配置页验收）：

```bash
PLAYWRIGHT_MODULE=playwright \
CHROME_PATH=/path/to/chromium \
TENTACLE_TEST_PYTHON=.venv/bin/python \
node dev/check_configuration_ui.cjs
```

除配置页 [dev/check_configuration_ui.cjs](../dev/check_configuration_ui.cjs) 外，`dev/` 目录还包含 `check_media_ui.cjs`、`check_concept_audit.cjs`、`check_stats_ui.cjs` 与 `check_subagents_ui.cjs` 等专项无头检查脚本。
