# Tentacle

Tentacle 是一个面向 **Linux / WSL** 的轻量自托管持久上下文 AI Agent 系统。整个服务以单一进程（[run_web.py](run_web.py)）运行，核心服务不依赖外部数据库或容器运行时即可工作（仅可选的可视化浏览器插件需要 Docker），让 Web 工作台、定时心跳、后台任务、外部消息渠道插件与端侧客户端共享同一连续对话与记忆库，并将只读源码与运行时状态目录彻底隔离。

## 核心特性

- **单一进程共享上下文与每日会话交接**：主对话、周期心跳、外部渠道消息共用同一 `AgentSession` 与 SQLite Chronicle 历史；支持在配置的时间窗口内完成近况整理后自动开启新会话，保持上下文干净且有连续性。
- **结构化三层模型管理与多协议方言**：通过供应商连接、模型目录与角色策略分层编排模型能力（支持 `deepseek`、`openai`、`openai_compatible`、`google_developer`、`google_vertex`、`openrouter` 等），内置 Chat / Responses / Gemini Interactions & GenerateContent 方言与显式缓存控制，并支持主模型动态切换与兼容历史无损交接。
- **内嵌图谱记忆与原文双路检索**：基于内嵌 LadybugDB 持久化事件、人物、地点与概念节点，结合向量相似度与原文分块混合检索返回可溯源证据；支持可选的写入前概念对齐与 Web 端可控历史概念扫描去重。
- **全模态附件输入与受控文件回传**：支持图片、音频、视频及常见文档解析输入（含可选 B 站视频拉取），并统一通过路径防越界与敏感路径拦截机制对外发送工作区文件与附件。
- **插件沙盒、外部渠道网关与 MCP**：
  - 本地扩展插件经 `PluginContext` 最小权限门控加载，自定义扩展路由统一挂载至 `/api/plugins/ext/<id>/*` 并继承主站鉴权。
  - 外部消息发送统一收口为 `send_message` 通道网关（无活跃外部通道插件时自动向模型隐藏）。
  - 随包内置四款插件：`browser`（基于 Docker Neko + `agent-browser` 的可视化浏览器自动化）、`proton_mail`（Proton 加密邮箱助手，默认关闭）、`qqbot`（QQ 官方机器人 C2C 私聊，默认关闭）与 `harmony`（鸿蒙客户端联动，默认关闭）。
  - 支持接入外部 `stdio` / `streamable_http` MCP 工具服务并在 WebUI 热重连。
- **正交后台调度与编程子代理**：内置定时调度器静默执行工具调用、一次性子进程命令与预约唤醒，不污染主对话上下文；可选集成 `@deepseek-ai/dsh` 编程子代理实现双向任务接力与原生工作区审批，并提供受黑名单约束的交互终端。
- **完整的 WebUI 与安全运维边界**：内置 React 19 工作台（对话、图谱、配置、插件、子代理、终端、用量与维护面板）；采用双层鉴权（Web 密码 + 设备授权门控）、纯写入 `.env` 凭据隔离、配置差异预览与自动备份回滚、加密迁移导出及 Linux `systemd` 自动校验升级/回退机制。

## 运行环境要求

Tentacle 的运行与部署设计面向 **Linux / WSL**（使用 `systemd` 托管、POSIX 文件锁 `fcntl`、`uvloop` 与 Bash 入口）：

- **硬件与内存参考**：核心服务零外部数据库依赖（SQLite + 内嵌 LadybugDB），可选重依赖按需加载，`systemd` 单元默认启用 `MALLOC_ARENA_MAX=2`；日常主进程常驻约 **280–350 MiB**，可在 **1C1G VPS** 上直接运行（如需同时按需启停 `dsh` 编程子代理或 Docker Neko 可视化浏览器，建议 **2 GiB+** 内存，1 GiB 主机建议开启 `zram` / Swap）
- **Python 3.13**（Linux 一键安装脚本自动配置独立 `3.13.15` 解释器）
- **Node.js**（仅从源码构建前端 `frontend/` 时需要，CI 使用 Node 24；基于 Release 包生产部署的主机无需安装）
- 可选系统工具：见 [音视频输入](docs/MEDIA_INPUTS.md)、[Docker Neko 可视化浏览器](docs/BROWSER_NEKO.md) 与 [DSH 编程台](docs/SUBAGENTS.md)

## 快速开始

### 方式一：Linux / systemd 部署（推荐）

从干净的仓库检出执行安装脚本，自动初始化隔离目录、独立运行环境、`systemd` 服务与定期检查定时器（详情见 [Linux 部署与更新](docs/DEPLOYMENT.md)）：

```bash
sudo python3 scripts/install_linux.py --apply
```

部署完成后通过 SSH 转发端口访问 WebUI（安装器默认监听 `127.0.0.1:8420`），在 WebUI 页面中配置模型接入、角色绑定与登录密码。

### 方式二：Linux / WSL 从源码本地运行

```bash
# 1. 安装 Python 依赖并构建 Web 前端静态产物
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build

# 2. 准备独立的数据根目录 TENTACLE_HOME（不能位于源码目录内）
export TENTACLE_HOME=/path/to/tentacle-home
mkdir -p "$TENTACLE_HOME"/{config,data,prompt,skills,workspace,logs,.cache,tmp}

# 3. 启动 WebUI
./start.sh
```

首次启动后打开 WebUI，进入「系统配置中心」添加供应商与模型、绑定 `chat` 角色，并在设置页设置访问密码。各配置段用途与修改方式见 [配置项说明](docs/CONFIG_GUIDE.md) 与 [应用配置管理](docs/CONFIGURATION.md)。

## 命令行管理（`tentacle`）

通过 Linux 安装器部署后，系统会自动安装 `/usr/local/bin/tentacle` 命令行入口（源码位于 [scripts/tentacle.sh](scripts/tentacle.sh)），用于管理服务启停、日志查看与版本升级回退：

| 命令 | 说明 |
| --- | --- |
| `tentacle start` / `stop` / `restart` | 启动、停止或重启 `tentacle.service` |
| `tentacle status` | 查看 `systemd` 服务当前运行状态 |
| `tentacle logs` | 查看最近 80 行 `journalctl` 服务日志 |
| `tentacle version` | 查看当前生产环境运行版本 |
| `tentacle check-updates` | 立即检查正式 GitHub Release，不安装 |
| `tentacle update-status` | 离线查看最近一次更新检查结果 |
| `tentacle update [vX.Y[.Z]]` | 预览从正式 GitHub Release 升级 |
| `tentacle update vX.Y --apply` | 确认并构建新版本、切换 `current` 并健康校验 |
| `tentacle update --recover` | 恢复中断的更新流程（可离线运行） |
| `tentacle revert [vX.Y]` / `--apply` | 预览或回退至上一个成功版本 |
| `tentacle browser <subcommand>` | 一键安装、启停、查看凭据与反代配置管理本机 Docker Neko 可视化浏览器 |
| `tentacle help` | 查看完整命令行帮助 |

如需在任意目录下离线检查或修改应用配置，可使用 [scripts/configure.py](scripts/configure.py)（详见 [Linux 部署与更新](docs/DEPLOYMENT.md)）。

## 项目结构

| 路径 | 职责 |
| --- | --- |
| `run_web.py` / `start.sh` | 单进程运行入口与 Linux/WSL 启动脚本 |
| `agent/` | 主循环、会话编排、系统提示词组装、会话交接与 SQLite Chronicle |
| `config/` / `config.defaults.yaml` | 三层配置合并校验、供应商目录与模型读取逻辑 |
| `dialects/` | OpenAI Chat、Responses、Gemini Generate/Interactions 方言适配 |
| `memory/` | LadybugDB 图谱记忆、混合检索、概念对齐与历史去重 |
| `attachments/` | 图片、音视频、文档多模态解析与受控附件导出 |
| `runtime/` | 心跳、定时调度、目标管理、用量统计、迁移备份与更新维护客户端 |
| `plugins/` | 插件权限沙盒、MCP 客户端与内置插件（`browser` / `proton_mail` / `harmony` / `qqbot`） |
| `tools/` | 自动发现工具模块与统一外部通道网关 `send_message` |
| `subagents/` | 可选 DSH 编程子代理托管 |
| `web/` / `frontend/` | FastAPI + WebSocket 后端与 React 19 WebUI |
| `scripts/` | Linux 安装、更新校验、回退、配置与 OpenAPI 导出脚本 |
| `docs/` | 用户指南、客户端说明与开发参考 |

## 文档导航

完整目录见 [docs/README.md](docs/README.md)：

- **配置与部署**：[配置项说明](docs/CONFIG_GUIDE.md) · [应用配置管理](docs/CONFIGURATION.md) · [Linux 部署与更新](docs/DEPLOYMENT.md)
- **功能与插件**：[内置插件与插件管理](docs/BUNDLED_PLUGINS.md) · [Docker Neko 可视化浏览器](docs/BROWSER_NEKO.md) · [接入 QQ 机器人](docs/QQBOT_SETUP.md) · [插件与多端扩展开发](docs/PLUGINS_DEV.md)（示例：[plugin-example](docs/plugin-example/README.md)）
- **前端与端侧**：[Web 前端架构与开发验证](docs/WEB_FRONTEND.md) · [鸿蒙客户端接入与插件说明](docs/HARMONY_CLIENT.md) · [开发环境](docs/DEVELOPMENT.md)
