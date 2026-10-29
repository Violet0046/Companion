# 消息与记忆任务链路

本轮将聊天边界、模型生成、记忆处理进度分开。入口在 `services/chat_service.py` 与 `services/extraction_service.py`。

## 消息

收到输入后，在同一事务内保存用户消息和 queued generation。`client_message_id` 在同一聊天内唯一；相同 ID、相同内容的重发返回已有记录，不重跑模型。相同 ID 携带不同内容会被拒绝。

WebSocket 回复 received 事件后继续接收用户输入，生成在独立工作线程执行。新输入取消本聊天之前 queued/running 的生成；已完整保存的助手消息保留，未完成草稿取消。过期生成不能继续保存正文。

每次生成默认可返回 1～3 条完整消息，提示词使用换行包围的 `<message_break>` 分隔。解析器支持分隔符被切成多个网络片段；前端不显示分隔符。未使用分隔符时仍是一条回复。

每条完整助手消息独立保存，并记录 generation_id、output_index、reply_to_message_id。turn_index 只保留兼容用途，旧的每轮每角色唯一限制已移除。

生成中的临时气泡不作为正式消息参与记忆。客户端过滤旧生成的迟到事件，并以正式 message ID 去重。未确认收到的输入保存在当前聊天的浏览器 outbox，重连时携带同一请求 ID 重发。

取消是逻辑取消：立即停止继续显示/落库；已阻塞的模型网络调用可能直到下一个片段或超时才退出，不能承诺服务商立即停止计费。连接断开会取消未完成生成，完整消息和用户输入仍保留。

生成任务超过租约会标为 failed，保留错误和输入，不自动重复生成可能已经交付的内容。

## 记忆任务

memory_job 记录 conversation_id、after_message_id、through_message_id、status、attempt_count、retry_at、lease_token、lease_expires_at、last_error，以及创建/开始/完成时间。

范围为 `(after_message_id, through_message_id]`，只读取该聊天的正式 user/assistant 消息。一批最多 200 条，创建后不随新消息扩大。一个聊天最多一个未成功任务，不同聊天独立执行。

领取任务后设置 300 秒租约，每 30 秒续期。模型调用发生在事务之外。事实、可核查的来源引用、画像更新和任务成功状态在同一事务提交。任何条目失败，整批回滚。嵌套仓库操作使用 SAVEPOINT，不提前提交。

失败按 30/60 秒间隔重试，最多三次；达到上限保留 failed，阻塞该聊天后续记忆整理，聊天功能照常可用。手动重试会重置次数，并保持原范围。

完成一批后如仍有新消息则排入下一批。服务启动及每 60 秒恢复 queued、到期 running 和可重试 failed；该恢复不依赖 idle_session_minutes。手动整理与空闲边界都是触发方式，marker 不再表示抽取进度，失败时不删除 marker。

note_evidence 保存能够在本批用户原话中逐字核对的引用。旧格式抽取响应仍兼容，缺少可验证来源时不会伪造来源记录。本轮沿用现有事实/画像抽取器；两层记忆草稿和自动关系维护尚未接入正式写入。

## 接口

- `POST /api/messages`：先接收并返回 message_id、generation_id，后台生成；支持 client_message_id。
- `POST /api/chat`：保留同步调试入口，输入先保存，HTTP 响应仍等待生成结束。
- `GET /api/persona/{user}/{persona}/generations/{id}`：查看生成状态及完整消息。
- `GET /api/persona/{user}/{persona}/memory-jobs`：最近 50 个记忆任务及失败原因。
- `POST /api/persona/{user}/{persona}/memory-jobs/{id}/retry`：重试失败任务。
- WebSocket：新增 received、message_done、cancelled；回复事件携带 generation_id。

角色总览的 last_record_at 改为任务成功完成时间，不再把 marker 创建时间当作抽取完成。

## 迁移与历史

首次启动时 `storage/migrations.py` 将数据库迁移到 user_version=1。有历史数据时，先用 SQLite backup API 创建同目录 `.bak-before-jobs-*` 备份，包含 WAL 中的提交数据。

迁移在事务内重建消息表，保留消息 ID、原有字段、思考外键和 AUTOINCREMENT 高水位；检查外键后提交。重复启动不重复迁移。

**历史边界是显式兼容策略：迁移时已有消息保留，旧笔记继续可召回，新任务从该聊天迁移时的最大消息 ID 之后开始。** 此值存为 conversation.memory_start_message_id，不表示历史已经被新系统成功抽取。不会自动重抽全部历史；历史重建需后续单独安排，旧 marker 也不会伪装成成功任务。

本轮已在真实数据库副本上核对所有原消息、思考、笔记、关系、画像和设置字段，验证外键与完整性；原数据库未因该验证发生改变。下次正常启动会执行正式迁移，建议先停止旧版本服务再启动新版本。

## 验证

`tests/test_durable_flow.py` 覆盖旧库迁移、重复请求、模型失败保留输入、多气泡与跨 chunk 分隔符、批次整体回滚、固定范围、过期租约接管、旧执行者拒写及 WebSocket 插话。
`tests/test_web_ws_flow.py` 在 Node 中执行实际前端消息处理，验证连续发送、旧事件过滤与正式消息去重。全部测试使用假模型，不调用外部 API。
