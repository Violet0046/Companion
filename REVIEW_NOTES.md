# 待 Review 项

这里只列仍存在的问题和未决定的方向。已完成行为以 [架构](docs/ARCHITECTURE.md)、[记忆规则](docs/MEMORY_POLICY.md)、[后台任务](docs/BACKGROUND_TASKS.md) 为准；验证入口见 [评测](docs/MEMORY_EVALUATION.md)。

## 已知问题

| 编号 | 当前情况 | 代码入口 |
|---|---|---|
| R-001～003 | decay_score 未参与评分；get_note 的访问计数未接入实际召回，排序新鲜度仍用 updated_at。暂未决定引入访问强化，应先评测。 | `memory/notes/repository.py`、`memory/notes/service.py`、`memory/retrieval/service.py` |
| R-006 | strength 只影响关系遍历优先级，不直接改变排序分数。是否加入评分待验证。 | `memory/notes/links.py`、`memory/retrieval/service.py` |

## 尚未决定或实现

- 按业务模块重构：角色、会话与记忆边界已完成，R-014/R-015 已修复；离线评测已移出生产服务，公共执行与入口收敛按 [架构迁移方案](docs/ARCHITECTURE.md) 继续推进。停用/恢复目前为业务入口，尚未增加管理页面或公开接口。

- 语义检索/重排与语义去重：目前词法入口对改述、指代仍会漏召回，固定集合 Recall@5 为 78.6%。
- contradicts：尚未增加“冲突但无法确定先后”的边，当前仅支持三种确定关系。
- 旧 uncertain 笔记的情境确认：迁移不猜测现实/共同想象，保留待核实；不能只为消除提示而批量改为现实。
- 模型概括质量：引用合法不等于结论语义完全正确，仍需审阅实际样本。
- 独立手机 App、群聊和主动触达：仍未实现，不在本轮清理中扩展。
