# 验证与开发工具

常规入口：`python tools/check.py`。每个脚本测试在独立进程、临时数据库和本地假模型环境下运行，前端回归通过 Node.js 执行实际网页逻辑。不要只跑 pytest，它不能自动覆盖全部脚本 main()。

## 保留的工具

| 工具 | 用途 |
|---|---|
| `tools/check.py` | 全部回归，不调用模型 |
| `tools/evaluate_memory.py` | 单批草稿参考/复评/模型评测；--flow 用临时库跑跨轮契约并合计逐次调用 |
| `tools/evaluate_retrieval.py` | 临时库中的实际检索评测，不调用模型 |
| `tools/verify_migration.py` | 只读复制现有库，在副本核对迁移、原字段、FTS、完整性及重复启动 |
| `tools/verify_background_recovery.py` | 实际 HTTP 应用的自动任务/恢复；--memory-graph 验证图/工具协议；必须显式 --model |

数据库备份和 trace 查看统一使用 CLI，不再维护另一套同用途脚本。

```powershell
# 无模型：验证公共参考输出和检索
python tools/evaluate_memory.py --reference --output memory/reviews/local/reference.json
python tools/evaluate_retrieval.py --output memory/reviews/local/retrieval.json
python tools/verify_migration.py
python tools/evaluate_memory.py --flow --reference --output memory/reviews/local/flows-reference.json

# 显式模型调用：六项已确认公共规则
python tools/evaluate_memory.py --model --background --case C03 C04 C07 C08 C13 C15 --output memory/reviews/local/model.json

# 显式模型调用：自动任务端到端验证
python tools/verify_background_recovery.py --model --output memory/reviews/local/background.json

# 显式模型调用：虚构数据，初次漏召回后的主动检索和事实复用
python tools/verify_background_recovery.py --model --memory-graph --output memory/reviews/local/memory-graph.json

# 六组人工确认工程预期的跨轮场景：运行生产整理图、摘要图与召回
python tools/evaluate_memory.py --flow --model --output memory/reviews/local/flows-model.json

```

显式模型命令会向 .env 的服务商发送对应样本并产生费用。后台恢复默认使用源库的私人任务数据，只写临时副本；报告、测试快照和完整 trace 保存在 memory/reviews/，不导回正式库。可用 `--resume-db` 从旧测试快照继续。`--memory-graph` 不打开源库，只使用两个虚构批次，故意让预取为空以验证模型主动检索；它验证图与模型协议，不等同于定时调度验证。

--flow 仅用虚构临时库。跨轮工程预期已获用户确认，具体措辞/摘要语义质量仍待人工审阅；期待值和 reference 草稿只由本地评测器读取，模型只收到生产服务实际构造的输入。reference 通过只代表链路契约可运行，不代表模型效果。--flow 不支持把单批草稿响应文件当作跨轮重放输入，原始响应保存在同名 .traces/ 中。

## 样本与解释边界

`tests/fixtures/memory_cases.json` 有 18 个虚构样本，只有 C03/C04/C07/C08/C13/C15 的已确认范围计入规则分母，其余是探索样本。C15 仅确认“不升级长期偏好”，不要求一律保留或丢弃经历；C07 的日常吐槽边界不能推广到所有经历。

模型输入仅含消息、候选、范围及可执行结构要求，不含人工期望、审阅反馈或评测答案。输出绑定输入指纹；缺失、生成失败、结构错误和规则不通过分别报告。结构合法率不是语义准确率，未审阅样本不会被当作已确认答案。

私人原话与早期审阅保留在 gitignored 的 memory/reviews/，不混入公开 fixtures，也不作为运行时依赖。

## 最近基线（2026-10-31）

- 全量回归 46 个脚本套件通过；涵盖两条真实图的生成/复用/清空/纠错、跨轮契约、实际用量及未知字段、时间/历史窗口、不隐式猜检索主题、成本调度、证据拒绝、原子回滚、旧 token、子进程退出接管、后台消息恢复、已读状态与权限。
- MiniMax-M3 按当时后台配置实际评测上述六例，结构 6/6、已确认规则 6/6。报告为 `memory/reviews/2026-10-30/background-rules-v5.json`，逐次请求/回复在同名 .traces/。
- 记忆图的真实模型验证：MiniMax-M3 在预取为空时主动调用 search_notes，2 次模型调用复用原 fact 并补证据；下一批无长期信息，1 次调用、无工具、无写入完成。报告为 `memory/reviews/2026-10-30/memory-graph-v2.json`，同名 .traces/ 保留每次输入/输出。v1 记录了模型跳过检索而重复新建的失败，随后增加空召回必查规则；该小样本不代表语义去重准确率。
- 自动恢复测试副本两批记忆成功；一次 SSL 连接中断保留原重试计划，从测试快照启动后七批摘要追平 93 条操作，current=true、pending_changes=0，外键与完整性通过。失败和成功报告均保留为 background-v5-citations、background-v5-resumed，不把副本结果冒充正式任务状态。
- 固定检索集 `tests/fixtures/memory_retrieval.json`：18 个查询，正相关宏平均 Recall@5 78.6%，空召回 4/4，无关/重复占位 0%。这是小型虚构语料；改述与指代仍有漏召回，不代表真实聊天整体表现。

离线图回归另覆盖两轮补查后的去重/关系原子写入、越权拒绝、调用与时间预算、超大工具结果、必要检索被跳过和并发修改拒写。具体遗留问题见 [REVIEW_NOTES](../REVIEW_NOTES.md)。

角色模块重构的 `test_persona_service.py` 使用临时库与假模型，覆盖并发创建不丢角色、创建/停用失败整体回滚、有效列表撤权、内容保留与恢复、旧聊天/整理/摘要/编辑结果拒写，以及永久失败任务不因恢复而重试。v5→v6 另验证唯一约束、编辑版本、重复设定拒绝迁移且不改数据；现有正式库仅以只读源生成临时副本，迁移后 18 张历史表保留，外键与完整性检查通过，原库仍为 v5。

会话模块的 `test_conversation_execution.py` 不启动 Web，直接验证多订阅者只产生一次模型调用、订阅异常与事件篡改不影响生成或其他订阅者、CLI 同步重放共用正式消息、排队回执补执行、后台恢复、取消保留完整气泡、失败显式重试、跨角色权限及思考可见性。未读数量查询已归会话服务，原有本机游标语义不变。HTTP/WS 的断线与丢回执继续通过 test_chat_reconnect/test_durable_flow 验证。

`test_chat_service.py` 替代旧聊天图测试：验证普通多轮的轮次持久化、CLI 输入 `/quit`/`/exit` 仍正常回复并可继续聊天，EOF/Ctrl+C 只退出且不创建整理任务。维护整理继续通过显式服务测试，不再依赖聊天文本触发。

记忆模块的 `test_memory_boundaries.py` 约束业务用例不直接执行 SQL、调度不依赖应用容器、生产模块不导入 Web 或离线评测；真实 HTTP 另验证任务/操作记录的范围隔离、404/403/409、显式失败重试、完成时间与摘要实例复用。两条图、成本调度、租约与原子提交继续由原有测试覆盖，六组离线跨轮契约全部通过。评测实现已归 tools/evaluation，入口和夹具不变；本轮未调用收费模型，也未迁移正式数据库。

`test_cost_aware_schedule.py` 核对空闲最低消息数、24 小时兜底、不同 fact 的净变化计数、只优先处理摘要引用的失效、200+ 条证据免模型消费、执行中新输入重新受门槛约束，以及整理完成后立即评估摘要。短消息批次回归中 85 条消息由 8 次整理降为 1 次；65 条简单 fact 的摘要初始化由 6 批降为 3 批。这是确定性测试中的调用数变化，不等于实际 token 或语义质量收益。

新调度的真实三层验证使用临时库、12 条虚构中文用户消息及固定短应答：自动整理一次生成 11 条笔记，完成回调自动触发一次摘要，随后调用一次正常聊天追问；均成功，完整性通过。`memory/reviews/2026-10-30/scheduling-usage-v1.json` 保存服务商实际 usage：整理输入/输出 4718/1416、摘要 3504/831、追问聊天 1285/283 tokens；这不是 12 轮聊天总成本，也不是普遍均值。该临时验证显式开启流式 include_usage，正式链路的历史字符粗估不能与这些实际值混算。报告旁 .traces/ 保留原始调用，不读取或写入正式数据库。

跨轮真实模型报告为 `memory/reviews/2026-10-30/flow-model-after-v3.json`：六组契约 6/6，整理和摘要的用量、缓存及耗时按层汇总，trace_write_error=false。早期 F02 失败来自评测器关系计数查询错误，不能当模型质量失败；F06 确实暴露持续约定被归为 episode，补充提示词后通过。F03/F05 的仅年份输出触发纠错，时间精度规则补充后的定向报告为 flow-time-precision-v4：均通过，但仍可能需要一次纠错，未放宽日期校验。

缓存隔离对照为 `memory/reviews/2026-10-31/chat-cache-isolated.json`：相同模拟时钟，新增稳定前缀标识，第二轮请求共同前缀由 64 字符增为 3835；时间/旧事探针均通过。当前接入服务报告的第二轮缓存率约为旧布局 8.0%、新布局 6.2%，未证明缓存费用下降。其他复测出现过高命中，不能排除旧请求预热和服务端状态影响；不以单次高命中宣称普遍收益，也不把请求文本的共同前缀长度等同于服务端命中率。

上述布局试验已按用户决定暂停，聊天布局和额外参考消息包装已回退，相应 CLI 试验分支不保留；报告仅作为探索记录，不代表当前实现。按指代词拼最近两条用户原话的检索规则也已撤回，现有评测不足以证明其有效，不纳入质量改进结论。
