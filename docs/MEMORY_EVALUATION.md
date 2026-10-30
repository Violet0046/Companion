# 验证与开发工具

常规入口：`python tools/check.py`。每个脚本测试在独立进程、临时数据库和本地假模型环境下运行，前端回归通过 Node.js 执行实际网页逻辑。不要只跑 pytest，它不能自动覆盖全部脚本 main()。

## 保留的工具

| 工具 | 用途 |
|---|---|
| `tools/check.py` | 全部回归，不调用模型 |
| `tools/evaluate_memory.py` | 固定记忆样本的参考验证、旧输出复评或显式模型评测 |
| `tools/evaluate_retrieval.py` | 临时库中的实际检索评测，不调用模型 |
| `tools/verify_migration.py` | 只读复制现有库，在副本核对迁移、原字段、FTS、完整性及重复启动 |
| `tools/verify_background_recovery.py` | 临时副本启动实际 HTTP 应用，验证自动记忆、摘要、续租与恢复；必须显式 `--model` |

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
```

后两条会向 .env 的服务商发送对应样本并产生费用；后台恢复工具使用源库的私人任务数据。它只写临时副本，将报告、测试快照和完整 trace 保存在 memory/reviews/，不导回正式库。可用 `--resume-db` 从旧测试快照继续，不重复生成已经完成的笔记。

## 样本与解释边界

`tests/fixtures/memory_cases.json` 有 18 个虚构样本，只有 C03/C04/C07/C08/C13/C15 的已确认范围计入规则分母，其余是探索样本。C15 仅确认“不升级长期偏好”，不要求一律保留或丢弃经历；C07 的日常吐槽边界不能推广到所有经历。

模型输入仅含消息、候选、范围及可执行结构要求，不含人工期望、审阅反馈或评测答案。输出绑定输入指纹；缺失、生成失败、结构错误和规则不通过分别报告。结构合法率不是语义准确率，未审阅样本不会被当作已确认答案。

私人原话与早期审阅保留在 gitignored 的 memory/reviews/，不混入公开 fixtures，也不作为运行时依赖。

## 最近基线（2026-10-30）

- 全量回归 40 个脚本套件通过；涵盖证据拒绝、原子回滚、旧 token、真实子进程退出后接管、后台消息恢复、已读状态与权限。
- MiniMax-M3 按当前后台配置实际评测上述六例，结构 6/6、已确认规则 6/6。报告为 `memory/reviews/2026-10-30/background-rules-v5.json`，逐次请求/回复在同名 .traces/。
- 自动恢复测试副本两批记忆成功；一次 SSL 连接中断保留原重试计划，从测试快照启动后七批摘要追平 93 条操作，current=true、pending_changes=0，外键与完整性通过。失败和成功报告均保留为 background-v5-citations、background-v5-resumed，不把副本结果冒充正式任务状态。
- 固定检索集 `tests/fixtures/memory_retrieval.json`：18 个查询，正相关宏平均 Recall@5 78.6%，空召回 4/4，无关/重复占位 0%。这是小型虚构语料；改述与指代仍有漏召回，不代表真实聊天整体表现。

这次清理只调整文档、工具与已失效实现；验证不需要重跑收费模型。具体遗留问题见 [REVIEW_NOTES](../REVIEW_NOTES.md)。
