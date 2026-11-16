# 开发环境

业务源码按职责归组：`config/` 管理配置与模型接入，`agent/` 管理会话与协议，`attachments/` 管理文件和媒体，`runtime/` 管理宿主服务与工具装配。`run_web.py` 仍是唯一运行入口；`tools/` 保持自动发现目录。

程序目录保存源码、静态文件、默认配置和公开模板。运行时从显式设置的绝对路径 `TENTACLE_HOME` 读取和写入状态，开发和 Linux 部署使用同一布局。可在 Linux / WSL 设置 `export TENTACLE_HOME=/path/to/tentacle-home`（Windows 开发测试可设置 `$env:TENTACLE_HOME = 'C:\path\to\tentacle-home'`），在目录下创建 `config`、`workspace`、`prompt`、`skills` 及 `data`。CLI 与测试也使用独立数据根。Linux 发布与旧主机迁移见[部署指南](DEPLOYMENT.md)。

Tentacle 的运行与部署面向 Linux / WSL；开发与轻量单元测试可在 Linux / WSL 或本地 Python 3.13 `.venv` 中运行。不要复制运行中的生产环境或其凭据来搭建开发环境。

常用检查（Linux / WSL；Windows `.venv` 将 `.venv/bin/python` 替换为 `.\.venv\Scripts\python.exe`）：

```sh
.venv/bin/python -m pip check
.venv/bin/python -m ruff check .
.venv/bin/python scripts/export_openapi.py
npm --prefix frontend run typecheck
npm --prefix frontend run test
```

前端 React 19 工程位于 `frontend/`，构建产物输出至 `web/static/`（已由 `.gitignore` 忽略）。修改后端 Web Schema 时同步更新 OpenAPI 类型契约，Web 前端与鸿蒙客户端开发文档请分别参见 [Web 前端架构与开发验证](WEB_FRONTEND.md) 与 [鸿蒙客户端接入与插件说明](HARMONY_CLIENT.md)。

本机是开发环境，执行轻量、定向检查；完整 pytest 与独立候选验证使用 GitHub 托管 runner。服务器性能与资源限制在部署环境评估。

不要用 `run_web.py` 做启动验证：它会启动 Web、心跳、scheduler 及配置启用的外部适配器。优先使用以临时数据和 mock 服务为基础的单元测试。测试不得读取私人消息、`.env`、运行数据库或 prompt。

配置读取和用户覆盖规则见 [应用配置管理](CONFIGURATION.md)。架构行为从入口 `run_web.py::main`、会话编排 `agent/session.py::AgentSession.run_round` 和主循环 `agent/loop.py::AgentLoop.run` 开始查看。

常见定向检查（如鸿蒙端联动、回合协议、设备鉴权）：`.venv/bin/python -m pytest tests/test_phone.py tests/test_round_completion.py tests/test_web_device_auth.py -q`（Windows `.venv` 使用 `.\.venv\Scripts\python.exe`）。
