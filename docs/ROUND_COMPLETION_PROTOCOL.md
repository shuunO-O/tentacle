# 回合完成通知协议

供需要消费 Tentacle HTTP/WS 聊天接口的客户端参考。

## 完成是什么意思

原来的 WS `final` 继续保留，用来停止打字效果。它只表示模型停止输出，不能用于确认保存成功。
新增 WS `round_finished`：后端完成上下文、逐条归档、用户回退点等宿主处理后，才报告 completed。
模型失败、保存失败、命令异常报告 failed；取消报告 cancelled；没有执行的后台回合报告 skipped。
工具、文件等可能已经产生部分效果，failed/cancelled 不代表自动回滚，也不要自动重发。

命令也产生相同终态，仍不调用主模型、不进入聊天归档。`stop_reason=command`，结果在 command 字段。
未知命令、错误参数等原有反馈仍是命令文本；completed 表示处理器执行并返回，不把文本内容解析成成功/失败。
心跳让路、等待用户输入、迭代上限保留各自 stop_reason（yielded、waiting_input、iteration_limit）；
completed 表示宿主正常结束本次处理，不保证模型完成了用户目标。

## 请求和回合标识

POST `/api/chat/send` 可额外提交 `request_id`：1～128 字符，每次新发送生成一个客户端标识。
例如 `{"text":"你好","platform":"harmony","request_id":"client-request-123"}`。
`/api/chat/command` 兼容入口也接受此字段。识别仍由后端统一完成，客户端不维护命令逻辑。

后端在进入 run_round 时生成独立 round_id，复用它作为本轮归档已有 round_id，不改数据库结构。
命令虽然不归档，也会分配自己的 round_id。心跳、子任务结果的 request_id 通常为 null。
request_id 只用于关联，既不是鉴权凭证，也不是幂等键；不提供自动重试或请求去重。
进入回合前的鉴权、空输入、缺失附件、重启拒绝等校验错误可没有 round。

## 显式停止

POST `/api/chat/stop` 使用与聊天接口相同的鉴权。请求体至少提供 `round_id` 或 `request_id`，
两者都提供时必须同时匹配同一回合。优先使用事件携带的 round_id；尚未收到事件时可用发送时的 request_id。
request_id 若匹配多个排队或运行回合，返回 ambiguous，不会任意挑选一个停止。

例如 `{"request_id":"client-request-123","round_id":"server-round-abc"}`，接受后返回：

```json
{"accepted":true,"state":"stopping","round_id":"server-round-abc","request_id":"client-request-123"}
```

此回执只说明停止信号已设置，不是回合终态。重复请求不会创建新回合。
accepted=false 时，state 为 not_found（已结束或尚未登记）、ambiguous（标识不唯一）或 finishing（输出已结束，正在保存）。
迟到请求不会转而停止下一回合。没有提供标识返回 400。

停止会取消模型等待及流读取，阻止后续模型请求；正在执行的工具等待返回，余下工具标记为 not_executed。
终端命令、外部写入及已经启动的后台子任务不随主回合强制终止；不撤销已执行操作、不自动重发或重试。
排队回合可以直接取消，无模型调用和对话归档。媒体准备等前置处理期间的停止，在进入模型前生效。
停止当前回合不暂停心跳或 Scheduler 后续运行。

运行回合中断时，已展示的正文保留原文归档，最后一条可见回复附 `interrupted=true`；
没有输出时不伪造 assistant。另有归档的系统说明，中断后的模型上下文也包含已输出正文和宿主中断说明。
残缺工具参数和未完成的厂商续接状态不进入上下文。客户端应显示“已中断”，不把它当作完整回复。
被中断的请求若没有收到完整 usage，Token 统计可能不包含该次请求的实际消耗。

保存完成后通过原发送请求和 WS round_finished 返回相同的 cancelled 终态，stop_reason=cancelled。
原发送 HTTP 返回 200、ok=false，表示请求已停止；若保存失败则仍返回 500、failed。
客户端只有收到原回合终态后才解除发送状态；停止接口的回执不能用于提前发送下一条消息。

## Web 断线重连

网络断开本身不设置停止信号。Web 初次连接、刷新或重连后调用 GET `/api/chat/sync?limit=50`，
使用与聊天相同的鉴权。返回一份一致的展示快照：

- messages、has_more：与历史接口相同的归档消息。
- rounds：排队、准备、运行或保存中的回合，含 round_id、request_id、source、platform、phase、
  stopping、created_time 和可见 input。不会返回模型上下文、内部提示词或远端附件凭据。
- events：这些在途回合已经产生的展示事件，可用于恢复正文、思考、工具和投递状态。
  连续增量可能合并，已经归档的同一回合不应再次回放。
- finished：当前进程最近 128 个回合的终态，供本地未确认请求补收回执；不提供永久状态查询。
- instance_id、seq：本进程的展示流标识及快照边界。

带 round 的 WS 事件额外含 chat_instance、chat_seq；seq 在同一进程内递增，不是永久消息 ID。
请求快照期间先缓冲 WS 事件，恢复归档与在途展示后，仅处理相同实例且 chat_seq 大于快照 seq 的事件，
防止重复增量或漏接快照之后的输出。旧连接和过期的快照响应不能覆盖新连接状态。

Web 发送与停止共用一个按钮：空闲时发送，运行时停止，停止中或保存中暂不可重复操作。
重连后可按服务端 round_id 停止仍在运行的回合，包括来自其他客户端或心跳的主会话输出。
若断线期间回合已结束，则显示归档结果并恢复发送；重连不重发输入、不重跑工具。
服务器进程重启时，在途展示快照和短期终态缓存不保留，只能恢复已经保存的历史。
原请求不在当前回合或短期终态中时，只能报告结果未确认，不能据此认定失败或自动重试。

## HTTP 和 WebSocket

HTTP 成功响应继续含 ok、total_tokens，新增 round。命令保留顶层 command，旧命令接口保留 text。
模型错误或保存失败返回 HTTP 500、ok=false、detail 和相同 round；输入处理 ValueError 为 400。

```json
{
  "ok": true,
  "total_tokens": 120,
  "round": {
    "round_id": "server-round-abc",
    "request_id": "client-request-123",
    "source": "user",
    "platform": "harmony",
    "status": "completed",
    "stop_reason": "complete",
    "error": null
  }
}
```

相应 WS 事件为 `{"type":"round_finished","data":上述round对象,"round":关联信息}`。
其 data 与 HTTP 的 round 相同。命令终态额外含
`command: {"text":"命令反馈","refresh_history":true}`，即使 HTTP 回执迟到也能收尾。

本轮产生的 message_delta、message、thinking、tool_call、final、error 等原有事件名和 data 不变，
只增加顶层 `round: {round_id, request_id, source, platform}`。非回合事件如 hello、设置、独立终端输出可以没有它。
平台仍表示输入渠道；source 表示 user/heartbeat/subagent，不要由平台或消息文本猜测来源。
chat_seq 只用于同一进程内的展示同步，不改变增量内容或 round_finished 的终态语义。

## 鸿蒙接入要点

1. 发送前记录新的 request_id，与 HTTP 请求一起提交；将事件的 round 元数据及终态映射到类型定义。
2. 收到 final/error 只结束对应输出气泡。不能因任意 final 或心跳通知解除自己的 sending。
3. 仅在终态 request_id 等于当前请求时解除该请求发送状态。HTTP 和 WS 任一路先返回均可，重复到达只处理一次。
4. 命令反馈也只处理一次；保留现有历史刷新、防竞态、草稿、滚动与 REST 补救逻辑。
5. 迟到的旧 final、旧终态或 HTTP finally 不能关闭新回合的气泡、解除新请求的发送状态。
6. 无 WS 时用 HTTP round 收尾；网络错误且没有已收到的终态时，只能报告连接错误、结果未确认。
   这不证明服务端失败，不自动重发。HTTP 已失败后到达的旧事件也不能影响新的请求。

只用合成资料验收：final 后延迟保存、保存失败、模型错误、慢命令、命令/心跳交错、WS 缺失、
HTTP/WS 重复或迟到回执，以及两个客户端同时发送。不要读取真实历史或启动真实模型对话。
