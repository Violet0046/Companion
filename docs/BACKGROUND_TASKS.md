# 运行、自动任务与恢复

当前 schema v5。Web 服务启动后每 15 秒扫描，先恢复持久任务，再创建到期工作。手机页面关闭或切后台不负责维持任务；Python 服务仍须运行。

## 自动触发

| 工作 | 默认触发 | 新批次预算 |
|---|---|---|
| note 整理 | 累积 6 条用户新消息，或最后消息后约 3 分钟；回复生成中暂缓 | 12 条 user/assistant 消息、约 4000 字来源 |
| 用户画像/关系概况 | 首次自动建立基线；后续 5 条不同 fact 变化，或操作等待约 15 分钟 | 12 条操作、约 4000 字变更 |

删除、归档、修改和替代使来源失效时优先更新摘要。没有新内容不调用模型；成功后继续处理已积压批次。摘要等待同角色 queued/running 的记忆任务完成，两类任务共享一个后台模型执行名额，聊天独立执行。

note 整理另取最多 12 条/约 4000 字候选及最近 6 条/约 2000 字参考前文。单条超长来源完整保留，旧任务保留原固定范围；不截断证据。摘要完整输入超过 60000 字符会明确失败并保留进度。

正常页面隐藏记录、摘要维护按钮和任务错误；`?debug` 保留触发、重试、来源及后台执行记录。自动完成静默更新页面缓存。角色基础设定由显式创建/编辑修改，不受自动摘要影响。

## 聊天消息与后台回复

接收输入时，用户消息和 queued generation 同事务保存。client_message_id 在同一聊天内防重；相同 ID 内容不一致会拒绝。模型默认可用 `<message_break>` 输出 1～3 条回复，完整消息逐条保存，草稿不参与记忆。

新输入逻辑取消该聊天旧生成的未完成输出，已经保存的消息保留。断线只停止推送，服务器继续生成。前端通过 HTTP 确认 outbox、恢复 generation/历史，不依赖 WebSocket ready；过滤旧响应并按正式 message ID 去重。回到前台恢复完整消息，不恢复中断的逐字草稿。

queued 生成在启动/扫描时恢复；过期 running 生成保留失败，避免擅自重复可能已交付的回复。没有部分正式输出的最后一条失败回复可沿用原用户消息重试。取消不能保证服务商立刻停止计费。

已读游标只前进：当前可见会话滚到最新才标为已读，后台/其他页面保持未读；旧计数响应不能复活红点。当前没有跨设备已读同步。

## 记忆与摘要的执行

任务范围分别为 `(after_message_id, through_message_id]`、`(after_change_id, through_change_id]`。每个聊天/角色最多一个未成功的对应任务。取得执行名额后才领取租约，默认 300 秒、每 30 秒独立连接续期。

| 故障 | 处理 |
|---|---|
| 续租连接/更新暂时失败 | 保存诊断，继续尝试；续租连接等 SQLite 锁约 1 秒 |
| 进程退出、租约丢失、正常关闭 | 原范围重新排队；新 token 隔离旧结果，不消耗真实模型失败预算 |
| 网络、超时、限流、服务端故障 | 单轮最多三次，间隔 30/60 秒；自动模式耗尽后冷却约 15 分钟 |
| JSON、引用、关系或容量不合法 | 同一总预算内最多定向纠错一次；仍失败则暂停，不跳过或写入半份结果 |
| 认证、配置等永久错误 | 暂停，修正后通过调试入口重试 |

模型流式接收，单次读等待默认 120 秒，生成及纠错总预算约 360 秒，SDK 不叠加重试。MiniMax-M3 后台首次关闭长思考，唯一一次纠错用 adaptive；其他模型不注入不支持的参数，普通聊天保留原默认。设置见 .env.example。

`task_attempt` 保存逐次执行、固定范围、心跳和分类；`task_event` 保存阶段和 trace ID。attempt_count 只是当前快速重试预算，完整次数看 sequence。写入、进度及成功记录同事务提交。完整请求/原始回复不放状态接口，保存在 [trace 文件](TRACING.md)。

## 配置与接口

[.env.example](../.env.example) 列出全部可选项：`COMPANION_AUTO_MEMORY`、`COMPANION_MEMORY_TRIGGER_MESSAGES`、`COMPANION_MEMORY_IDLE_SECONDS`、`COMPANION_SUMMARY_TRIGGER_FACTS`、`COMPANION_SUMMARY_INTERVAL_SECONDS`、`COMPANION_TASK_RETRY_COOLDOWN_SECONDS`、`COMPANION_BACKGROUND_THINKING`、`COMPANION_TRACE`。

`COMPANION_AUTO_MEMORY=0` 关闭新自动触发，已持久化任务仍恢复。旧 `COMPANION_IDLE_MINUTES` 只表示逻辑会话边界，marker 不代表记忆处理成功。

HTTP 定义直接看 `api/routes/chat.py`、`memory.py` 或服务的 `/docs`。主要诊断接口位于 `/api/persona/{user}/{persona}/` 下：memory-jobs、summary-jobs、summary-status、fact-changes、task-attempts；均检查 PIN 与角色归属。`/api/health` 返回 schema_version、auto_memory 和 memory_idle_seconds。

## 升级与备份

新数据库直接建立 v5；旧库按顺序迁移。消息/笔记 ID、原文、证据、设定和历史摘要保留，旧 type 只在迁移映射中存在。旧 persona_runtime 删除，另外两个摘要保留供查看；新摘要独立追平后进入 prompt。

旧 fact 建立一次 bootstrap 快照，不伪造历史操作；旧笔记 reality 为 uncertain。历史消息不自动全部重抽，conversation.memory_start_message_id 是兼容起点，不表示历史已整理成功。

迁移先用 SQLite backup API 备份，再在事务中升级并检查约束。v5 对明确的旧“租约耗尽”失败保留 legacy_snapshot 后排队，其他永久失败不擅自重试。停止旧服务再启动新服务，避免不同版本同时写库；无需清库。

`companion-cli maintain backup` 只读打开现有数据库，用 SQLite backup API 包含 WAL 中的已提交数据，不初始化或迁移来源。只读迁移检查使用 `tools/verify_migration.py`；开发副本重放见 [验证与工具](MEMORY_EVALUATION.md)。

备份清理保留最近完整快照及最近升级前恢复点；当前库的 -wal/-shm 不作为旧备份删除。memory/reviews/ 中的测试数据库快照可以在排查结束后删除，报告与 trace 独立保留，后续需要时再生成新测试快照。
