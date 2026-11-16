# 接入 QQ 机器人（内置插件 `qqbot`）

Tentacle 通过内置插件 `plugins/bundled/qqbot` 接入 QQ 官方开放平台机器人，以 C2C 私聊方式接收消息并回复。聊天进入 Tentacle 的共享会话历史；请先确认自己接受这项存储行为。

## 创建机器人和开通私聊

在 [QQ 开放平台](https://q.qq.com/) 创建机器人，记下 AppID 和 AppSecret。按平台要求申请或配置 C2C 私聊能力；开发阶段可以使用开放平台提供的沙箱测试账号。未获得私聊权限时，即使连接成功也可能收不到消息。

## 配置凭据与启用插件

1. 在 Tentacle 运行环境的 `.env` 中填写密钥（也可在 WebUI `/plugins` 页面中直接写入 `.env`，界面不会回显明文）：

```dotenv
QQ_APP_ID=YOUR_APP_ID
QQ_CLIENT_SECRET=YOUR_APP_SECRET
```

2. 在 WebUI `/plugins` 页面打开并保存 **QQ 官方机器人（`qqbot`）** 配置，或在 `config.yaml` 中启用：

```yaml
plugins:
  items:
    qqbot:
      enabled: true
      trusted: true
      config:
        app_id_env: QQ_APP_ID
        client_secret_env: QQ_CLIENT_SECRET
        allow_from:
          - YOUR_USER_OPENID
```

`.env` 已被 Git 忽略，不要把真实凭据提交到仓库、放进 `config.yaml` 或发到聊天中。

留空 `allow_from` 表示接受所有符合平台权限的私聊用户。保存配置并重启 Tentacle 后，`send_message` 工具会自动开放 `platform="qq"` 通道。

## 使用范围

- 当前插件处理 QQ C2C 私聊，不处理群聊和频道消息。
- QQ 与 Web 共用同一个 Agent 会话；定时任务和心跳不会因此自动发送到 QQ。
- 启用通道插件后，模型可通过 `send_message(platform="qq", text=...)` 外发消息；没有指定目标时发给最近一次私聊对象，也可用插件配置中的 `default_to` 设置默认 `openid`（最近会话持久化于 `data/plugins/qqbot/chat.json`）。
- 出站长消息会分段；文件单个上限为 20 MiB。见 [附件发送说明](FILE_OUTPUTS.md)。
- 未启用插件或缺少凭据时不会暴露 `qq` 通道，Web 服务仍正常运行。

连接问题优先检查 AppID/AppSecret、平台私聊权限和测试账号状态。只在 QQ 开放平台允许的范围内测试，不要尝试绕过平台限制。
