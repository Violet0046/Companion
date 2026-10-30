# 待 Review 项

这里只列仍存在的问题和未决定的方向。已完成行为以 [架构](docs/ARCHITECTURE.md)、[记忆规则](docs/MEMORY_POLICY.md)、[后台任务](docs/BACKGROUND_TASKS.md) 为准；验证入口见 [评测](docs/MEMORY_EVALUATION.md)。

## 已知问题

| 编号 | 当前情况 | 代码入口 |
|---|---|---|
| R-001～003 | decay_score 未参与评分；get_note 的访问计数未接入实际召回，排序新鲜度仍用 updated_at。暂未决定引入访问强化，应先评测。 | `storage/note_repo.py`、`services/note_service.py`、`retrieval_service.py` |
| R-006 | strength 只影响关系遍历优先级，不直接改变排序分数。是否加入评分待验证。 | `storage/note_link_repo.py`、`services/retrieval_service.py` |
| R-014 | UserRepo.remove_persona 只改用户列表，persona_definition 仍会被 Authorizer 作为归属兜底，不能真正撤权。当前无调用方；接删除角色功能前应修复。 | `storage/user_repo.py`、`services/authz.py` |
| R-015 | persona_definition 缺少用户/角色唯一约束；is_archived 没有完整归档入口。并发 put 可能产生多行，get 只取一行。 | `storage/persona_repo.py`、`storage/schema.sql` |

## 尚未决定或实现

- 语义检索/重排与语义去重：目前词法入口对改述、指代仍会漏召回，固定集合 Recall@5 为 78.6%。
- contradicts：尚未增加“冲突但无法确定先后”的边，当前仅支持三种确定关系。
- 旧 uncertain 笔记的情境确认：迁移不猜测现实/共同想象，保留待核实；不能只为消除提示而批量改为现实。
- 模型概括质量：引用合法不等于结论语义完全正确，仍需审阅实际样本。
- 独立手机 App、群聊和主动触达：仍未实现，不在本轮清理中扩展。
