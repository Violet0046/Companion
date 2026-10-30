# 验证与开发工具

常规入口：`python tools/check.py`。每个脚本测试在独立进程、临时数据库和本地假模型环境下运行，前端回归通过 Node.js 执行实际网页逻辑。不要只跑 pytest，它不能自动覆盖全部脚本 main()。

## 保留的工具

| 工具 | 用途 |
|---|---|
| `tools/check.py` | 全部回归，不调用模型 |
| `tools/evaluate_memory.py` | 固定记忆样本的参考验证、旧输出复评或显式模型评测 |
| `tools/evaluate_retrieval.py` | 临时库中的实际检索评测，不调用模型 |
| `tools/verify_migration.py` | 只读复制现有库，在副本核对迁移、原字段、FTS、完整性及重复启动 |
| `tools/verify_background_recovery.py` | 临时副本启动实际 HTTP 应用，验证自动记忆、摘要、续租与恢复；`--memory-graph` 改用虚构临时库验证图/工具协议；必须显式 `--model` |

数据库备份和 trace 查看统一使用 CLI，不再维护另一套同用途脚本。

```powershell
# 无模型：验证公共参考输出和检索
python tools/evaluate_memory.py --reference --output memory/reviews/local/reference.json
python tools/evaluate_retrieval.py --output memory/reviews/local/retrieval.json
python tools/verify_migration.py

# 显式模型调用：六项已确认公共规则
python tools/evaluate_memory.py --model --background --case C03 C04 C07 C08 C13 C15 --output memory/reviews/local/model.json

# 显式模型调用：自动任务端到端验证
python tools/verify_background_recovery.py --model --output memory/reviews/local/background.json

# 显式模型调用：虚构数据，初次漏召回后的主动检索和事实复用
python tools/verify_background_recovery.py --model --memory-graph --output memory/reviews/local/memory-graph.json
```

显式模型命令会向 .env 的服务商发送对应样本并产生费用。后台恢复默认使用源库的私人任务数据，只写临时副本；报告、测试快照和完整 trace 保存在 memory/reviews/，不导回正式库。可用 `--resume-db` 从旧测试快照继续。`--memory-graph` 不打开源库，只使用两个虚构批次，故意让预取为空以验证模型主动检索；它验证图与模型协议，不等同于定时调度验证。

## 样本与解释边界

`tests/fixtures/memory_cases.json` 有 18 个虚构样本，只有 C03/C04/C07/C08/C13/C15 的已确认范围计入规则分母，其余是探索样本。C15 仅确认“不升级长期偏好”，不要求一律保留或丢弃经历；C07 的日常吐槽边界不能推广到所有经历。

模型输入仅含消息、候选、范围及可执行结构要求，不含人工期望、审阅反馈或评测答案。输出绑定输入指纹；缺失、生成失败、结构错误和规则不通过分别报告。结构合法率不是语义准确率，未审阅样本不会被当作已确认答案。

私人原话与早期审阅保留在 gitignored 的 memory/reviews/，不混入公开 fixtures，也不作为运行时依赖。

## 最近基线（2026-10-30）

- 全量回归 42 个脚本套件通过；涵盖实际记忆图/只读工具、成本调度、证据拒绝、原子回滚、旧 token、真实子进程退出后接管、后台消息恢复、已读状态与权限。
- MiniMax-M3 按当时后台配置实际评测上述六例，结构 6/6、已确认规则 6/6。报告为 `memory/reviews/2026-10-30/background-rules-v5.json`，逐次请求/回复在同名 .traces/。
- 记忆图的真实模型验证：MiniMax-M3 在预取为空时主动调用 search_notes，2 次模型调用复用原 fact 并补证据；下一批无长期信息，1 次调用、无工具、无写入完成。报告为 `memory/reviews/2026-10-30/memory-graph-v2.json`，同名 .traces/ 保留每次输入/输出。v1 记录了模型跳过检索而重复新建的失败，随后增加空召回必查规则；该小样本不代表语义去重准确率。
- 自动恢复测试副本两批记忆成功；一次 SSL 连接中断保留原重试计划，从测试快照启动后七批摘要追平 93 条操作，current=true、pending_changes=0，外键与完整性通过。失败和成功报告均保留为 background-v5-citations、background-v5-resumed，不把副本结果冒充正式任务状态。
- 固定检索集 `tests/fixtures/memory_retrieval.json`：18 个查询，正相关宏平均 Recall@5 78.6%，空召回 4/4，无关/重复占位 0%。这是小型虚构语料；改述与指代仍有漏召回，不代表真实聊天整体表现。

离线图回归另覆盖两轮补查后的去重/关系原子写入、越权拒绝、调用与时间预算、超大工具结果、必要检索被跳过和并发修改拒写。具体遗留问题见 [REVIEW_NOTES](../REVIEW_NOTES.md)。

`test_cost_aware_schedule.py` 核对空闲最低消息数、24 小时兜底、不同 fact 的净变化计数、只优先处理摘要引用的失效、200+ 条证据免模型消费、执行中新输入重新受门槛约束，以及整理完成后立即评估摘要。短消息批次回归中 85 条消息由 8 次整理降为 1 次；65 条简单 fact 的摘要初始化由 6 批降为 3 批。这是确定性测试中的调用数变化，不等于实际 token 或语义质量收益。

新调度的真实三层验证使用临时库、12 条虚构中文用户消息及固定短应答：自动整理一次生成 11 条笔记，完成回调自动触发一次摘要，随后调用一次正常聊天追问；均成功，完整性通过。`memory/reviews/2026-10-30/scheduling-usage-v1.json` 保存服务商实际 usage：整理输入/输出 4718/1416、摘要 3504/831、追问聊天 1285/283 tokens；这不是 12 轮聊天总成本，也不是普遍均值。该临时验证显式开启流式 include_usage，正式链路的历史字符粗估不能与这些实际值混算。报告旁 .traces/ 保留原始调用，不读取或写入正式数据库。
