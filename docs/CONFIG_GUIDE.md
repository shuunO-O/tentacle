# 配置项说明

本文介绍仓库根目录 [config.defaults.yaml](../config.defaults.yaml) 中各个配置段的作用。

Tentacle 的配置采用“仓库默认值 + 本机 `$TENTACLE_HOME/config/config.yaml` 增量覆盖 + `$TENTACLE_HOME/config/.env` 凭据值”三层机制，具体合并、校验、动态应用、备份恢复与 CLI 规则见 [应用配置管理](CONFIGURATION.md)。日常修改推荐直接在 WebUI「系统配置中心」（`/config`）与「插件与 MCP」（`/plugins`）完成。

---

## 1. 模型接入目录：`providers` / `models` / `roles`

模型体系拆分为相互引用的三层结构（`config_version: 2`）：

- **`providers`（供应商连接）**：管理接入类型（`deepseek`、`openai`、`openai_compatible`、`google_developer`、`google_vertex`、`openrouter`、`typesafe`）、服务端点 `base_url`、代理 `proxy`、认证方式（API Key 变量名 `api_key_env` 或 Vertex ADC/项目配置 `project` / `location` / `credentials_env` / `gcs_base_url`），以及可选的模型列表接口 `models_url` 与余额查询设置 `balance_mode` / `balance_url`。
- **`models`（模型目录）**：引用一个 `provider`，声明实际模型名称 `name`、协议 `api_style`（`chat`、`responses`、`interactions`、`gemini_generate`、`systemone`）、多模态能力（`vision` / `audio` / `video`）、`context_window` 及该模型的默认生成参数 `defaults`。
- **`roles`（业务角色）**：为系统固定职责分配模型（引用 `model`）、设置每轮最大迭代次数 `max_iterations`、可见核心工具白名单 `tools.enabled`、参数覆盖 `overrides`，以及 Gemini `explicit_cache` 等策略。系统默认包含以下角色：
  - `chat`：主对话与心跳共用的核心角色（须绑定模型后方可对话）。
  - `compress`：每日对话压缩与摘要生成。
  - `memory_extractor`：从对话中抽取事件、人物、地点与概念写入记忆图谱。
  - `embedding`：记忆向量化与检索嵌入模型。
  - `concept_alignment` / `concept_dedup`：可选写入前概念对齐与历史概念扫描使用的 System One 评分角色。

> 注：如果用户 `config.yaml` 声明了 `config_version`、`providers`、`models`、`roles` 中任意一节，会整体取代默认文件中的对应四节，避免已删除模型被默认文件补回。

---

## 2. 应用身份与 Web 服务：`app` / `webui` / `logging`

- **`app`**：
  - `timezone`：程序统一使用的 IANA 时区（默认 `Asia/Shanghai`），决定调度、日志、统计和会话切换窗口的日历时间，独立于操作系统时区。
  - `user_name` / `agent_name`：替换提示词与界面中的双方称呼；留空时提示词回退为 `user` / `assistant`，界面标题显示 `Tentacle`。
- **`webui`**：HTTP 监听地址 `host`（默认 `0.0.0.0`；Linux 部署脚本初始化为 `127.0.0.1`）与端口 `port`（默认 `8420`）。
- **`logging`**：日志等级 `level`（默认 `INFO`）及滚动日志文件输出路径 `logs/agent.log`、单文件大小上限与保留份数。

---

## 3. 记忆系统：`memory`

- **`memory.ladybug.path`**：内嵌 LadybugDB 图谱数据库路径（默认 `data/memory.lbdb`）。
- **`memory.embedding`**：指定嵌入模型角色 `model_role` 与向量维度 `dimensions`（默认 `1024`）。
- **`memory.search`**：控制检索召回策略，包括 Person/Concept 召回数 `top_k`、Event 详细结果数 `event_top_k`、正文返回模式 `content_mode`（摘录 `excerpt` 或完整原文 `full`）及是否附带候选索引（`include_index`、`index_top_k`）。详情见 [记忆检索与原文证据](MEMORY_RETRIEVAL.md)。
- **`memory.concept_alignment`** 与 **`memory.concept_dedup`**：控制写入前概念对齐自动开关（默认关闭 `enabled: false`）与历史概念扫描的评分角色 `model_role`、候选数 `top_k`、合并/独立概率门槛（`same_probability` / `different_probability`）及超时时间。详见 [写入前对齐](CONCEPT_ALIGNMENT.md) 与 [历史去重](MEMORY_CONCEPT_DEDUP.md)。

---

## 4. 自主运行：心跳 `heartbeat` 与定时调度 `scheduler`

- **`heartbeat`**：周期唤醒主 `AgentSession` 的心跳器。包含开关 `enabled`、间隔 `interval_minutes`（默认 `60`；Linux 安装器首装先关闭）、普通唤醒文本 `message`、是否附带进行中任务 `include_in_progress_tasks`、Gemini/Cloud 档位 `service_tier`、超时与重试次数，以及 **`session_switch`**（在指定程序时区窗口如 `01:00`–`07:00` 触发近况整理并开启新会话）。
- **`scheduler`**：与心跳正交的后台静默调度器配置，主要控制服务停机错过触发点后的补跑宽限窗口（`catch_up_grace_hours`、`interval_catch_up_grace_hours`）。详见 [定时任务与预约心跳](SCHEDULES.md)。

---

## 5. 工具、媒体与编程托管：`web_tools` / `media_inputs` / `files` / `terminal` / `subagents` / `coding`

- **`web_tools`**：公开网页搜索与提取工具（`web_search` / `web_fetch`）后端配置（支持 `parallel`、`tavily`、`deepseek`，默认按所选服务商自动读取 `PARALLEL_API_KEY` / `TAVILY_API_KEY` / `DEEPSEEK_API_KEY`）、超时与单次最大输出字符数，见 [公开网页工具](WEB_TOOLS.md)。
- **`media_inputs`**：是否自动解析分享的 B 站视频链接（`auto_bilibili`）及下载后端（本机 `local` 或远端协议服务 `http_jobs`），见 [音视频输入](MEDIA_INPUTS.md)。
- **`files`**：对话附件存储与远端上传策略（默认留存本地；可按需配置私有 GCS 或基于 `provider_ref` 复用的厂商文件接口）。
- **`terminal`**：`terminal_run` / `terminal_interact` 交互式终端默认 Shell（留空跟随 `$SHELL`）、工作目录（默认 `$TENTACLE_HOME/workspace`）、默认超时及拒绝执行的交互监控命令黑名单 `deny_commands`。
- **`subagents` 与 `coding`**：可选外部 `@deepseek-ai/dsh` 编程子代理托管开关（默认 `enabled: false`）、并发上限、轮次/时间预算与 `dsh_binary` / `dsh_home` 路径，见 [DSH 编程台](SUBAGENTS.md)。

---

## 6. 插件与 MCP 扩展：`plugins`

- **`plugins.items.<id>`**：管理内置与用户扩展插件的启用状态 `enabled`、权限信任状态 `trusted`、禁用工具列表 `disabled_tools` 及插件专属配置 `config`。
  - 默认内置插件包含 **`browser`**（默认开启 `enabled: true`）、**`harmony`**（默认关闭 `enabled: false`）、**`proton_mail`**（默认关闭 `enabled: false`）、**`qqbot`**（默认关闭 `enabled: false`）。说明见 [内置插件与插件管理](BUNDLED_PLUGINS.md)。
- **`plugins.mcp.<id>`**：外部 `stdio` 或 `streamable_http` MCP 服务配置（含环境变量名映射、Bearer 凭据变量名与工具屏蔽列表）。

---

## 7. 凭据安全约定

`config.yaml` 中只保存**环境变量名称**（如 `DEEPSEEK_API_KEY`、`QQ_APP_ID`），绝不存放真实密钥。实际密钥值保存于 `$TENTACLE_HOME/config/.env`（权限 `0600`），通过主机维护或在 WebUI「配置」/「插件」页面以纯写入模式录入；Web 界面、配置备份与导出记录默认都不包含 `.env` 明文内容（仅在启用 Web 密码或设备鉴权后允许通过设置页一次性口令加密流式导出迁移）。
