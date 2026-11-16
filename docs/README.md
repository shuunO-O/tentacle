# 文档

这里只索引适合随项目维护的用户指南和开发参考。代码与配置校验是运行行为的最终依据；本索引不代替源码。

## 用户指南

- [Linux 部署与更新](DEPLOYMENT.md)
- [配置项说明](CONFIG_GUIDE.md) · [应用配置管理](CONFIGURATION.md)
- [内置插件与插件管理](BUNDLED_PLUGINS.md)
- [接入 Google Cloud Gemini](GEMINI_SETUP.md)
- [接入 QQ 机器人](QQBOT_SETUP.md)
- [音视频输入](MEDIA_INPUTS.md) · [文件输入](FILE_INPUTS.md) · [文件发送](FILE_OUTPUTS.md)
- [定时任务](SCHEDULES.md)
- [公开网页搜索与提取](WEB_TOOLS.md)
- [记忆写入前概念对齐](CONCEPT_ALIGNMENT.md) · [历史概念去重](MEMORY_CONCEPT_DEDUP.md)
- [记忆检索与原文证据](MEMORY_RETRIEVAL.md)
- [远程 Chrome（可选）](REMOTE_BROWSER.md)

## 开发参考

- [开发环境](DEVELOPMENT.md) · [Web 前端架构与开发验证](WEB_FRONTEND.md)
- [鸿蒙客户端接入与插件说明](HARMONY_CLIENT.md)
- [工具注册与扩展](TOOLS.md)
- [插件与多端扩展开发参考](PLUGINS_DEV.md)（示例：[plugin-example](plugin-example/README.md)）
- [子 agent](SUBAGENTS.md)
- [模型切换与会话兼容](SESSION_HANDOFF.md) · [回合完成协议](ROUND_COMPLETION_PROTOCOL.md)

版本默认值位于仓库根目录的 `config.defaults.yaml`。运行状态位于独立的 `TENTACLE_HOME`。
日常设置从 Web 的“应用配置”页面修改；也可手动编辑`TENTACLE_HOME/config/config.yaml`。
