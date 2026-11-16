# 对话中的文件发送

Agent 先生成本地文件，再在助手正文中输出：

```text
文件整理好了：
[file:tmp/报告.pdf]
```

支持行内标记及一条消息内多个标记，按正文顺序处理。相对路径以 TENTACLE_HOME 为基准，
也可用服务账号能读取的绝对路径；不要求放到固定目录，不复制文件、不上传到 GCS，
出站本身不新增数据库字段或发送工具。路径首尾空白会被去掉，不用引号包裹，不能包含换行或 `]`。
带反斜杠的 `\[file:path]`、同一行的反引号代码和 Markdown 围栏中的标记保持文字。
标记未完整输出时保留文字，闭合后才显示附件。

历史、模型上下文与 WS 仍保存原始正文。历史下载访问的是原路径的**当前文件**：
文件移动/删除后无法下载，同一路径覆盖后下载的是新内容。没有隐式归档副本。
模型可用法由对应提示词说明；文件发送通过现有 `send_message` 工具完成。

用户文件上传、按日期保存与文档读取工具见 [FILE_INPUTS.md](FILE_INPUTS.md)。

## Web 与鸿蒙

Web 只对助手正文渲染下载按钮，思考、工具输出、用户消息不渲染。
复制回复保留 `[file:path]` 原文。点击才发请求，不在流式输出/重放历史时自动下载。
文件名按纯文本转义，Markdown 中的符号不改变路径。

下载请求：`GET /api/files/download?path=URL编码后的路径`。
继承普通 REST 的密码 Bearer token、设备 cookie、设备冻结及新设备入口限制；
不接受 query token、不生成公开分享链接。Web 复用 `api` 鉴权处理，读取 Blob 后保存。

- 成功：二进制附件，`Content-Disposition: attachment; filename*=UTF-8''...`。
  服务端以 64 KiB 块流式读取，响应不缓存并设置 `nosniff`。
- `401`：现有设备/密码鉴权未通过；入口关闭的匿名设备也可能被外层拒绝为 `403`。
- `403`：敏感材料、没有读取权限、目录、硬链接或特殊文件。
- `404`：文件不存在/已删除；`400`：非法或无法解析的路径。

鸿蒙后续可按相同语法渲染按钮，用已有 Bearer 和设备 cookie 下载并交给系统保存。
正文格式保持不变；历史记录仍保存原始正文，不附加文件副本或额外元数据。

## QQ 单聊

仍使用现有 `send_message`，`text` 中放同样的标记：

```json
{"platform":"qq","text":"文件整理好了：\n[file:tmp/报告.pdf]"}
```

`QQAdapter._deliver`（位于 `plugins/bundled/qqbot/adapter.py`）在文本分段/硬分片前解析完整消息，文字、文件交替按顺序发送。
普通文件经 `/v2/users/{openid}/files`，发送 `file_type=4`、原文件名、base64 `file_data`、
`srv_send_msg=false`；取到 `file_info` 后再调用 `/messages` 的 `msg_type=7` 发送附件。
图片、音频和视频也作为普通文件发送，本版不转码或发送为原生媒体消息。
协议形状参考腾讯维护的 [DTO](https://github.com/tencent-connect/qqbot-agent-sdk/blob/main/src/qqbot_agent_sdk/dto.py)
及 [REST 客户端](https://github.com/tencent-connect/qqbot-agent-sdk/blob/main/src/qqbot_agent_sdk/api_client.py)。

单文件上限 **20 MiB**，空文件拒绝；读取和 base64 编码放在线程中，避免阻塞主循环。
此限制是本应用为小文件直传设置的内存上限，不代表 QQ 平台允许的最大值。
无需把本地文件暴露成公网 URL，不引入新 SDK，也不实现分片上传。

`sent_chunks` / `total_chunks` 现在包含文字消息和文件消息，成功时另带 `sent_files`。
上传成功不等于发送成功：最终消息接口确认后才计数并返回 `status=sent`。
中途失败停止后续发送，反馈已确认条数，不自动重发。未确认/失败发送沿用现有
VisibleMessages 规则，不写成已发送消息。QQ 的可用权限与平台限额以 QQ 开放平台配置为准。

## 共用路径检查

`attachments/outgoing.py::open_outgoing_file` 检查请求路径和解析后的真实路径，拦截：

- `.env*`、常见 GCP JSON 凭据、当前 `GOOGLE_APPLICATION_CREDENTIALS` 指向的文件。
- SSH、AWS、Azure、Kubernetes、gcloud、credentials/secrets、Git 认证目录和常见私钥文件。
- GitHub 令牌、GitHub CLI `hosts.yml`、Web 设置与设备认证文件（包括常见备份后缀）。
- `/proc`、`/sys`、`/dev`、非普通文件及硬链接。

允许指向普通文件的软链接，但真实目标仍受相同限制；逐层无跟随方式打开已解析路径，
防止检查后替换软链接改变读取目标。Web 还保护实际传入的 WebSettings 自定义路径。
本功能按路径保护已知认证材料，不扫描任意文档内容，也不能识别改名复制或压缩包内的秘密。

## 开发验证

`tests/test_outgoing_files.py` 使用临时文件、临时设置和 fake QQ API 验证解析、两端语法一致、
鉴权/下载、敏感路径和链接检查，以及上传/发送顺序与失败确认；不读生产数据或真实发消息。
完整测试在 GitHub 托管 runner 执行。本机仅做语法、改动范围 lint 和浏览器合成预览检查。
