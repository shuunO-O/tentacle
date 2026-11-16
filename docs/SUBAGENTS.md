# DSH 编程台

Tentacle 主 Agent 和浏览器共用一个 DeepSeek Harness Web Host。DSH 保存编程会话、模型设置和工具配置。Tentacle 保存任务状态和委派结果，主 Agent 只把任务所需的背景交给 DSH。

## Linux 配置

使用 Node.js 24.9 或以上版本全局安装官方 DSH CLI（无需再安装外部 `dsh-acp-server` 插件）：

~~~sh
npm install -g @deepseek-ai/dsh@0.2.2-rc.1
~~~

在 Tentacle 服务环境设置 `DEEPSEEK_API_KEY`，并在 `config.yaml` 启用：

~~~yaml
subagents:
  enabled: true
coding:
  dsh_binary: dsh
~~~

Tentacle 在第一次打开编程台或派发任务时启动 DSH，只监听本机的随机端口。浏览器从 Tentacle 登录后进入 `/coding/`，无需单独登录 DSH。DSH 读取 `$DSH_HOME/profiles/web`，默认的 `DSH_HOME` 是 `$TENTACLE_HOME/data/dsh`。可在编程台的设置里选择模型，模型密钥通过服务环境配置。`coding.dsh_home` 可指定独立状态目录，重启后生效。

Tentacle 后端复用同一 DSH Web Host 进程的原生 `/api` RPC 接口调用工作区与会话控制器（`workspace/create`、`session/*`），并通过启动补丁注入轻量会话桥，保留原生工作区、消息 ID 和网页审批。`/acp` 与 `/connect` 不对浏览器代理开放。

工作区是 `$TENTACLE_HOME/workspace` 的现有子目录。主 Agent 可传相对或绝对 `cwd`，浏览器可在原生界面选目录。DSH 直接使用已有文件和未提交改动。DSH 的 `workspace-write` 策略约束工具写入，磁盘读取仍按 DSH 和 Linux 账号的权限执行。后台任务使用同一 Host，权限提示可在编程台处理。

## 双向接力

- **主 Agent 先派活**。主 Agent 用 `subagent_create` 创建任务，用户在 Tentacle 的子代理面板打开 DSH。任务详情提供会话 ID，可在 DSH 侧栏按 ID 查找。用户可以直接在原会话继续发消息。
- **用户先开工**。用户在 DSH 新建并运行编程会话，再回 Tentacle 的子代理面板点「关联已有会话」。拿到任务 ID 后，在主对话说明要继续的工作。也可以在主对话提供 DSH 会话 ID，让主 Agent 调用 `subagent_attach`。
- **主 Agent 跟进**。用 `subagent_view` 并开启 `include_dsh_history` 查看近期编程对话，用 `subagent_followup` 在同一会话排入要求。历史查询不运行模型。

主 Agent 可以后台委派，也可以用 `wait=true` 等结果。等待超时后任务继续，后台结果由 Tentacle 交给主 Agent。DSH 一轮结束只表示本轮停止，验收仍需检查改动和测试。

`subagent_cancel` 和 `subagent_archive` 只取消 Tentacle 发起、且可归属到该任务的轮次。中止正在运行的轮次时，DSH 会保留用户已排队的消息，收到下一次输入后继续处理。归档作用于 Tentacle 的任务记录，DSH 会话仍可在编程台打开。

旧版只读任务仍可查看。早期迁移保留下来的会话 ID 会在继续前由 DSH Web Host 验证会话和工作区。停机中断的任务可以显式追问续接。
