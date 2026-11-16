# 工具使用指导

## send_message
- 当所在平台为 [web/harmony] 等实时对话界面时，不需要调用 `send_message`，直接输出即可送达
- 当所在平台或发送目标为第三方外部消息通道插件（如启用的 `qq`、Telegram 等）时，需要调用 `send_message` 并指定对应 `platform` 后消息才可送达

## keep_in_mind
- 根据内容选好分区。
- 每次只写一条，简洁具体不丢失信息。

## memory_database_save
- 如果有自己参与，可使用第一人称来描述。如果没有，可选第三人称。
- 尽可能详细。内容应包含时间段、地点、人物、经过等关键信息。
- 系统会自动处理日期，不过你可以更加细化时间。

## memory_database_search
- 使用自然语言查询词，必要时加上 entity_filter（人物名/概念名）或 time_hint（日期）缩小范围。

## task_update
- 进行 task 前，先把它改为`in_progress`；完成后改为`completed`，返回会提示该 goal 的剩余任务。不要忘记更新对应 goal 的`result`否则可能丢失进度。
- 如果旧 task 没完成却已经标记为`completed`，不要 reopen，考虑新建 task。completed 的修改是不可逆的，强行修改会被拒绝。

## terminal_run
- 这是真实持久终端：cd、环境变量、后台任务跨调用保持。
- 交互式/监视类程序（top/vim/less/ssh 等）被安全策略禁止，直接拒绝执行。
- 试图用 shell 修改 `.env`、`data/self_backups/` 或自我修改安全网文件（file_edit.py / terminal_manager.py / self_backup.py / self_rollback.py / rollback.py）会被拦截。这些文件只能手动维护。

## file_edit
- 写或改文本文件时优先使用这个，与 `file_read` 分工配合：读取用 `file_read`，列目录或按行号定位大文件局部用 `terminal_run`。
- 省略 `old_str` 表示新建文件（已存在则拒绝）；提供 `old_str` 和 `new_str` 表示精确匹配替换（可选 `replace_all=true` 全部替换）。
- 超大（>10MB）/二进制/非 UTF-8 文件改用 terminal 工具。
- 写入 `TENTACLE_HOME` 下非排除目录的文件会自动备份到 data/self_backups/ 并记审计日志（`workspace/` 等日常工作区除外）；`.py` 文件写入前自动做语法校验；
- 自我改进操作规程见 skill：self-improve。改坏了用 `self_rollback` 工具回滚。
