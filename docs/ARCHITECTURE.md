# 按交互链路 Review

当前数据库 schema v5。应用通过 `build_app(AppConfig)` 装配，业务在 `services/`，SQLite 访问在 `storage/`，HTTP/WebSocket、CLI、网页负责输入输出。LangGraph 承担记忆整理的步骤与条件循环，SQLite 任务层负责持久状态、租约和恢复。

## 阅读方法与顺序

先看 `src/companion_ever/app.py` 的 build_app，了解服务如何连接；随后按一条用户操作从入口跟到结果，再检查失败分支。每经过一个函数，记录：输入是什么、读了什么、写了什么、何时算成功、失败后由谁恢复。以下路径默认相对 `src/companion_ever/`。

| 顺序 | 链路 | 完成这轮阅读时应能解释 |
|---|---|---|
| 1 | 聊天正常完成 | 用户输入何时落库，模型收到什么，回复何时成为正式消息 |
| 2 | 聊天断线、重发与插话 | 丢连接后谁继续执行，如何防重、恢复和拒绝旧结果 |
| 3 | 聊天记录整理为 note | 哪些原话进入本批，如何决定新增/确认/修改，如何原子写入 |
| 4 | fact 操作演化为摘要 | 增删改如何影响摘要，摘要何时可重新进入聊天 |
| 5 | 后台公共运行层 | 谁触发、如何续租，永久失败与可恢复故障如何区分 |
| 6 | 其他入口与迁移 | 角色编辑、固定记忆、历史查询及旧库兼容是否遵守同一范围约束 |

提示词集中在 `prompts/`。回归入口为 `tools/check.py`；仍需讨论的问题见 [REVIEW_NOTES](../REVIEW_NOTES.md)。

## 三条业务链

**聊天：** HTTP/WebSocket → ChatService 接收用户消息和 generation → PromptService 组装设定、上次成功摘要、近期聊天和检索记忆 → 模型 → 逐条保存完整助手消息 → 页面推送或 HTTP 恢复。

**长期笔记：** BackgroundService → SessionCloseService → ExtractionService 领取固定范围任务 → LangGraph 加载/预取/模型/工具/校验 → MemoryWriter 同事务提交 note、证据、版本、关系及任务成功。

**派生摘要：** fact 写入触发操作流水 → 独立 summary_job → 上次摘要、操作差量及相关有效 fact → 模型整理/校验 → 两个摘要、引用、checkpoint 和任务成功同事务提交。

模型调用在事务外。两种后台任务共用一个模型执行名额，取得名额后才领取租约；聊天独立执行。摘要不反写 note，避免摘要自证循环。

### 记忆图编排设计与当前实现

记忆整理已从单一 extract 节点拆为 load_input → prefetch → model → validate，validate 分支到 tools、repair 或 commit；tools/repair 回到 model，commit 后结束。模型可直接返回原 Draft，也可返回严格的 JSON tool_calls 请求；这是现有文本客户端上的明确工具协议，不依赖服务商原生 function calling。

search_notes(query,kinds,limit) 和 get_note_context(note_id) 只读取服务端绑定的用户/角色范围。工具读取的新候选加入本次校验集合及版本快照，关系写入仍由 MemoryWriter 统一原子提交。工具查询本身不强化访问次数、不修改事实。陌生/越权 ID 不返回内容，过大的结果明确报告而不截断证据。

库中有未归档笔记、但预取候选为空时，不能把空召回当作没有旧记忆；任何保存建议都须先完成一次 search_notes。该要求同时出现在模型输入和校验中，无记忆的 skipped 结果不强制检索。其余场景由模型判断是否补查。检索仍为词法检索，不能保证消除所有语义重复。

单次执行最多 2 轮工具、4 次只读调用、4 次模型调用（含最多一次校验纠错），共用 3×整理超时的截止时间。无新记忆仍校验跳过理由并完成原任务；无本批用户消息时可直接完成。所有调用与节点、工具输入/结果分别进入已有 trace。

ExtractionService 负责领取任务、租约/心跳、执行图和记录结果，图负责步骤与条件循环；App 与 SessionCloseService 共用一个服务实例。现有 SQLite 任务/事务继续作为业务状态依据，本阶段不新增图检查点或后台重试层。聊天图与摘要任务暂不重编。

正常业务入口为 ExtractionService.run；ctx.workflows.extraction 是该服务内部的编译图，执行所需的已领取 job、截止时间和运行上下文由 run 提供。进程退出后重新执行同一固定批次，不承诺从模型/工具节点中间续跑。

## 1. 聊天：从发送到正式回复

主线：`send → ws_chat / receive_message → ChatService.receive → GenerationRepo.receive → reply_events → PromptService.build → LLM → save_message → handleWs`。

| 阶段 | 代码入口 | Review 问题 |
|---|---|---|
| 点击发送 | [web/app.js](../src/companion_ever/web/app.js) 的 send、readOutbox、writeOutbox | client_message_id 是否稳定？临时气泡是否标记 pending？断线后是否仍有可重送的内容？ |
| 接收与授权 | [api/routes/chat.py](../src/companion_ever/api/routes/chat.py) 的 ws_chat、receive_message；[chat_service.py](../src/companion_ever/services/chat_service.py) 的 receive | WS 与 HTTP 是否使用相同用户/角色范围、校验和业务接收入口？ |
| 输入落库 | [generation_repo.py](../src/companion_ever/storage/generation_repo.py) 的 receive | 用户消息与 queued generation 是否同事务保存？重复请求是否返回同一记录？相同 ID 不同正文如何处理？ |
| 后台执行 | chat.py 的 _start_reply / _launch_reply；ChatService.reply_events | 执行是否先于确认启动？重复执行能否成功 claim 两次？页面断开后是否仍有执行者？ |
| 构造上下文 | [prompt_service.py](../src/companion_ever/services/prompt_service.py) 的 build；[retrieval_service.py](../src/companion_ever/services/retrieval_service.py) 的 search | 当前输入是否恰好一次？历史是否截止到本次输入？固定笔记、角色设定、相关记忆和上次成功摘要分别从哪里来？ |
| 模型通信 | [llm/openai_client.py](../src/companion_ever/llm/openai_client.py)、[traced_client.py](../src/companion_ever/llm/traced_client.py) | 实际 messages 是否与预期一致？思考字段、空输出、截断流与异常如何处理？ |
| 分条与保存 | ChatService.reply_events；[assistant_reply.py](../src/companion_ever/services/assistant_reply.py)；GenerationRepo.save_message | 分隔符跨分片时是否正确？正文与思考是否分开？每条完整回复是否只保存一次？已取消/过期生成是否仍能写入？ |
| 前端展示 | app.js 的 handleWs、renderChat、markVisibleChatRead | delta 是否只是草稿？message_done 是否替换为正式 ID？完成消息是否去重？可见且滚到最新时才标已读吗？ |

几个事件的含义：received 确认用户消息已接收；delta 是生成中的临时内容；message_done 对应已保存的完整助手消息；done 表示这次生成结束。Review 要分别核对这些时刻与实际数据库状态。

核心持久数据：conversation_log、generation、thinking_log。对应测试：`tests/test_chat_workflow.py`、`test_durable_flow.py`、`test_thinking_split.py`、`test_web_ws_flow.py`、`test_web_unread.py`。

## 2. 聊天异常：断线、重发、插话和重启

沿聊天主线分别替换一个条件，检查状态变化及恢复入口：

| 情况 | 重点代码 | 应核对的结果 |
|---|---|---|
| 服务端已收消息，received 丢失 | app.js 的 flushOutbox/recoverReply；GenerationRepo.receive | 沿用原 client_message_id 重送，不新增用户消息或重复生成 |
| 页面切后台，WS 断开 | chat.py 的 _stream_reply、producer；app.js 的 visibilitychange/pageshow、recoverReply、loadHistory | 推送订阅结束后生成继续；回前台通过 HTTP 恢复已保存消息，旧历史响应不覆盖新回复 |
| 回复 A 尚未结束，又发送 B | GenerationRepo.receive、active、save_message；handleWs | A 的已保存消息保留，未完成草稿取消；A 的迟到事件不能覆盖 B |
| 模型失败后重试 | GenerationRepo.retry；app.js 的 retryReply | 复用原用户消息，新执行 ID 隔离迟到回调；已有部分正式回复时不静默重生成 |
| 服务进程重启 | [api/server.py](../src/companion_ever/api/server.py) 的 _sweep_services；resume_queued_replies、GenerationRepo.recover | queued 可继续执行；过期 running 明确失败，已保存回复仍可查 |

聊天 generation 的状态为 queued → running → completed / failed / cancelled。这里的恢复政策与后台记忆任务不同，分别检查。

对应测试：`tests/test_chat_reconnect.py`、`test_web_reconnect.py`、`test_web_ws_flow.py`、`test_durable_flow.py`。读测试时查看它制造了什么故障、断言了什么状态；仅正常返回的断言不足以验证恢复。

## 3. 记录整理：从原话到 note

主线：`定时扫描 → BackgroundService._memory → close_session_background → memory_job → ExtractionService.run → load_input → prefetch → model → validate ↔ tools/repair → commit → MemoryWriter.apply → record_done`。

| 阶段 | 代码入口 | Review 问题 |
|---|---|---|
| 自动触发 | [api/server.py](../src/companion_ever/api/server.py) 的 _session_sweeper；[background_service.py](../src/companion_ever/services/background_service.py) 的 tick/_memory | 消息阈值、空闲条件、未完成任务和正在生成的聊天是否正确处理？ |
| 固定批次 | [memory_job_repo.py](../src/companion_ever/storage/memory_job_repo.py) 的 enqueue | 起点来自哪里？范围是否固定？新消息是否留给后续批次？超长单条是否完整保留？ |
| 启动与连续处理 | [session_close_service.py](../src/companion_ever/services/session_close_service.py) 的 close_session_background/_run | 有积压时谁继续下一批？marker 与成功进度是否分开？ |
| 图与准备材料 | [extraction_workflow.py](../src/companion_ever/workflows/extraction_workflow.py)；[extraction_service.py](../src/companion_ever/services/extraction_service.py) 的 _load_input/_prefetch | 本批新消息、参考前文和候选是否明确区分？候选检索是否受角色范围、数量和字符预算约束？ |
| 生成建议/请求工具 | _model；[memory_draft.py](../src/companion_ever/services/memory_draft.py) 的 draft_messages；[memory_draft_system.txt](../src/companion_ever/prompts/memory_draft_system.txt) | 模型能提哪些动作？Draft 与工具请求是否互斥？跳过和不确定内容如何表示？ |
| 只读工具 | _tools；[memory_tools.py](../src/companion_ever/services/memory_tools.py) | 模型能否改变授权范围？补查的正文/证据/关系是否完整可核查？新候选是否进入校验快照？超量与循环如何停止？ |
| 校验和纠错 | _validate/_repair；validate_draft；[structured_generation.py](../src/companion_ever/services/structured_generation.py) 的 correction_messages | 引文是否逐字属于本批用户原话？用户新消息是否覆盖？目标/关系是否合法？纠错与工具交互是否共享预算？ |
| 原子写入 | [memory_writer.py](../src/companion_ever/services/memory_writer.py) 的 apply | 是否重新核对实际来源、候选版本和 token？任一条失败时，笔记/证据/关系/成功状态是否全部回滚？ |
| 更新页面 | chat.py 的 notify_record_done；app.js 的 record_done | 通知来自已提交结果吗？多批处理中是否被误报为全部完成？漏推送后是否仍能重新查询？ |

写入器重点跟四个分支：create 的事实精确防重、confirm 追加证据、append 保留经历旧版本、revise 创建新事实并建立 supersedes。核对同一批的多条建议最终是否意外落在同一个 note ID。

核心持久数据：memory_job、note、note_evidence、note_revision、note_link。fact 写入还会产生操作流水，连接下一条链路。

对应测试：`tests/test_memory_graph.py`、`test_kind_storage.py`、`test_extraction_guards.py`、`test_memory_pipeline.py`、`test_background_service.py`。其中图测试实际走检索→读证据/关系→确认旧 fact+新 episode→原子写关系，另测范围、调用预算、空召回必查、超大结果与并发变更；原子回滚测试会在任务标成功后注入异常，检查整批仍被撤回。

## 4. 摘要演化：从 fact 操作回到聊天上下文

主线：`fact 写入事务 → fact_change → BackgroundService._summary → summary_job → SummaryService._execute → 校验/原子替换 → summary_updated → PromptService.build`。

| 阶段 | 代码入口 | Review 问题 |
|---|---|---|
| 记录操作依据 | [storage/summary_schema.sql](../src/companion_ever/storage/summary_schema.sql) 的触发器 | 新增、修改、删除、归档、证据和替代变化是否都记录？事务回滚是否也撤回流水？硬删除后是否还有撤回依据？ |
| 决定何时整理 | BackgroundService._summary/memory_settled；[summary_repo.py](../src/companion_ever/storage/summary_repo.py) 的 change_window/enqueue | 首次、实质变化阈值、等待时间、被引用事实失效如何触发？原始操作很多时是否仍推进？是否与同角色记忆任务错开？ |
| 准备增量输入 | [summary_changes.py](../src/companion_ever/services/summary_changes.py) 的 coalesce_changes；[summary_service.py](../src/companion_ever/services/summary_service.py) 的 _execute；SummaryRepo.affected/fact | 是否先合并同一 fact 的净变化？原始流水是否保留？仅有元数据/证据/改回原值时是否免模型消费？删除/归档/被替代事实是否被排除？ |
| 模型整理与校验 | [summary_system.txt](../src/companion_ever/prompts/summary_system.txt)；_citation_errors/_validate | quote 是否来自 fact.content？旧操作和聊天引文是否被误当当前证据？reality、条数和字段容量是否保持约束？ |
| 提交 | _execute 的 transaction | 模型运行期间来源或摘要变化时是否拒绝旧结果？两个摘要、引用版本、checkpoint、任务成功是否一起提交？ |
| 持续用于对话 | SummaryService.status/prompt_text；PromptService.build | 排队、运行中或更新失败时是否仍注入上次成功摘要？新批次提交后是否整体替换？是否明确近期用户纠正优先？ |
| 页面更新 | chat.py 的 notify_summary_updated；app.js 的 summary_updated、openRelation | 页面是否可查看来源？显示中的历史摘要与模型实际使用的摘要是否区分清楚？ |

额外检查两条无需模型的分支：净变化为零且旧结论仍有效时复用摘要；没有任何可用依据时清空摘要。后台续批只覆盖启动时已有的积压，执行中新变化重新受调度门槛约束。角色基础设定不在摘要写入范围中，摘要也不创建新 note。

核心持久数据：fact_change、summary_job、summary_checkpoint、memory_block。对应测试：`tests/test_summary_evolution.py`、`test_background_service.py`、`test_task_transport.py`。

## 5. 三条链路共同检查的内容

- **范围与权限：** 同一 user/persona 是否贯穿请求、候选、来源、关系、写入、状态查询与推送？入口校验之后，写入是否仍检查实际数据？
- **成功边界：** 模型返回、校验通过、事务提交、页面收到通知是四个时刻；状态和计数应对应实际提交结果。
- **并发与恢复：** [task_runtime.py](../src/companion_ever/services/task_runtime.py) 的 slot/execute/_heartbeat/shutdown；检查先取得名额再 claim、独立续租、旧 token 拒写、中断预算和失败重试分类。
- **可观测性：** 将 trace 的 input_case、llm-01.payload、原始 response、validation 和 db.commit 串起来；用 job_id、attempt_id 定位执行，不把触发查询当成模型执行。方法见 [TRACING](TRACING.md)。
- **测试证据：** `tests/test_task_runtime.py` 覆盖续租异常、写入前丢租约与进程重启；`test_task_traces.py` 覆盖调用前落盘、部分回复和纠错保留。检查实际断言能否发现你担心的错误。

## 6. 用一个案例贯穿 Review

在临时数据库中跟踪“我长期喜欢茶，不喜欢咖啡”：先找到用户 message ID 和 generation，再找到处理这句话的 memory_job、note_evidence、fact_change、summary_job，最后查看下一次聊天的实际 payload。

再加入“更正，我现在更喜欢咖啡，不喜欢茶了”：检查新事实、supersedes、旧事实的保留及检索有效性、摘要撤回/重建。分别在模型响应前、校验后写入前、提交后推送前制造中断，观察哪些数据已经正式保存、重试是否重复、进度是否越过失败范围。

每轮可按下表记自己的结论，定位到具体函数或 SQL：

| 入口/触发 | 本批输入与 ID | 写入及事务边界 | 成功/失败/恢复 | 已核对测试或 trace |
|---|---|---|---|---|
| 用户操作或后台条件 | 来源、候选、固定范围 | 涉及表、版本/token | 状态变化、重试责任方 | 文件与具体断言/产物 |

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
