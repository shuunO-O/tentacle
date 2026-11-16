# 接入 Google Cloud Gemini

Tentacle 通过 Vertex AI 调用 Gemini，使用 Google Application Default Credentials（ADC）。在自托管 Linux 服务器上，最常见的接入方式是在 GCP 中创建服务账号并下载 JSON 密钥文件，通过环境变量指定密钥路径（密钥文件与环境变量仅保存在运行环境中，不写进 `config.yaml`）。

## 准备 Google Cloud 项目与服务账号

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 选择或创建项目，启用结算并开通 **Vertex AI API**（`aiplatform.googleapis.com`）。
2. 进入 **IAM 与管理 → 服务账号**，创建服务账号并授予 **Vertex AI User**（`roles/aiplatform.user`）权限；如果同时需要使用私有 GCS 存储媒体附件，额外授予对应 bucket 的 **Storage Object Admin**（或读写）权限。
3. 为该服务账号创建并下载 **JSON 密钥文件**，保存到应用可读的安全路径（例如 `/home/tentacle/config/gcp-sa.json`，权限设为 `0600`、属主为 `tentacle`，切勿放入代码仓库）。
4. 在 `.env`（或运行环境）中配置项目 ID 与凭据文件路径：

```dotenv
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
GOOGLE_APPLICATION_CREDENTIALS=/home/tentacle/config/gcp-sa.json
```

`GOOGLE_CLOUD_LOCATION` 默认使用 `global`。Tentacle 的 ADC 模式不读取 `GEMINI_API_KEY`。

> [!NOTE]
> 如果你直接将 Tentacle 部署在绑定了服务账号的 GCP 云主机（如 GCE / Cloud Run）上，或在本地开发机上使用 `gcloud auth application-default login` 登录开发凭据，可省略 `GOOGLE_APPLICATION_CREDENTIALS`，ADC 会自动读取云主机元数据或本机默认凭据。

## 在 Tentacle 里选择 Gemini

打开 Web 的“应用配置”，在“模型与角色”中新增 Google Vertex AI 连接，设置项目和 `global` 区域；新增 Gemini 模型，从可用目录选择模型和 API 协议，再把模型分配给 `chat` 角色。使用 `interactions` 或 `gemini_generate` 协议，按所选模型能力开启图片、音频或视频输入。保存后按页面提示重启或应用配置。

用 Web 配置可以保留完整的供应商、模型和角色关系。手工编辑配置时，`config_version`、`providers`、`models`、`roles` 必须作为一组完整设置，格式见[配置说明](CONFIGURATION.md)。

Google 当前将 Interactions API 标为 Preview。Tentacle 默认使用无状态请求并保留本地完整历史；启用 `interactions_stateful` 后才会把交互状态交给 Google 保存并续接。请先确认该 API 的预览条款和数据保留方式符合你的使用要求，再启用状态续接。

## 多个 Vertex 项目使用独立身份和存储

每个 `google_vertex` provider 可设置 `credentials_env` 和 `gcs_base_url`。
前者填写环境变量名，变量值为运行主机可读的 Google 凭据文件路径；后者填写此连接的私有 `gs://BUCKET/PREFIX`。模型调用、模型目录及此 bucket 的上传使用同一连接身份，不修改进程级 ADC。

```yaml
providers:
  vertex_secondary:
    type: google_vertex
    auth: adc
    project: YOUR_SECOND_PROJECT
    location: global
    credentials_env: VERTEX_SECONDARY_CREDENTIALS
    gcs_base_url: gs://YOUR_SECOND_BUCKET/tentacle-images
```

在主机环境或 `.env` 中设置 `VERTEX_SECONDARY_CREDENTIALS` 指向该项目的服务账号 JSON 密钥文件路径，不要把密钥内容写入 `config.yaml`。凭据路径环境变量变更后需要重启服务。

任意连接启用这些字段后，附件入口先保存本地副本，模型请求时按目标连接惰性上传。不同连接的远端引用分别缓存；已有历史图片有本地副本时会按需上传到目标 bucket，无需改写历史或批量迁移。仅剩旧云端引用且无本地副本时仍需要迁移或旧 bucket 访问权。显式凭据加载失败会报错，不回退到默认 ADC；独立凭据未配置 bucket 时，图片使用本地内联回退，音视频则需要配置可用的上传目标。

未填写两个字段的旧连接继续使用 ADC 和原有全局 GCS 设置。使用独立连接时不必替换现有默认凭据文件。

## 图片保存到私有 GCS（可选）

如果希望历史图片通过稳定的云端引用发送，准备一个私有 Cloud Storage bucket，并为应用服务身份授予所需对象读写权限。在用户配置的 `files` 段指定 bucket 路径：

```yaml
files:
  uploader: gcs
  base_url: gs://YOUR_BUCKET/tentacle-images
  api_key_env: ""
```

图片仍保留本地副本用于历史和惰性迁移。bucket 应保持私有，不要在配置里放凭据。

## 常见问题

- **找不到项目**：确认 `.env` 的 `GOOGLE_CLOUD_PROJECT` 与已启用 Vertex AI API 的项目一致。
- **权限错误**：确认进程实际使用的 ADC 身份有该项目的 Vertex AI 调用权限；管理员自己的 `gcloud` 登录状态不能证明服务身份有权限。
- **模型不可用**：确认模型、区域和协议支持组合正确，并且项目具有所需访问权限。

首次配置和身份问题可参考 [Vertex AI 文档](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/overview) 与 [Interactions 开发指南](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/interactions/developer-guide)。
