# Proton 邮箱助手（`proton_mail`）

Tentacle 内置插件，封装 [proton-cli](https://github.com/roman-16/proton-cli) 的邮件能力，让 Agent 可以在无桌面环境的服务器上访问 Proton 端到端加密邮箱。无需安装 Proton Bridge，也不需要本地桌面客户端。

插件注册工具 **`p_proton_mail__proton_mail`**，并在每次心跳前扫描未读邮件，有新邮件时自动向心跳注入简报。

---

## 1. 安装与登录

插件不随附 proton 二进制，需要先在服务器上安装并登录：

```bash
curl -fsSL https://raw.githubusercontent.com/roman-16/proton-cli/main/scripts/install.sh | sh
proton account login
```

- 安装脚本默认写入 `~/.local/bin/proton`，请确认该目录在服务进程的 `PATH` 中。
- 登录会提示输入 Proton 邮箱、密码及二次验证；成功后会话保存在 proton-cli 自己的配置目录，插件不再接触密码。
- 验证安装：`proton version`；验证登录：`proton account get`。

插件按以下顺序探测可执行文件：

1. 插件配置 `executable`（WebUI `/plugins` 中填写）。
2. `PATH` 中的 `proton`（`shutil.which`）。
3. `~/.local/bin/proton`。
4. `/usr/local/bin/proton`、`/usr/bin/proton`。

找不到二进制时，`status` / `list` / `read` / `send` 都会返回安装与登录引导，而不是抛出异常。

---

## 2. 启用插件

1. 打开 WebUI **插件与 MCP**（`/plugins`）。
2. 展开 **Proton 邮箱助手**，确认权限并开启启用开关。
3. 如需自定义路径或心跳简报条数，修改配置后保存，并按提示重启应用生效。

默认关闭（`enabled: false`），不会主动执行任何 `proton` 命令。

### 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `executable` | 空 | proton 可执行文件路径；留空自动探测。 |
| `heartbeat_unread_limit` | `5` | 心跳简报最多列出的未读邮件数（`0`–`20`）；设为 `0` 关闭自动简报。 |

---

## 3. 工具用法

调用 `p_proton_mail__proton_mail`，通过 `operation` 选择操作：

| operation | 作用 | 主要参数 |
| --- | --- | --- |
| `status` | 查看邮箱地址、用户名、会话解锁状态与容量使用。 | — |
| `list` | 列出收件箱邮件，默认 10 封，最多 50 封。 | `unread_only`、`limit` |
| `read` | 读取指定邮件全文（纯文本渲染）。 | `message_id` |
| `send` | 发送纯文本邮件。 | `to`、`subject`、`body` |

示例：

```json
{"operation": "list", "unread_only": true, "limit": 5}
```

```json
{"operation": "read", "message_id": "5bH2mQxK"}
```

```json
{"operation": "send", "to": "alice@example.com", "subject": "周报", "body": "本周进展见下……"}
```

- `limit` 超出 `1`–`50` 时回退为默认值 `10`。
- `send` 通过参数数组调用 proton CLI，不经过 shell；发送超时为 45 秒。
- 所有命令均带 `--no-input --no-color`，避免在无人值守环境中等待交互或输出颜色控制符。

---

## 4. 心跳未读简报

插件订阅 `before_heartbeat` 钩子：每次心跳前以 10 秒超时查询未读邮件（最多 `heartbeat_unread_limit` 封），有未读时向心跳注入类似简报：

```text
📬 Proton 邮箱当前有 3 封未读邮件：
  1. [Alice] 项目进度 (ID: 5bH2mQxK...)
  2. [GitHub] New issue (ID: 9cKd2mPq...)
```

- 没有未读、未安装、未登录或查询超时时静默跳过，不影响心跳主流程。
- 未读数超过配置上限时，简报仍会显示邮箱返回的未读总数。

---

## 5. 权限与数据

| 权限 | 用途 |
| --- | --- |
| `tools:register` | 注册 `proton_mail` 工具。 |
| `hooks:mutate` | 在 `before_heartbeat` 中注入未读简报。 |

插件不申请 `fs:plugin_data`，不额外保存邮件、凭据或索引；登录态与本地缓存完全由 proton-cli 管理。邮件正文仅在工具调用结果中返回给当前会话，是否写入记忆由 Tentacle 的记忆策略决定。

---

## 6. 故障排查

| 现象 | 处理 |
| --- | --- |
| 提示未检测到 proton CLI | 执行上方安装命令，或在插件配置中填写 `executable` 绝对路径。 |
| 提示登录失败/会话过期 | 在服务器终端执行 `proton account login`，完成后重试。 |
| `proton account get` 正常但插件调用失败 | 检查服务进程的 `HOME`、`PATH` 与文件执行权限，必要时显式配置 `executable`。 |
| 心跳没有简报 | 确认插件已启用、`heartbeat_unread_limit > 0`，且 `proton account get` 与未读查询正常。 |
| 命令超时 | 检查网络与 Proton API 可达性；发送邮件默认超时 45 秒。 |

更多 proton-cli 用法见官方命令参考：<https://proton-cli.lerchster.dev/about/commands/>。
