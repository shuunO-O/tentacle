# Concept 写入前对齐

新记忆写入时，可选用 Concept 对齐检查实体是否适合共享记忆检索入口。

已有概念分组另有 Web 手动启动的独立功能，见 `docs/MEMORY_CONCEPT_DEDUP.md`；不随写入开关自动执行。

入口仍是 `memory_database_save`，不增加主 agent 工具。流程为：

```text
主 agent 原始记忆摘要 → MemoryExtractor
  → 生成事件摘要与实体名称向量（复用同一次 embedding 批次）
  → Concept 名称/别名精确候选 + 向量 Top-K 候选
  → Jev 对每个候选对做三档 Score 判断
  → 原始提及保留独立节点 → 加入等价概念组
  → 一个 Ladybug 事务写入实体、事件、关系、对齐记录
```

对应代码：`memory/concept_aligner.py::ConceptAligner.align`、
`memory/ladybug_store.py::LadybugStore._recall_concepts`、`LadybugStore._save_memory_tx`。

## 启用

功能默认关闭。要启用时，在 `.env` 设置 `TYPESAFE_API_KEY`，再在 `config.yaml` 中将
`memory.concept_alignment.enabled` 改为 `true`，也可在 Web「配置 → 运行与工具 → 记忆去重」开启。
密钥不能写入配置文件或提交到仓库；配置需重启后生效。

```yaml
memory:
  concept_alignment:
    enabled: true
    model_role: concept_alignment
    top_k: 5
    same_probability: 0.95
    different_probability: 0.90
    timeout_seconds: 15
    total_timeout_seconds: 60
    max_concepts: 32
```

使用现有 httpx，直接调用 [TypeSafe HTTP API](https://docs.typesafe.ai/api) 的
`POST /v1/systemone`，不新增依赖、不经过 AgentLoop 或普通聊天方言。
模型版本固定，实际响应版本也写入记录；可在配置中调整。
在 Web「模型与角色」添加 System One 模型并绑定 `concept_alignment` 后，即可启用概念对齐。供应商、模型名、密钥变量和代理均在此处管理。
用量经 UsageTracker 保存到 `memory_alignment` 平台。当前无自动重试，HTTP/答案异常直接降级。

也支持 [OpenRouter System One 接口](https://openrouter.ai/docs/guides/community/typesafe-sdk)：
在模型与角色中选择 OpenRouter 连接（`https://openrouter.ai/api/v1`、`OPENROUTER_API_KEY`），
模型名填写 `typesafe/jev-1.13`，协议选择 `systemone`。这里的 HTTP 客户端仅追加 `/systemone`，
不同于官方 SDK 的 base URL 规则。使用 OpenRouter Key 时不能指向 TypeSafe 直连接口。
实测响应的 score 和概率分别保留两位小数，结构校验允许相应舍入误差；对齐仍按原始概率
和既定门槛判断，不因为舍入放宽 0.95/0.90 判定条件。

## 判定与退化

- 三档含义：应保持独立检索入口、归并证据不足、适合共享记忆检索入口。
  目标是通过任一名称找回同一组连贯记忆，同时不混淆仍有独立查询价值的对象。
  大小写、缩写、翻译可作为同一对象的名称变体；项目主题与具体文档/工具载体可以归并，
  前提是语境支持稳定的同一记忆主题且不丢失有用区分。语境不同或粒度不同不再一律否决；
  仅共同出现、同属大类不足以归并，也不把独立产品、人物、版本或主题随意混在一起。
- 名称/别名精确候选最多 `top_k` 个，与向量 `top_k` 个取并集，按等价组去重。
  精确候选超过上限、缺 ID 或 ID 重复会记录召回异常，不能截掉冲突候选后自动对齐。
  无相似度硬门槛；相似度不参与最终分组判断。
- Jev 每次比较一个新提及与其全部候选，一次 HTTP 请求包含多个 Score 问题。
  新提及带原始记忆摘要前 6000 字符和事件短摘要前 500 字符；已有候选带名称、
  前 20 个别名、首次短摘要与最近 3 条 `MENTIONS` 事件摘要（每条最多 300 字符）。
  不传会话历史、图片或整个邻域。原始摘要全文仍按原流程保存在事件 content。
- 仅一个候选“适合归并”概率达到 0.95，且其他候选“应独立”概率均达到 0.90，才自动加入该组。
  所有候选明确不同则新建；其余情况新建并标记 pending。多个相同候选不按最高分强选。
  该概率门槛为保守初始阈值，尚未通过真实中文记忆样例校准，模型概率不是正确率保证。
- 本次提取中先新建的 Concept 也参与后续候选判断。已加入组的提及不另加为候选，
  避免经新别名传递对齐；后续提及重新与该组的原始证据比较。本批候选只带首次摘要前
  300 字符，不重复填充事件摘要，以控制请求大小。
- 每个新概念保留原始名称和 ID，等价提及通过 `equivalence_group` 共享入口。
  关系始终连接该次提及自己的节点，不迁移到参考节点。同组的精确名称提及可复用自己的节点；
  判定不同或不确定的同名提及仍创建独立节点。关闭对齐时，名称/别名歧义会报错。
- Person 和 Location 仍按名称复用；跨类型同名和保留名 CURRENT_EVENT 会拒绝写入。
- 保存锁覆盖召回、Jev 与事务提交。事件、实体、关系、原文分块及对齐记录一起提交；
  取消或失败不会留下半条记忆，也不会自动重发。
- 候选上下文合并同组成员的名称与事件证据。Concept 定向检索、实体过滤、共享概念计数和
  expand 均按组汇集相关记忆；原文、摘要和日期过滤的含义不变。

对齐判断和提交结果仍写入现有 SQLite 操作审计，图中的历史分组操作另存 `ConceptGroupAudit`。
图事务提交后的审计确认失败不会诱导再次保存。图谱保留所有原始节点及关系，
Concept 展示 ID 为 `Concept:<数据库ID>`，另带 `equivalence_group`。
本次不提供旧数据迁移、数据库回退或备份恢复。

## 写入、追溯和边界

新事件的 `concept_alignment` 属性保存 JSON 数组，包含原始提及、最终 ID、create/join、
状态与原因、候选 ID/名称/分数/概率、模型版本、规则版本和门槛。
同组提及保留自己的名称与向量，通过组归属汇集检索，不追加别名或删除节点。
新节点保存 `first_context`（首次事件短摘要），用于以后的消歧，不将事件摘要当作概念定义。

`memory_database_save` 成功响应仅返回 `status: ok` 和 `event_id`，不再向主模型返回
`concept_alignment` 明细。不论新建、加入组或 pending，提交成功均用同一回执；
详细判定、候选和本地错误码继续留在本次事件属性中。存储层不依赖调用者请求返回报告来保存审计。
不把正文、密钥或供应商错误正文打印到对齐日志。

`ConceptAuditLog` 在 chronicle.db 同目录的 `concept_dedup.db` 中新增 `concept_audit` 表。
真实 Jev 客户端在请求前记录语境、候选、规则、模型和门槛，完成后记录校验过的概率及结果；
错误/取消保留安全错误类型和本地错误码，不保存密钥、认证头或供应商错误正文。
每次写入的 create/join/pending 决策和图提交结果另有自动审计，关联事件 ID 与评价 ID。
先记录开始再执行副作用；未确认结果不自动重试。图已提交但 SQLite 审计完成状态写入失败时，
保存仍返回成功，留下未完成审计供核对，避免诱导重复保存。
这些记录也在概念去重页的「操作审计」中展示，自动与人工参与分开筛选。

关系保留原始名称引用到事务内，再统一映射为实际节点 ID。复用已有节点时使用其实际 ID，
不能用本次预分配但未使用的 ID。目标丢失/ID 不唯一、未知关系类型或端点缺失会使事务失败，
不返回“事件保存成功但关系漏写”。跨 Person/Location/Concept 同名及使用 CURRENT_EVENT
作为实体名的提取结果会被拒绝，因为现有提取格式无法区分其关系端点。
相同提及在一个事件中仍只表示一个实体；需要描述两种同名含义时，提取器应使用不同名称。

同进程写入锁覆盖召回、判断与提交，避免并发写入互相看不到本批新节点。
精确余弦排序仍只选择有限候选，候选之外的等价概念无法由本次 Jev 判断覆盖。
写入对齐不承诺全库无重复，也不自动启动历史扫描或重试 pending。
图谱 API 的 Concept 展示 ID 改为 `Concept:<数据库ID>`，
避免同名异义节点在图中重叠为一个身份；名称展示不变。实体过滤支持别名。

对齐记录用于定位错误，不是自动回滚功能。现有 `HAS_ATTITUDE` 仍采用原本的关系更新语义，
对齐可能更新已有边的属性，撤销需要结合事件证据处理；本版不重写这套记忆关系设计。
关闭对齐后若已存在多个同名节点，旧名称复用会明确报错，避免关系连接到多个节点。
