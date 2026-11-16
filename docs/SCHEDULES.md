# 定时任务与预约心跳

## 模型接口

`schedule_update` 保留 create_schedule、update_schedule、run_schedule 三种操作。
批量写入失败会整体回滚，顶层返回 error，前面的步骤改为 rolled_back，不返回失效 ID。
创建计数、修改校验、手动执行登记放在同一 BEGIN IMMEDIATE 写事务，活跃上限为 50。

默认 `schedule_list` 只返回活跃任务；指定 schedule_id 可查看活跃或归档任务。
run_limit 默认 0，按需返回最近 1～10 次运行，存储最多保留 50 次。
详情包含状态、最近错误、run_id、scheduled_for 和运行摘要，结果未知与正在运行也能查看。

归档仍不可逆，不物理删除；不提供暂停/恢复。

## 三种动作

### tool：固定工具调用

```json
{"type":"tool","name":"send_message","args":{"platform":"qq","text":"该喝水了"}}
```

工具名和参数在创建时确定。普通动作按序执行，结构化顶层 error 或 status=error/failed 会停止后续动作。
批量 goal/task 更新失败也会返回顶层 error，不再被误记为运行成功。
参数未声明的业务文字、嵌套业务数据里的 error 不作为通用失败信号。

不允许直接调用 schedule_update、subagent_create、subagent_followup，防止后台自复制或委派。
也不允许 terminal_run、terminal_interact 直接进入共享终端或主会话相关状态：
命令使用独立 command，需要主 Agent 判断处理则使用 heartbeat。
这不保证所有其他工具内部都不调用模型；它们仍按自身实现执行。

### command：独立命令

```json
{"type":"command","command":"python scripts/synthetic_job.py","timeout":60}
```

不使用主 Agent 的 PTY，工作目录和环境不在持久 shell 中延续。
生产 Linux 创建独立进程组，超时或取消会终止进程组并 wait 回收；输出持续排空，只保留有限摘要。
Windows 开发环境使用 taskkill /T /F 清理进程树。

普通动作默认执行预算 max_runtime=600 秒，不包括排队和数据库开销；失败通知另有最多 10 秒预算。
普通任务同时执行最多 2 个。数据库连接的锁等待有界，连接离开作用域时关闭。

### heartbeat：带事项唤醒主 Agent

```json
{
  "op":"create_schedule",
  "name":"明天检查调查进展",
  "trigger":{"type":"once","expr":"2030-01-01T09:00:00+08:00"},
  "actions":[{"type":"heartbeat","content":"检查调查是否有新进展，决定是否需要通过 QQ 通知。"}]
}
```

- 必须是唯一动作，只允许 once 或 cron；创建、修改组合及执行前均会校验。
- 不接受 max_runtime，沿用已有心跳的模型请求预算、重试和用户优先让路机制。
- 经 `Heartbeat.run_scheduled` 复用心跳的目标提醒及唯一 AgentSession.run_round。
- source 始终是 heartbeat，不被识别为用户命令；request_id 为 schedule-run-ID。
- 不调用 reset_timer，不修改固定心跳的下一次触发时间；用户输入仍可正常重置固定心跳。
- 用户正在交互时等待；心跳让路后预约保留，随后接续完整历史重试本次唤醒。
- 待执行期间任务被归档或修改，旧预约取消，新的计划由调度器重新处理。
- 多种心跳共用一把执行锁；不会同时生成 note 或更新完整目标回顾游标。
- waiting_input 记录为等待输入；模型错误或达到迭代上限记录失败，不自动当成完成用户目标。

禁用 interval 是动作规则，不代表频率硬上限。cron 可按分钟表达，多个 once 仍可临近触发；
不同事项各自保留执行记录；固定心跳间隔由应用配置决定。

## 执行与恢复

手动、轮询、补跑都共用事务内执行准入。先在 schedule_runs 中登记 running，获得 run_id 后才执行动作。
手动工具回执 accepted 只表示已经接收；忙时返回 busy，不再虚报“已触发”。

同任务不能并行执行，普通任务另有全局并发限制。启动补跑只规划进度，普通轮询公平派发，
不会在启动阶段顺序等待某个任务执行 20 次。

- once：宽限期默认 24 小时，超期归档；已接收的自动执行结束后归档，包括失败。
- interval：补最近 6 小时内错过的点，每任务最多 20 个；每完成一个点才推进持久游标。
  中途重启可继续剩余点。正常周期仍从本次完成后计算下一次。
- cron：停机错过的点不补跑，从当前时间计算下一次。

执行记录成功保存后才解除持久占用。写结果失败仍留下 running，不会被下一次轮询重新执行。
重启将残留 running 标记 unknown，并停止该任务的自动触发。执行中取消同样保守地标记 unknown；
执行前取消不视为未知效果。

先查询记录，确认可能存在部分效果后，才能显式重试：

```json
{"operations":[{"op":"run_schedule","schedule_id":12,"acknowledge_unknown":true}]}
```

该标志确认“可能重复”，不是撤销先前效果，也不保证外部平台绝对只发送一次。
重试成功后恢复计划；对已到期的原 once 执行明确重试后归档。
如果当前进程持续无法写入结果，残留 running 会保持占用；修复存储并重启后按 unknown 恢复。
