# 内置插件与插件管理使用指南

Tentacle 通过 WebUI 的 **插件与 MCP** 页面（`/plugins`）统一管理内置插件、本地扩展插件与外部 MCP 工具服务，配置保存到 `TENTACLE_HOME/config/config.yaml`，敏感凭据仅写入 `.env`。

---

## 1. 在 WebUI 中管理插件（`/plugins`）

- **默认折叠卡片**：每张本地插件卡片默认仅展示标题、来源（如 `内置插件`）、运行状态、摘要计数以及启用开关；点击右上角 **`展开配置`** 后可查看申请权限、环境变量、详细配置项与注册的工具列表。
- **启用 / 停用与保存生效**：切换开关或修改配置后点击 **`保存配置`**；状态出现 `待重启生效` 徽章时，点击页面右上角 **`重启应用使变更生效`** 即可生效（MCP 服务额外支持 **`立即重连`** 热重载）。
- **安全录入 `.env` 凭据**：展开包含 `环境变量 / 凭据` 区块的插件卡片，点击 **`录入`** 或 **`更新`** 提交密钥值。服务端只会写入 `.env`，不会在页面回显或记录到对话与日志。
  - **缺失（红色）**：必填环境变量未设置，插件会保持待审核/未就绪状态。
  - **可选环境变量**（如鸿蒙推送配置）：未填时插件仍可正常启用基础功能，按需在面板录入即可。
- **内置插件保护**：`内置插件` 随应用版本打包分发，支持正常开关禁用、编辑配置、甚至在 `$TENTACLE_HOME/plugins/<id>/` 放置同名目录覆盖，但不允许在面板删除内置源码目录。
- **安装用户本地插件**：将插件目录放入 `$TENTACLE_HOME/plugins/<id>/`，或在对话中让主 Agent 调用 `plugin_install_local` 暂存；新插件默认处于未批准状态，需在 `/plugins` 查看其声明权限并点击 **`批准并启用`**。

---

## 2. 内置插件列表

### `browser` — 浏览器自动化（[manifest.yaml](../plugins/bundled/browser/manifest.yaml)）

封装 `agent-browser` CLI 控制本机 Docker Neko（Chromium + WebRTC + 本机回环 CDP），适用于需要登录态、页面点击/填表、视觉截图或人工随时接管的场景；暴露工具名为 **`p_browser__browser`**。

- **一键部署与 WebUI 托管**：
  - 宿主机安装 `agent-browser`（`npm i -g agent-browser`，无需本地 Chrome），首次在 Linux 服务器执行 `sudo tentacle browser install --install-docker` 即可完成 Docker Neko 容器与受控启停通道初始化，详见 [Docker Neko 可视化浏览器指南](BROWSER_NEKO.md)。
  - 之后可直接在 WebUI `/plugins` 展开 `browser` 卡片修改全部 Neko 参数（`live_view_url`、`neko_bind`、`neko_http_port`、`neko_cdp_port`、`neko_mux_port`、`neko_nat_ip`、`neko_screen`、`neko_shm_size` 等），保存后直接点 **同步并启动 / 重启 / 停止** 管理容器。
  - 填写 `live_view_url` 并保存后（无需重启），WebUI 左侧栏 **「插件页面」** 分组与命令面板会立即显示 **「可视化浏览器」**（`/browser`）入口，卡片顶部也会提供 **「打开可视化界面」** 直达按钮；留空则自动隐藏。
- **`agent-browser` 行为与安全配置项**：
  - `executable`：`agent-browser` 二进制路径（留空自动探测）。
  - `allowed_domains`：允许访问的域名白名单（在 `/plugins` 中**每行填写一个域名**，留空表示不限制）。
  - `allow_eval`：是否允许页面内 `eval` 脚本执行（默认关闭以降低脚本风险）。
  - `max_output` / `timeout`：单次命令输出上限与超时秒数。
- **视觉截图**：调用工具时可配合 `screenshot_as_image: true` 让支持多模态的模型直接查看页面截图。

---

### `harmony` — 鸿蒙手机联动（[manifest.yaml](../plugins/bundled/harmony/manifest.yaml)）

默认关闭（`enabled: false`），按需在 `/plugins` 启用后重启生效。为鸿蒙客户端提供设备授权注册、一次性手机闹钟（工具名为 **`p_harmony__phone_alarm`**）、断线续跑与华为实况窗推送支持，数据独立保存在 `TENTACLE_HOME/data/plugins/harmony/phone.db`。

- **插件配置项**：
  - `action_wait_seconds`：模型调用闹钟后同步等待手机端返回执行回执的秒数（默认 `12` 秒；超时会明确返回 `pending` 提示用户打开手机确认，绝不谎称已设置成功）。
  - `allow_alarms`：是否允许模型向已绑定手机下发一次性闹钟。
  - `allow_live_view`：是否允许接收手机端 `client_meta.live_view` 实况窗关联并在回合结束后推送信令。
- **华为云端推送（可选环境变量）**：
  - 若只需连接与设置闹钟，无需填写任何推送环境变量。
  - 若需要实况窗云端状态推送，在 `/plugins` 的 `harmony` 卡片中展开环境变量区块，按实际方案录入以下任意一种（均安全保存至 `.env`）：
    - 推荐：填写服务账号 JSON 文件路径到 `HUAWEI_PUSH_CREDENTIALS_FILE`（该私钥文件受敏感路径保护，禁止被附件工具读取发送）。
    - 或逐项填写 `HUAWEI_PUSH_PROJECT_ID`、`HUAWEI_PUSH_KEY_ID`、`HUAWEI_PUSH_SUB_ACCOUNT`、`HUAWEI_PUSH_PRIVATE_KEY`（及可选 `HUAWEI_PUSH_AUDIENCE`、`HUAWEI_PUSH_TEST_MESSAGE`）。
- **端侧接入规格与客户端仓库**：见 [鸿蒙客户端接入与插件说明](HARMONY_CLIENT.md) 与 [tentacle-harmony](https://github.com/shuunO-O/tentacle-harmony)。

---

### `proton_mail` — Proton 加密邮箱助手（[manifest.yaml](../plugins/bundled/proton_mail/manifest.yaml) · [README](../plugins/bundled/proton_mail/README.md)）

基于 [proton-cli](https://github.com/roman-16/proton-cli) 访问 Proton 端到端加密邮箱，无需 Proton Bridge 或桌面环境（默认关闭：`enabled: false`）；工具名为 **`p_proton_mail__proton_mail`**，支持 `status`、`list`、`read`（默认读后自动标已读）、`mark_read`、`mark_unread`、`send` 操作，并在 `before_heartbeat` 时自动注入未读邮件简报。

- **安装与登录**：`curl -fsSL https://raw.githubusercontent.com/roman-16/proton-cli/main/scripts/install.sh | sh`，然后执行 `proton account login`。
- **二进制探测**：若填写 `executable` 则仅校验该路径；留空时依次探测 `PATH`、`~/.local/bin/proton`、`/usr/local/bin/proton` 与 `/usr/bin/proton`；未安装时工具会返回安装与登录引导。
- **常用配置项**：
  - `executable`：proton 可执行文件路径，留空自动探测。
  - `heartbeat_unread_limit`：心跳简报最多列出的未读邮件数（`0`–`20`），设为 `0` 关闭自动简报。
  - `allow_send`：是否允许对外发送邮件（默认 `false` 只读，按需开启以降低 Prompt Injection 误发风险）。
- **数据边界**：不申请 `fs:plugin_data`，登录态与本地缓存均由 proton-cli 管理；插件不额外保存邮件或凭据。

---

### `qqbot` — QQ 官方机器人（[manifest.yaml](../plugins/bundled/qqbot/manifest.yaml)）

接入 QQ 官方开放平台机器人，以 C2C 私聊方式接收消息并回复（默认关闭：`enabled: false`）；数据与最近私聊对象持久化于 `TENTACLE_HOME/data/plugins/qqbot/chat.json`。启用并配置有效凭据后，主模型统一网关工具 `send_message` 会开放 `platform="qq"` 通道。

- **必填凭据环境变量**：默认映射 `QQ_APP_ID`（`app_id_env`）与 `QQ_CLIENT_SECRET`（`client_secret_env`），可在 `/plugins` 展开卡片后直接安全写入 `.env`。
- **常用配置项**：
  - `allow_from`：C2C 私聊用户 `user_openid` 白名单（每行一个；留空表示允许所有符合平台权限的私聊用户）。
  - `default_to`：可选默认主动发送目标 `openid`（未指定 `target` 且尚未记录最近私聊对象时回退使用）。
  - `proxy`：显式 HTTP 代理地址，留空直连且不继承进程环境变量代理。
  - `max_chunk` / `split_messages` / `split_max_segments`：出站单段字符上限、按空行语义分段开关及最大分段数。
- **详细开通与使用说明**：见 [接入 QQ 机器人](QQBOT_SETUP.md)。

---

## 3. 外部 MCP 工具服务

在 `/plugins` 切换到 **MCP 服务** 标签页点击 **`添加 MCP 服务`**：

- 支持 **`stdio`**（本地子进程命令 + 参数 + `env` + `env_secrets` 环境变量映射）与 **`streamable_http`**（远端 URL + `auth_bearer_env` + 请求头映射）。
- 服务注册后的工具统一命名为 `mcp_<server_id>__<tool_name>`，可通过 `disabled_tools` 屏蔽特定工具；更改后可直接在卡片点击 **`立即重连`**，无需重启整个主服务。
