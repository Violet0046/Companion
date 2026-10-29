# 代码阅读指南

本轮的目的：业务规则只写一份，按功能名能找到代码，不通过增加抽象层准备未来功能。

## 建议阅读顺序

1. `src/companion_ever/app.py`：查看全部依赖如何装配。
2. `services/chat_service.py`：一轮聊天，先组装上下文、调用模型，再保存回复。同步和流式共用 `_save_reply`。
3. `services/prompt_service.py`：人设、画像、关系、近期历史、召回记忆如何进入 prompt。近期历史跨抽取边界保留。
4. `services/retrieval_service.py` 与 `storage/note_repo.py`：候选如何产生、排序和去重。
5. `services/session_close_service.py` → `services/extraction_service.py`：抽取边界、模型 JSON、事实写入、失败处理。
6. `storage/schema.sql`：核对数据约束，尤其是当前单轮单助手消息的限制。
7. 最后看 `api/`、`cli/` 和 `web/`，它们是使用这些业务规则的入口。

上述路径除第一项外都相对 `src/companion_ever/`。

## 两条主要调用链

```text
WebSocket / HTTP → ChatService → PromptService → RetrievalService → NoteRepo
                            → LLM → 共用回复保存入口 → ConversationService
CLI → chat_workflow（路由） → 同一个 ChatService

手动 / 空闲关闭 → SessionCloseService → extraction_workflow（适配）
                                   → ExtractionService
                                   → NoteService.remember / BlockService
```

LangGraph 目前没有承担持久化任务队列或复杂 agent 决策；保留现有 invoke 接口，图内不再复制业务代码。直接调用抽取服务与图入口使用相同规则。未来手机实现不要求复刻 LangGraph。

## 职责边界

- `services`：业务规则与用例。现在直接依赖具体仓库，不提前增加接口工厂、事件总线或通用插件层。
- `storage`：SQL 和数据映射；`remember` 的查重与插入在同一事务中，防并发重复。
- `llm`：模型通信。聊天和抽取是两种任务，目前共享一个配置和客户端，并非两个独立模型实例。
- `domain`：数据结构与类型。
- `api` / `cli`：传输和用户输入；不能各自维护抽取或回复保存规则。
- `workflows`：现有调用方的适配，不是第二套业务层。

## 本轮改变的行为

- 相同用户、角色、类型、去首尾空白后完全相同的活跃事实，抽取时返回已有 ID，新增计数为零。
- 手动 `create_note` 仍是明确的新增操作。既有重复未自动删除，召回时相同类型和正文只占一个位置。
- 精确去重不做标点删除、同义改写合并，不更新原有证据或时间；归档事实不阻止重新写入。
- 关键词命中的候选排在不命中的常驻候选前，再按现有权重排序。此规则不等价于语义理解。
- FTS 用稳定的 `note_id` 连接笔记，不假设两个表的 rowid 始终相同；LIKE 将用户的 `%`、`_` 视为字面字符。
- 无效抽取 JSON 和部分条目校验失败会标记失败，关闭服务撤销本次 marker，供下一次重试。
- `history_max_messages` 配置实际传入 PromptService。

## 保留与移除

保留权限归属校验、输入校验、失败可见性、SQLite 事务、思考与正文分离、trace，以及有具体事故依据的回归测试。

移除重复抽取实现、图内重复聊天实现、同步/流式重复保存代码、重复的抽取报告模型；压缩文件开头的历史过程说明。抽取写入只捕获预期的输入错误，数据库异常向上报告，不伪装成成功。

旧 Python 导入路径 `application.*` / `infrastructure.*` 已改为 `services.*` / `storage.*` / `llm.*` / `trace`，仓库内调用方和测试同步更新；外部个人脚本需同步修改。CLI 命令与 HTTP 路径保持原接口。

## 暂未解决的边界

- 会话关闭仍使用 marker，后台任务不是持久化队列；并发关闭和进程退出恢复需要下一阶段处理。
- 回复仍在模型完成后保存，缺少消息 pending/failed 状态和重试幂等 ID。
- 部分抽取写入成功后再失败，旧事实可安全重试；整个批次和画像更新尚不是原子提交。
- `last_reasoning` / `last_usage` 仍是共享客户端可变状态，多角色并发需改为每次调用的返回对象。
- 1000 条抽取窗口上限可能截掉超长会话的早期消息，需要按稳定范围分批和记录进度。
- 前端 `app.js` 仍较大。本轮集中重构后端，移动端选型前不做一次无用的前端框架迁移。
