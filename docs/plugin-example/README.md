# Tentacle 插件模板说明

将本目录整体复制到 `$TENTACLE_HOME/plugins/example-helper/`（目录名必须与 `manifest.yaml` 的 `id` 一致），或通过主 Agent 工具 `plugin_install_local` 暂存后，前往 WebUI `/plugins` 页面审核权限并启用重启。

- `manifest.yaml`: 声明 ID、入口函数、最小权限集（`permissions`）、环境变量名（`requires_env`）与配置表单（`config_schema`）。
- `plugin.py`: 实现 `setup(ctx)`（支持 `async def`）与可选 `teardown(ctx)`。
- `prompts/guild.md`: 若申请了 `prompts:append`，会自动注入主提示词 `<guild>` 区块。
- `skills/`: 若申请了 `skills:provide`，会自动暴露到 `<available_skills>` 与 `skills_list` / `skill_view`。
