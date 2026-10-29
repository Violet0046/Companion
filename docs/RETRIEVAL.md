# 统一记忆检索与关系扩展

入口：`ctx.services.retrieval.search(user_id, persona_id, MemorySearch(...))`。
现有聊天 `retrieve()` 已委托这个入口。记忆整理可使用同一入口的 consolidation 模式；两层记忆写入器仍未接入。

## 请求

| 字段 | 行为 |
|---|---|
| query | 当前查询，可空；最多 2000 字 |
| context_query | 调用方明确提供的近期话题补充，最多 1000 字；不自动猜指代 |
| purpose | chat 或 consolidation |
| kinds | episode / fact 的任意子集 |
| limit | 最多 1～50 条结果 |
| max_hops | 0～2，默认一跳 |
| seed_ids | 最多 10 个明确的起始笔记 ID，仍受权限限制 |
| recent_limit | 0～10，默认 0；只有 consolidation 可使用近期兜底 |

调用方提供用户和角色范围；请求体不能覆盖范围。未拥有角色会拒绝访问，外部角色的 seed ID 不会进入结果。

## 选择过程

1. 使用现有 SQLite FTS5 + 中文短词 LIKE 召回 query/context_query 的候选。
2. 对有实际词法匹配的候选打分，每路最多取 20 个入口；不再用 always_include_types 填充无关笔记。
3. 合并明确 seed，整理用途才可显式加入少量近期笔记。
4. 查询双向关系，最多两跳、64 个节点；每轮最多读取 100 条关系。
5. 过滤归档笔记、类型、已被取代事实及重复正文，最后限制返回数量。

目前 backend 是 lexical+graph，尚无向量或语义重排；措辞不同可能找不到入口，图也不能凭空解决入口丢失。
没有种子时返回空结果，不启动全图遍历。空召回不再触发原有“检索静默失败”不变量。

## 关系方向

- supports：经历 → 事实；从事实可以反查支持它的经历。
- continues：新经历 → 前序经历；从旧经历可以反查后续，在当前有限范围内优先后续。
- supersedes：新事实 → 旧事实；chat 隐藏已有有效后继的旧事实，consolidation 保留其上下文并标记 superseded。

归档笔记可以作为词法入口，帮助找到未归档的后继，但不会直接出现在结果里。
仅信任方向明确、端点类型符合约定的三种关系；未知类型或错误类型组合不参与扩展。
两端均进行用户/角色隔离，不能通过跨角色脏边绕过权限。

环路使用访问集合限制；supersedes 环路和多后继会返回警告，不声称已确定唯一最新状态。
达到跳数、节点、候选或输出数量限制时可标记 truncated。有限扩展不保证得到整条历史的最终状态，调用方必须留意截断提示。

本轮不自动生成或修改 note_link，也不迁移、清洗用户笔记。当前两层分类从既有 type 推导：event/milestone 等视为经历，其余视为事实；并不声称旧笔记已完成两层迁移。

## 结果与 API

返回 hits、relations、warnings、truncated。每个 hit 包含 note、推导 kind、score、reasons、关系路径 ID、superseded 标记，以及最多 3 条有范围校验的原话证据。
created_at / updated_at 是笔记存储时间，不当作事件发生时间。score 是当前排序值，不是准确率或可信度概率。

`POST /api/persona/{user}/{persona}/memories/search` 提供同样的只读 JSON 接口，沿用 PIN 和角色归属校验。
聊天的 trace 记录候选数、返回数、路径、原因和警告，方便检查为何召回；还没有检索结果的专用前端页面。

## 验证与后续

`tests/test_memory_search.py` 用虚构笔记覆盖空召回、归档入口、双向关系、新事实替代、两种用途、类型过滤、跨角色脏边、循环、预算、英文大小写及 API 权限。

后续待做：

- 让两层整理器使用 consolidation 候选并可靠地维护笔记与关系。
- 用已审阅样本评测不同措辞的召回，再决定语义通道。
- 模型主动调用检索工具：本轮不实现，也未改变 LLM 协议。
- 用户反馈的连续发送后可能不回复问题：保留为待修复，专项假模型测试通过不能代替真实使用验证。
