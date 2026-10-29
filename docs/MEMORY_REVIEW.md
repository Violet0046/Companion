# 两层记忆：先审阅，再接入

当前阶段只产出草稿，没有修改现有聊天抽取入口、数据库结构或存量记忆。

## 审阅材料

真实样本及报告保存在本地 `memory/reviews/2026-10-29/`，被 Git 忽略。
`REVIEW.md` 包含 18 个真实案例的原话、人工建议、禁止推断项及召回验收问题。
这些建议由编码助手整理，待用户确认，不能当作已标注的标准答案。
`cases.json` 是固定输入快照。`MODEL_REVIEW.md` / `.json` 是指定样本的独立模型输出。
其中 existing_notes 是明确标注的实验候选笔记，不是正式记忆库的直接导出。

样本涵盖相对日期、跨批补充、澄清、同义确认、短期状态、单次选择、共同游戏、
不确定事件归属、现实与共同想象、无相关记忆时的空召回。
没有声称这 18 例能衡量总体效果，也未计算语义召回提升。

## 运行

```powershell
# 仅渲染本地人工建议，不联网
.\.venv\Scripts\python.exe tools/review_memory.py --input memory/reviews/2026-10-29/cases.json --output memory/reviews/2026-10-29/REVIEW.md

# 会发送选中的原话、参考前文和候选笔记到 .env 指定的模型服务，产生 API 费用
.\.venv\Scripts\python.exe tools/review_memory.py --input memory/reviews/2026-10-29/cases.json --output memory/reviews/2026-10-29/MODEL_REVIEW.md --model --case C03 C08 C09 C15
```

真实私人样本不会随 Git 提交。其他开发者可按下述契约自行创建虚构输入。
不要运行 setup/build_app 来生成审阅快照：该流程不需要初始化或迁移数据库。

## 数据契约

输入是 `{"cases": [...]}`，每个 case 包含：

- id、title：样本标识与说明。
- messages：id、role、content、created_at（UTC）、local_time（带时区）。
- new_message_ids：本批新消息。其他消息仅供上下文参考，不能单独成为新记忆的依据。
- existing_notes：候选 id、kind（episode/fact）、content，以及可选来源/情境说明。
- recommendation、do_not_infer、retrieval_check、link_reason：人工审阅基线；不发送给模型。

输出是可验证的 Draft：建议、关系、跳过原因、不确定项。
建议支持 create / append / confirm / revise，必须引用新消息中的证据，非新增操作要指明已有目标。
只允许 supports（经历→事实）、continues（新经历→前序经历）、supersedes（新事实→旧事实）。
这只是草稿格式，尚不是数据库迁移方案。

## 当前验证边界

代码验证 ID、引用原文、引用范围、更新目标、关系端点和类型、用户新消息覆盖。
不以结构校验代替语义审阅：是否误解原话、时间是否推断正确、是否值得长期记忆、
共同想象是否误当现实，仍要逐项审阅。模型生成失败不会伪装成“无需记忆”。

下一步由用户确定保存粒度和关键边界，再把确认过的案例转成固定评测集，
测试候选召回和跨批次整理；之后才实现正式写入、来源表和持久化任务。
