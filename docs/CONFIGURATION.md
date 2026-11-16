# 应用配置管理

Tentacle 通过 WebUI「系统配置中心」（`/config`）与离线 CLI 管理运行配置，不增加独立配置数据库。各个配置段的具体含义见 [配置项说明](CONFIG_GUIDE.md)。

运行配置由两份 YAML 文件和 `.env` 共同组成：

- `config.defaults.yaml`：仓库随版本提供的默认配置，不填写单机地址、凭据或私人称呼。
- `$TENTACLE_HOME/config/config.yaml`：本机用户覆盖配置；只需填写需要覆盖的设置，通过 Web 保存时会自动创建。

主程序、日志、Web 配置管理和离线 CLI 使用同一套读取逻辑（`config/files.py::ConfigFiles.read`）：先读取版本内默认文件，再叠加 `$TENTACLE_HOME/config/config.yaml`。普通字典递归覆盖；数组、标量和显式 `null` 整项替换。例如只写 `heartbeat: {interval_minutes: 30}` 会保留默认的 `session_switch` 设置；`[]` 清空列表，不追加默认条目。删除用户文件里的普通字段后，下次加载恢复继承默认值。

`config_version` / `providers` / `models` / `roles` 构成完整的模型接入配置：用户文件声明其中任一节时，整组使用用户配置，避免已删除的条目又被默认文件补回。如果用户尚未配置模型接入，Web 首次修改相关条目时会自动固定当前完整接入目录，再写入修改。

首次部署可直接打开 WebUI 的「系统配置中心」，在主机或页面配置好登录认证后，依次添加供应商连接、添加或导入模型、为角色（至少 `chat`）分配模型，再重启应用。系统内置角色的 `model` 默认为空（`null`）；`chat` 绑定模型后即可对话，其余角色按需绑定。

系统提示词可使用 `{{user_name}}` / `{{agent_name}}`，分别取自 `app.user_name` / `app.agent_name`；空值回退为 `user` / `assistant`。替换覆盖主提示词全部区块（含 skills 简介和近况）、压缩、记忆提取及子 agent 的系统提示词模板，生成的主提示词预览也使用替换后的内容，源模板保持原样。Web 界面统一使用 `app.agent_name`（留空显示 Tentacle）。

在「记忆系统 → 记忆去重」可调整新记忆写入前自动对齐的开关、两个去重流程使用的 `model_role`、概率门槛与超时；对应的 System One 供应商、模型与凭据环境变量统一在「模型与连接」管理。

---

## 供应商、模型与角色

模型配置采用三层结构（YAML 中以 `config_version: 2` 标识）：

- **供应商连接（`providers`）**：管理接入类型、服务端点、认证环境变量引用、网络代理及 Cloud 项目/区域；
- **模型目录（`models`）**：引用所属供应商，管理模型名称、协议、能力与默认生成参数；
- **业务角色（`roles`）**：选择模型、管理可用核心工具、迭代上限、会话策略及参数覆盖。

示例：

```yaml
config_version: 2
providers:
  primary:
    type: deepseek
    api_key_env: DEEPSEEK_API_KEY
    proxy: ""
models:
  fast:
    provider: primary
    name: example-model
    api_style: chat
    context_window: 1048576
    defaults:
      temperature: 0.3
roles:
  chat:
    model: fast
    max_iterations: 50
    tools:
      enabled: [get_time]
  compress:
    model: fast
    overrides:
      temperature: 0.1
```

页面中的「备注」（配置中为 `label`）仅作展示标签，不参与引用或标识生成。下拉选择同时保留备注与标识，避免同名备注无法区分；备注修改后立即生效，无需重启。

新增供应商的标识随接入类型自动生成（如 `deepseek`、`google_vertex`，自定义 OpenAI 兼容连接默认 `provider`）；新增或导入模型的标识默认为 `供应商标识/模型名称`（超长截断、重名自动追加 `-2` 等序号），可手动修改。

接入类型由 `config/providers.py::PROVIDERS` 集中定义，包含 `deepseek`、`openai`、`openai_compatible`、`google_developer`、`google_vertex`、`openrouter` 与 `typesafe`。同类连接可创建多个，各自独立配置地址、代理与认证。

### 从供应商读取模型

在供应商连接点击「读取模型 / 发现模型目录」，可直接向该连接查询可用模型并勾选预览导入（仅读取与预览均不写盘，确认差异后需点击保存，导入不改变 `chat` 绑定）：

- OpenAI / 兼容连接与 DeepSeek 查询连接地址下的 `/models`；Gemini Developer 使用原生 `models` 目录与 `x-goog-api-key`；Vertex AI 使用 ADC 查询 `publishers/google/models`。
- 如供应商使用非默认目录接口，可在 `providers.<id>.models_url` 填写完整 HTTP(S) 地址（可带查询参数，认证沿用连接，不得把密钥写入 URL），保存后立即生效。
- 查询受总超时、响应体积与分页上限限制，不跟随重定向。不支持目录接口时仍可手动添加模型。

接口依据：[DeepSeek 模型目录](https://api-docs.deepseek.com/api/list-models/)、[Gemini 模型目录与分页](https://ai.google.dev/api/models)、[Vertex 官方接口定义](https://github.com/googleapis/googleapis/blob/master/google/cloud/aiplatform/v1beta1/model_garden_service.proto)。

### Web 关系树、继承与统计联动

Web「系统配置中心」将供应商连接 → 模型 → 角色组织为关系树与三级跳转链，桌面端支持左侧树导航与右侧编辑联动，窄屏自动切为层级单页下钻。

- 已被模型/角色、上传器或余额查询引用的连接不能删除；被角色引用的模型不能删除；系统内置角色不能删除（可更换绑定的模型）。
- 角色覆盖参数未声明时继承所属模型的 `defaults`（界面支持「恢复继承」移除覆盖）；`explicit null` 表示不向接口发送该参数，使用供应商默认行为。
- 主对话固定使用 `chat` 角色，模型切换通过 Web 或 `/model use MODEL_ID` 修改 `roles.chat.model`；所选模型或所属供应商若有修改等待重启，整批等待重启生效，避免新旧连接混用。
- 统计页顶部按各供应商连接配置展示余额：可在连接配置中设置 `balance_mode`（`auto` / `none` / `deepseek`）和 `balance_url`（留空则使用 API 地址加 `/user/balance`）。
- 附件上传器可通过 `files.uploaders.<name>.provider_ref` 复用已配置的供应商连接（不可同时重复填连接字段）。

---

## 预览、保存与动态应用

修改字段后点击「预览保存」，核对 `config.yaml` 的字段差异后确认写入。文件若同时被外部修改，旧 `revision` 会返回 `409` 拒绝覆盖并要求刷新。

| 范围 | 生效方式 |
| --- | --- |
| 备注（`label`）、`models_url`、`balance_mode`、`balance_url` | 保存后立即生效，无需重启 |
| `roles.chat` 的模型切换（目标模型与供应商就绪时）、生成参数覆盖、工具列表 | 在唯一 `AgentSession` 锁内等待当前回合结束后动态发布 |
| `heartbeat` 间隔、普通心跳与交接配置、档位与超时 | 回合边界动态生效；间隔改动从应用时刻重新计时 |
| 供应商连接变更、新增/删除实体、后台角色、Web 地址端口、日志、网页工具与媒体配置 | 写入文件后按页面提示重启应用生效 |

模型切换生效时会校验会话兼容性；不兼容的接口/协议切换会自动将历史收敛为安全交接副本并保留本地附件引用，详见 [模型切换与会话交接](SESSION_HANDOFF.md)。动态应用失败时保留原有运行客户端，文件修改保留以便修正后重试「应用已保存配置」。

### 页面内重启

页面右上角与配置中心的「重启」复用设备与密码鉴权，并在停服前执行本地配置校验。确认后停止接收新对话与写入，等待当前回合结束后优雅关闭各组件并用当前 Python 解释器 `os.execv` 原进程重启（不调用 `sudo` 或外部命令）。

---

## 文件事务、备份恢复与 `.env` 凭据

`config/store.py::ConfigStore` 提供字段定义、校验、乐观并发版本号校验、跨进程文件锁、局部 YAML 修改、自动备份与原子替换。

- **注释与格式保留**：通过文本级局部 YAML 补丁修改目标节点，保留注释与顺序；YAML 重复键、别名和 merge 键会被拒绝。
- **备份与恢复**：每次实际写入前在 `data/config_backups/` 创建备份（权限 `0600`）；配置中心保留最近 20 份历史并支持差异预览回滚（三层模型实体与受管字段一起按事务恢复）。
- **`.env` 纯写入安全隔离**：在环境变量字段旁点击设置凭据，WebUI 只将密钥值写入 `$TENTACLE_HOME/config/.env`，`config.yaml` 只保存变量名；界面、接口和配置备份都不包含 `.env` 明文内容（仅支持在启用认证后于设置页手动执行一次性口令加密的迁移流式导出）。

---

## 离线命令行

`scripts/configure.py` 不启动应用、不加载 `.env`、不联网，可在服务停机或页面无法打开时做检查与修复：

```bash
.venv/bin/python scripts/configure.py show
.venv/bin/python scripts/configure.py check
.venv/bin/python scripts/configure.py set --set 'heartbeat.interval_minutes=30'
.venv/bin/python scripts/configure.py set --set 'heartbeat.interval_minutes=30' --revision SHA256 --apply
.venv/bin/python scripts/configure.py history
.venv/bin/python scripts/configure.py restore --backup BACKUP_ID
.venv/bin/python scripts/configure.py restore --backup BACKUP_ID --revision SHA256 --apply
.venv/bin/python scripts/configure.py create --kind providers --id second --value '{"type":"deepseek","api_key_env":"SECOND_KEY"}'
.venv/bin/python scripts/configure.py delete --kind providers --id second
.venv/bin/python scripts/configure.py set --set 'roles.chat.overrides.temperature={"$inherit":true}'
```

实际写入与删除均需传入 `show` 或预览返回的 `--revision` 以及 `--apply`。

---

## 验证

后端配置回归位于 `tests/test_configuration.py` 与 `tests/test_provider_config.py`；Web 前端合成预览与 Playwright 浏览器验收流程见 [Web 前端架构与开发验证](WEB_FRONTEND.md)。
