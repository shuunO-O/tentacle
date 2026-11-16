# 音视频输入

Web 聊天的附件按钮、拖拽和文件粘贴可上传图片、音频及视频。音视频先校验实际容器、
轨道、时长与体积，再流式上传到已有私有 GCS 或 Gemini Files。Gemini 直接读取媒体，
无需单独转录模型或抽帧服务。当前没有麦克风录制及音视频输出功能。

分享 B 站 `https://www.bilibili.com/video/BV…`、`av…` 或 `https://b23.tv/…`
链接时，共享 AgentSession 通过选定的下载后端获取第一个视频并附到本轮输入，Web/QQ 文本入口均适用。
长链接中的 `p` 指定单个分 P；不下载全集、收藏夹、直播及会员/登录内容，不读取浏览器 Cookie。
失败会显示宿主提示并在模型输入中标明未取得视频，原文字消息仍继续处理。
目前只支持 B 站，不把任意网页 URL 直接传给下载器。

默认单文件最多 **64 MiB、10 分钟**，单轮最多 10 个附件（含自动获取的视频）。
B 站选择最高 480p 的 MP4 视频和 M4A 音轨，ffmpeg 仅合并，不转码；下载限速 2 MiB/s、
单分片并发、不重试；`local` 后端最多 180 秒。媒体上传/下载串行，本机下载临时空间监控上限 128 MiB，
任务退出清理临时文件，超时/取消回收子进程树。本地已上传副本按现有图片规则保留；
GCS 对象跨 session 保留，用户删除附件时删除；Gemini Files 临时引用随 session 清理并惰性刷新。

支持 MP4/MOV、WebM；音频 MP3、WAV、M4A、OGG、FLAC、AAC、AIFF、音频 WebM。
不支持任意 MKV 和播放列表文件。文件扩展名/MIME 声明不作为后端校验依据。

## 运行依赖

音视频处理需要 `ffprobe`；`local` 下载后端另需 `yt-dlp` 和 `ffmpeg`。
在 Ubuntu 上可用系统包安装 ffmpeg（包含 ffprobe），Python 依赖见项目依赖清单。

```bash
# 使用运行 Tentacle 的 Python 环境。
.venv/bin/python -m pip install -r requirements-media.txt
# Ubuntu 系统包提供 ffprobe 和 ffmpeg。
sudo apt-get install ffmpeg
```

在实际使用的 Gemini 模型条目开启能力：

```yaml
audio: true
video: true
```

协议须为 `interactions` 或 `gemini_generate`，且配置 GCS（ADC）或 Gemini Files（API Key）
上传策略。配置页提供上述两个开关；默认 false，避免给其他模型发送不支持的输入。
为 Gemini 模型开启能力开关不会修改认证或其他模型配置。

自动下载缺省开启，也可在配置文件中禁用：

```yaml
media_inputs:
  auto_bilibili: false
```

`/api/files/upload` 沿用 multipart `file`，响应为原文件记录（含 mime）。
`/api/chat/send` 可传 `files: [local_id, ...]`；旧 `images` 字段继续兼容。
归档沿用 `raw_messages.images` 列保存所有附件的本地引用，类型以 mime 为准，避免数据库迁移；
正文使用 `[图片×N]` / `[音频×N]` / `[视频×N]` 标记。模型上下文使用独立 `audio` / `video`
块（local_id、mime、可选 file_id），不把远端厂商 ID 作为归档主键。
历史切换到其他接口时保留显式不支持占位；新音视频输入会拒绝未启用能力的模型。
大媒体不做 Base64 兜底，远端引用失败会报错。

## 下载后端

`media_inputs.download.backend` 明确选择 `local` 或 `http_jobs`，缺省 `local` 保持本机下载兼容。
后端只负责把输入 URL 获取为固定临时文件；`attachments/media.py::_DOWNLOADERS` 使用统一异步上下文接口，
以后新增下载协议可独立增加适配器，不接管 AgentSession、模型或文件存储。
`http_jobs` 由 `attachments/downloads.py::HttpJobsDownloader` 实现（协议版本 `1`），用于把 B 站视频拉取委托给独立的远程 HTTP worker 服务，源码不绑定具体服务地址。

```yaml
media_inputs:
  auto_bilibili: true
  download:
    backend: http_jobs
    base_url: "https://worker.example/api/v1/media"  # 部署时填写实际可达地址
    profile: bilibili-480p-v1
    timeout_seconds: 360
    proxy: ""                  # 明确直连，不继承进程代理
    api_key_env: ""            # 可选：Bearer 令牌环境变量名（无鉴权可留空）
```

配置页的「媒体输入」分组、离线配置 CLI 均可编辑这些字段；保存后重启生效。
切换到 `http_jobs` 时应在同一次预览/保存中填写后端与地址。地址不能包含认证、查询参数或片段，
令牌仅通过环境变量引用。服务地址、profile、代理等由部署环境独立维护，不整体复制仓库配置。
远程模式下 Tentacle 主机只需已有 HTTP 客户端、`ffprobe` 和文件上传依赖，不需要安装 `yt-dlp`/`ffmpeg` 下载组件。

### `http_jobs` 协议 1 简要开发指南

如需自建兼容的远程下载 worker，只需在 `base_url` 下实现以下 5 个 HTTP 接口：

- `GET /health`：返回 JSON，至少包含 `{"protocol": 1, "ready": true, "busy": false}`。
- `POST /jobs`：接收 JSON 请求体并校验 `Idempotency-Key` 请求头，幂等创建下载任务并返回任务 ID；创建响应丢失时客户端最多以相同键和正文重发一次。
- `GET /jobs/{id}`：轮询任务状态，就绪后返回包含 `sha256`（64 位小写十六进制）等元数据的就绪结果。
- `GET /jobs/{id}/content`：按流返回合并后的视频文件，必须提供准确的 `Content-Length` 且 `Content-Encoding` 为 `identity`（不压缩传输）。
- `DELETE /jobs/{id}`：清理临时任务并返回 `204`。

Tentacle 客户端行为与限制：
- 原始 `b23.tv` 链接会完整交给 worker，Tentacle 仅做 URL 格式检查，不跟随短链或远端 HTTP 重定向。
- 控制请求连接预算 5 秒、总预算 10 秒；整个等待及领取上限 360 秒（可调低），清理另有至多 10 秒尽力预算。
- 内容请求连接 5 秒、读取空闲 10 秒、单次总预算 160 秒。固定 `video.part` 按块写入，校验 `Content-Length`、累计字节和 `SHA256`；最大 64 MiB、600 秒、480p，拒绝无长度、压缩编码、无效元数据或摘要不符的内容。只有网络中断才允许从头重取同一任务一次，不使用 `Range`、不重新创建任务。
- 全部校验通过后原子改为 `video.mp4`，调用 `FileManager.upload_file_path` 经 `ffprobe` 复验后上传；无论成功、失败、超时或取消都会尝试 `DELETE` 清理，服务不可达或失败时不会回退到本机下载。
- 若通过内网或 Tailscale Serve 暴露 worker，请确保服务运行身份下域名解析及 HTTPS 证书有效（不跳过证书验证）；worker 的下载依赖、Cookie 与缓存上限由服务端自行管理，Tentacle 主机不读取浏览器 Cookie。

接口依据：[Google 音频输入](https://ai.google.dev/gemini-api/docs/audio)、
[Google 视频输入](https://ai.google.dev/gemini-api/docs/video-understanding)、
[yt-dlp 官方参数](https://github.com/yt-dlp/yt-dlp#usage-and-options)。
