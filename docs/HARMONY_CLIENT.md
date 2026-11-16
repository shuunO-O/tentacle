# 鸿蒙客户端接入与插件说明

Tentacle 通过内置插件 [plugins/bundled/harmony/](../plugins/bundled/harmony/manifest.yaml)（默认**关闭**：`enabled: false`）为独立维护的鸿蒙客户端（仓库：[tentacle-harmony](https://github.com/shuunO-O/tentacle-harmony)）提供手机设备注册、一次性手机闹钟、断线续跑与华为实况窗状态推送能力。扩展端点与多端适配逻辑全部通过宿主通用代理与 `/api/chat/send` 接入，独立于核心服务主逻辑。

---

## 1. 启用插件与配置项

如需接入鸿蒙客户端，可在 WebUI `/plugins` 页面开启 **鸿蒙手机联动（`harmony`）** 并保存重启，或在 `config.yaml` 中启用：

```yaml
plugins:
  items:
    harmony:
      enabled: true
      trusted: true
      config:
        action_wait_seconds: 12
        allow_alarms: true
        allow_live_view: true
```

- `action_wait_seconds`：模型调用闹钟后同步等待手机端回执的秒数（默认 `12` 秒；超时明确返回 `pending` 提示用户打开手机确认，绝不虚报已设置成功）。
- `allow_alarms`：是否允许模型通过 **`p_harmony__phone_alarm`** 向当前 HTTP 回合绑定的手机下发一次性闹钟。
- `allow_live_view`：是否允许接收手机端 `client_meta.live_view` 实况窗关联并在回合结束后推送状态更新。

插件私有数据与设备绑定记录保存在 `TENTACLE_HOME/data/plugins/harmony/phone.db`（首次启用使用后创建）。

---

## 2. REST 接口与鉴权协议

扩展路由由宿主统一挂载在 `/api/plugins/ext/harmony` 下，继承 `DeviceAccessGate` 与 Web 登录鉴权，并额外要求已注册的设备 ID 与独立 secret（注销设备或登录变化后立即失效）：

| 能力 | 接口 | 请求体 / 响应 |
| --- | --- | --- |
| 注册 / 刷新推送令牌 | `POST /api/plugins/ext/harmony/register` | 返回 `{"registered": true, "push_configured": bool}` |
| 轮询待执行动作与实况窗状态 | `GET /api/plugins/ext/harmony/state` | 返回 `{"actions": [...], "rounds": [...]}` |
| 上报闹钟执行回执 | `POST /api/plugins/ext/harmony/actions/{action_id}/receipt` | 返回 `{"ok": true}` |
| 注销设备绑定 | `DELETE /api/plugins/ext/harmony/register` | 返回 `{"ok": true}` |

鉴权请求头需同时携带 `X-Tentacle-Device`、`X-Tentacle-Device-Secret` 及主服务登录凭证（`Authorization` Bearer Token 及设备 Cookie）。

---

## 3. `/api/chat/send`、工具与实时事件

- **发起对话与实况窗载荷**：手机端继续请求 `POST /api/chat/send`，通过 `platform: "harmony"` 标识来源，并通过通用扩展字段 `client_meta` 传入实况窗参数：

```json
{
  "text": "明早九点提醒我开会",
  "platform": "harmony",
  "request_id": "turn-uuid-123",
  "client_meta": {
    "live_view": {
      "activity_id": 11,
      "event": "PROGRESS"
    }
  }
}
```

- **断线保活约束**：仅当携带有效 `X-Tentacle-Device` + `X-Tentacle-Device-Secret` 时，服务端会对本轮对话启用断线保活（`asyncio.shield`），并在回合结束后更新状态与异步推送实况窗；传入 `client_meta.live_view` 时要求设备已启用 `live_view=true`、`platform == "harmony"` 且提供唯一 `request_id`（`activity_id: 1..2147483647`、`event: "PROGRESS"`）。
- **模型工具名**：`p_harmony__phone_alarm`（只作用于当前 HTTP 回合绑定的手机设备，不可跨设备越权指定；`status=pending` 仅表示入队，`executed` 才表示手机系统提醒创建成功）。
- **WebSocket 事件**：`{"type": "phone_action", "data": {"device_id": "..."}}`。

---

## 4. 华为云端推送凭据（可选）

如果只需要手机连接与下发闹钟，无需配置推送环境变量；如需开启实况窗云端状态推送，可在 WebUI `/plugins` 的 `harmony` 卡片中将环境变量安全写入 `.env`（支持以下二选一，均不回显到页面、对话或日志）：

- **推荐**：填写服务账号 JSON 私钥文件路径到 `HUAWEI_PUSH_CREDENTIALS_FILE`（该私钥文件受 [attachments/outgoing.py](../attachments/outgoing.py) 敏感路径防外泄保护）。
- **或逐项填写**：`HUAWEI_PUSH_PROJECT_ID`、`HUAWEI_PUSH_KEY_ID`、`HUAWEI_PUSH_SUB_ACCOUNT`、`HUAWEI_PUSH_PRIVATE_KEY`（可选 `HUAWEI_PUSH_AUDIENCE`、`HUAWEI_PUSH_TEST_MESSAGE`）。

定向验证命令见 [开发环境](DEVELOPMENT.md)。
