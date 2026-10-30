# 架构与代码阅读

当前数据库 schema v5。应用通过 `build_app(AppConfig)` 装配，业务在 `services/`，SQLite 访问在 `storage/`，HTTP/WebSocket、CLI、网页负责输入输出。LangGraph 仅保留调用适配，不承担持久任务调度。

## 建议阅读顺序

以下代码路径相对 `src/companion_ever/`：

| 顺序 | 文件 | 看什么 |
|---|---|---|
| 1 | `app.py`、`config.py` | 依赖装配与配置 |
| 2 | `storage/schema.sql`、`summary_schema.sql`、`task_schema.sql` | 数据模型与约束 |
| 3 | `services/chat_service.py`、`storage/generation_repo.py` | 接收、取消、保存与恢复回复 |
| 4 | `services/prompt_service.py`、`retrieval_service.py` | 模型实际读取哪些内容 |
| 5 | `services/background_service.py`、`session_close_service.py` | 自动触发与积压处理 |
| 6 | `services/extraction_service.py`、`memory_draft.py`、`memory_writer.py` | 抽取、严格校验与原子写入 |
| 7 | `services/summary_service.py`、`storage/summary_repo.py` | fact 操作如何演化两个摘要 |
| 8 | `services/task_runtime.py`、`storage/task_repo.py` | 执行名额、心跳、中断和逐次记录 |
| 9 | `llm/openai_client.py`、`traced_client.py`、`trace.py` | 模型通信与完整诊断 |
| 10 | `api/routes/`、`cli/`、`web/app.js` | 入口、推送、恢复与已读状态 |

提示词集中在 `prompts/`。回归入口为 `tools/check.py`；仍需讨论的问题见 [REVIEW_NOTES](../REVIEW_NOTES.md)。

## 三条业务链

**聊天：** HTTP/WebSocket → ChatService 接收用户消息和 generation → PromptService 组装设定、有效摘要、近期聊天和检索记忆 → 模型 → 逐条保存完整助手消息 → 页面推送或 HTTP 恢复。

**长期笔记：** BackgroundService → SessionCloseService → ExtractionService 读取固定范围和候选 → Draft 生成/校验 → MemoryWriter 同事务提交 note、证据、版本、关系及任务成功。

**派生摘要：** fact 写入触发操作流水 → 独立 summary_job → 上次摘要、操作差量及相关有效 fact → 模型整理/校验 → 两个摘要、引用、checkpoint 和任务成功同事务提交。

模型调用在事务外。两种后台任务共用一个模型执行名额，取得名额后才领取租约；聊天独立执行。摘要不反写 note，避免摘要自证循环。

## 数据职责

| 数据 | 职责 |
|---|---|
| `user`、`persona_definition` | 用户归属与角色基础设定；基础设定只由显式创建/编辑修改 |
| `conversation`、`conversation_log` | 私聊范围、迁移起点与原始消息 |
| `generation`、`thinking_log` | 一次回复生成及独立思考内容 |
| `note`、`note_evidence`、`note_revision` | 两层长期笔记、逐字原话证据及修改前版本 |
| `note_link` | supports、continues、supersedes 与依据 |
| `memory_job` | 记忆处理固定范围及持久状态 |
| `fact_change` | 事实增删改、归档、替代、证据变化的事务内流水 |
| `memory_block`、`summary_checkpoint`、`summary_job` | 用户画像/关系概况、来源、操作游标及独立任务 |
| `task_attempt`、`task_event` | 每次后台执行、阶段、心跳和结果；详细内容通过 trace ID 关联文件 |

`persona_runtime` 和笔记旧 `type` 已删除。短期场景直接使用近期聊天。`kind` 与 `reality` 是两个独立维度，具体规则见 [记忆规则](MEMORY_POLICY.md)。

## Review 时注意的边界

- API 校验 PIN，服务和写入器检查用户/角色归属；关系遍历、来源和重连结果都受范围限制。
- 新用户输入取消旧生成的未完成输出，已保存消息保留。断线本身不取消生成。
- 数据一致性依赖 SQLite 事务、SAVEPOINT 和执行 token；旧执行者不能提交。
- 页面已读状态属于本机：可见会话滚到最新才推进游标，后台消息保持未读。
- 迁移保留旧消息和笔记，不会自动重抽全部历史。迁移与故障恢复见 [后台任务](BACKGROUND_TASKS.md)。
