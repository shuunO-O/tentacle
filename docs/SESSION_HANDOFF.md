# 模型切换与会话交接

切换发生在配置实际生效时。`ConfigurationService._apply` 等待当前回合完整结束，
持有 AgentSession 锁，构建新客户端后调用 `SessionManager.apply_model_config`。
配置预览、保存但等待重启的字段不会提前改写历史。

## 兼容规则

active_session.json 记录一个会话级 `history_model`，不追溯单条消息来源。
连接标识是连接 ID、供应商行为、端点、认证、凭据、Cloud 项目和区域的摘要；
缓存不包含明文密钥或认证 URL。模型名和协议另外记录。

- 身份不变：不重建。温度、思考强度、工具列表等参数变化不会清空历史。
- 同一 Google 连接、Interactions 协议、两个 gemini-* 模型：保留完整历史，移除
  gemini_interaction 续接指针，重置上下文 token 估计。
- 同连接、同模型 Interactions → generateContent：优先导入正文、附件及闭合的普通函数
  调用/结果；丢弃旧 thought 和续接状态，在导入批次的首个 functionCall 上使用官方
  `skip_thought_signature_validator` 迁移占位。它不保留加密推理状态。内建搜索、未知
  step、多模态工具结果、缺失/不匹配结果等情况整份历史回退为主要对话。
- 其他模型、连接或协议变化（包括 generateContent → Interactions）：重建主要对话。
- 旧缓存没有有效标识：首次启动保守重建一次，保存标识后不再重复。

Interactions 例外依据 Google Thinking 文档：同协议内切模型仍须回传原 thought blocks，
兼容由后端处理；这不代表两种 Gemini 协议可以直接互换：
[Google Thinking 文档](https://ai.google.dev/gemini-api/docs/thinking)

generateContent 导入占位依据官方 FAQ 的历史迁移用法，仅用于导入的旧函数轨迹；
切换后的新回复继续保留真实签名，不添加迁移占位。没有跨协议密文兼容的承诺：
[思考签名 FAQ](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures?hl=zh-cn)。

## 重建与持久化

保留 user 消息的正文和附件、没有 tool_calls 的 assistant 正文，以及 send_message
具有 status=sent 且无顶层 error 的结果所确认的发送正文。移除工具调用、工具结果、
工具执行阶段的助手文字和推理/签名/原生响应字段；同一 user 回合内发送正文与最终回复
相同则只保留一次，不合并跨 user 回合的重复对话。不调用模型摘要，不读取归档补全。

DeepSeek 接收一条有历史角色标签和明确边界的 user 导入记录，附件按原顺序插入。
旧助手没有 DeepSeek 的 reasoning_content，因此不把导入内容伪装成它的原生输出。
交接后的新回合仍正常使用工具、保存并回传原生推理状态。

先将完整旧缓存写入同目录 session_handoffs/UUID.json，再原子替换 active_session.json。
保存失败则恢复内存历史和兼容标识，运行配置、客户端和心跳间隔保持原值。
配置文件已保存的目标仍显示待应用，可重试。中断后启动依据兼容标识重新核对。
同一 session ID 和已发送的 session 提醒状态保留；消息归档不重写，也不重发消息。
快照含旧会话内容，属于本地私人数据，不要分享或提交到仓库。

## 图片与协议状态

用户附件保留 local_id、MIME 等本地引用；旧 file_id 仅作为数据库反查线索。
每次请求按目标模型选择 GCS、Gemini Files、DeepSeek Files 或现有的 base64 通道。
DeepSeek/Gemini Files 使用目标模型连接的地址、认证和代理，避免使用全局默认上传账号。
GCS 继续使用显式配置的独立存储和 ADC，不随模型切换改写桶配置。
图片来源不可读取时发送明确的「图片不可用」占位；旧 ID 跨厂商碰撞且无法唯一反查时
也按不可用处理，避免错配本地原件。

远端引用按 local_id、上传器、端点、连接作用域缓存；作用域包含认证身份摘要。
旧引用迁移为归属未知，不作为新连接的有效缓存。来源不明的旧引用不会用当前账号
尝试删除，临时远端对象等待过期；本地原件保留。切换本身不会批量上传历史附件。

Responses 原始 output items（包括加密 reasoning）保存到 responses_output 并按原顺序
回传；OpenAI 连接显式使用 store=false 和 reasoning.encrypted_content。
Chat 请求只发送该协议允许的消息字段，reasoning_content 仅给 DeepSeek。

## 验证

定向用例使用临时 SQLite、合成历史和假上传器，覆盖重建、兼容例外、重启、保存失败、
确认发送、附件及同端点不同账号隔离。完整回归遵循仓库约定交给 GitHub 托管 runner；
未启动应用、访问私人对话或调用真实模型。
