# Companion Ever Memory — 项目知识库（面试复习卡片）

> **类型**：个人 / 研究型项目（LangGraph 长期记忆系统）
> **形式**：高频考点速记，便于把项目交给 LLM 后让其能基于此回答面试 / 设计问题
> **关键术语**：Session Sentinel / Chat-Extraction 双 LLM / FTS5 / LangGraph StateGraph / Persona Basics

---

## 1. 一句话定位

**Companion Ever Memory = 基于 SQLite + LangGraph 的对话式长期记忆系统**——
让 LLM 驱动的角色拥有跨 session 的真实记忆，每次新对话都能记住"上次说了什么"。
- Chat LLM 只管对话（拼 prompt + 调 LLM + 写 conversation_log）
- Extraction LLM 只管记忆（session 关闭时触发，从对话抽取 facts + 更新 3 个 Block）
- 两个 LLM 职责完全分离，靠 `session_close` sentinel marker 作为衔接点

---

## 2. 为什么做这个项目（业务背景）

### 痛点

- LLM-driven 角色（companion / agent）每次新 session 都"失忆"，不知道上次用户说过什么
- 业界方案要么：
  - 把所有历史塞进 prompt → context 爆炸，长会话不可行
  - 用 vector DB + RAG 检索 → 检索质量不稳定，关键上下文丢失
  - 用 fine-tuning / continual learning → 训练成本高，无法即时反映新会话

### 解法

- **Sentinel 标记 session 边界**：在 conversation_log 写 `role='session_close'` 的 marker，划分 session
- **双 LLM 分离**：
  - Chat LLM：对话过程中用当前 session 历史（session 边界不影响）→ 解决"对话连贯性"
  - Extraction LLM：session 关闭时抽取 → 写 notes + 更新 blocks → 解决"跨 session 记忆"
- **Sentinel 同时承载两层语义**：
  - 对 Chat LLM：可选的 session 边界（已废弃为 always show all）
  - 对 Extraction LLM：必读的 session 边界（决定抽哪个 session）
- **检索作为补充**：notes 用 FTS5 + LIKE 兜底，关键信息召回

### 业务结果

| 指标 | 数值 |
|---|---|
| 上下文控制 | 历史被 cap 在 `history_max_messages=50`（可配），取**最近**的 50 条（不是最早的）|
| 抽取粒度 | 单 session 内平均 N 个 facts + 1-3 个 block 更新 |
| 跨 session 记忆 | 旧 session 经抽取后，新 session 通过 retrieval 召回 |
| prompt 体积 | Chat LLM 的 system prompt 5 段（时间 / 人设 / 当前状态 / 相关记忆 / 行为指引）+ 独立的历史 role 消息（**user 带时间戳，assistant 不带**）|

---

## 3. 核心架构：双 LLM + Sentinel

```
用户输入 "宝宝，干啥呢？"
    ↓
[chat_workflow] chat_node
    ├─ prompt_service.build(query=user_input)
    │    ├─ system prompt（5 段）
    │    │    ├─ 当前时间（本地时区）
    │    │    ├─ persona_basics JSON（人设宪法）
    │    │    ├─ 3 个 Block（persona_runtime / user_profile / relationship_state）
    │    │    ├─ 相关记忆（retrieval → top_k from notes）
    │    │    └─ 行为指引（CHAT_FOOTER）
    │    └─ messages = [system] + 历史 role 消息（跨 session，cap 50）+ [当前 user_input]
    ├─ Chat LLM（仅生成回复，不做任何抽取/更新）
    └─ conversation_log: user + assistant 各一行
    ↓
[should_close 路由] user_input == "/quit" ?
    ├─ 否 → END
    └─ 是 → close_node
         ↓
[SessionCloseService.close_session]
    ├─ conversation_service.log_session_close() ← 写 sentinel marker
    └─ extraction_workflow.invoke()
         ↓
[extraction_workflow] load → extract → persist
    ├─ load: 读 [prev_marker, last_marker] 之间的对话
    ├─ extract: 调 Extraction LLM → facts + blocks
    └─ persist: 写 notes + merge 更新 blocks（per-fact 容错）
```

---

## 4. 关键设计决策 ⭐⭐⭐

### 决策 1：双 LLM 分离 ⭐⭐⭐

- **Chat LLM prompt**：system prompt 5 段（人设 + 当前状态 + 相关记忆 + 行为指引 + 时间）+ 独立的历史 role 消息
- **Extraction LLM prompt**：抽取指令 + 当前 session 对话
- **为什么分离**：
  - 单一 LLM 既要对话又要抽取，prompt 会非常臃肿
  - Extraction 是"后台活"，可以在 session 关闭时单独跑
  - Chat LLM 的 prompt 保持"干净"（不被抽取指令污染）

### 决策 2：Session Sentinel ⭐⭐⭐

- 在 `conversation_log` 写一行 `role='session_close'` 的 marker
- 不需要新表 / 新字段
- Marker 同时承载两层语义：
  - Extraction LLM 抽取边界：`[prev_marker.id+1, last_marker.id]` 之间的对话
  - Chat LLM 历史边界：之前是"只读 [marker+1, now]"，现在是"读所有（不截断）"

### 决策 3：用 id 边界而非时间戳边界

- SQLite `CURRENT_TIMESTAMP` 是秒级，多次 marker 写入可能落在同一秒
- 用 `since_id` / `until_id` 边界天然鲁棒
- `id` 是 `INTEGER PRIMARY KEY AUTOINCREMENT`，单调递增，不会冲突

### 决策 4：Authorizer 集中授权

- 之前 6 个 service 各有 `_check_permission` 副本（重复实现）
- 现在统一在 `application/authz.py::Authorizer`：
  - `require_persona(user_id, persona_id)`
  - `require_note_owner(note_id, user_id)`
- 构造时只需注入 `UserRepo` + `NoteRepo`，不依赖 services

### 决策 5：FTS5 query 自动包裹双引号

- 之前用户输入含中文标点（如"，"）时 FTS5 解析报错 `near "/"`
- 解决：在 `note_repo.search` 中用 `_escape_fts5_query` 把 query 包成字面字符串 `"..."`
- 失败回退到 LIKE 兜底（不阻断调用方）

### 决策 6：时间戳统一转本地时区显示

- SQLite `CURRENT_TIMESTAMP` 返回 UTC
- Prompt 渲染时统一用 `_utc_ts_to_local(ts, tz_offset_hours)` 转成本地
- 用户在 UTC+8 就看到 UTC+8 时间，LLM 不会混淆"现在"和"上次对话"

### 决策 7：DB 路径默认 `./memory/companion.db`

- 集中存放运行时产物（DB、缓存）
- `.gitignore` 已配，不会污染仓库
- 自动 `mkdir -p`，第一次跑 setup 就建好

### 决策 8：项目通过 `pip install -e .` editable 安装

- 注册 `companion-cli` 入口点
- 不用每次 `python -m companion_ever.cli ...`
- `pyproject.toml` 完整声明依赖和包结构

---

## 5. 数据契约（5 张 SQLite 表）

```sql
-- 用户表：persona_ids 是 CSV
user(id PK, persona_ids TEXT)

-- Wiki 笔记表：14 种 type + FTS5 全文检索
note(id PK, user_id FK, persona_id, type, content, importance, decay_score, is_archived, source_quote, created_at, updated_at, last_accessed_at)

-- 笔记关系图（预留，当前未使用）
note_link(id PK, from_id FK, to_id FK, link_type, strength, created_at)

-- Memory Block：3 种 type × 每个 (user, persona)
memory_block(id PK, user_id FK, persona_id, block_type, fields_json, updated_at)

-- 对话原文：含 session_close sentinel
conversation_log(id AUTO PK, user_id FK, persona_id, turn_index, role, content, created_at)
  role ∈ ('user', 'assistant', 'session_close')

-- FTS5 虚拟表：trigram tokenizer
note_fts(note_id, content, user_id, persona_id, is_archived) tokenize='trigram'
```

### 关键字段

| 表 | 字段 | 业务语义 |
|---|---|---|
| `note` | `type` | 14 种：9 用户侧（profile_fact / event / preference / promise / milestone / inside_joke / callback / sensitive / observation）+ 5 人设侧 |
| `note` | `importance` | [0, 1]，检索 / 排序权重 |
| `memory_block` | `block_type` | 3 种：persona_runtime / user_profile / relationship_state |
| `memory_block` | `fields_json` | JSON dict（merge 语义，不是替换）|
| `conversation_log` | `role='session_close'` | sentinel，标识 session 边界 |

---

## 6. 关键技术栈

| 层 | 技术 |
|---|---|
| 编排 | LangGraph `StateGraph`（chat_workflow + extraction_workflow）|
| 数据模型 | `@dataclass`（User / Note / Block / ConversationMessage / PersonaBasics）+ `pydantic.BaseModel`（LLM 输入输出 schema）|
| 存储 | SQLite + FTS5 trigram tokenizer + LIKE fallback |
| LLM | OpenAI 兼容协议（OpenAI / DeepSeek / Qwen / 自建等） |
| 提示词 | 纯文本 `.txt` 文件 + `importlib.resources` 加载 |
| 配置 | `.env` 文件 + `python-dotenv`（软依赖，未装也能 fallback 到 os.environ）|
| CLI | argparse + companion-cli 入口点（注册在 pyproject.toml）|
| 测试 | pytest 风格（直接 `python tests/xxx.py`）|
| 打包 | `pyproject.toml` + `setuptools`（editable install）|

---

## 7. 关键算法

### 7.1 Session 边界检测（extraction 用）

```python
# 找最近 2 个 session_close marker，按 id 范围取对话
markers = conv.get_last_n_session_closes(user_id, persona_id, n=2)
last_marker = markers[-1]                              # 最新 marker
prev_marker = markers[-2] if len(markers) >= 2 else None  # 上一 marker

# id 边界（不用时间戳，因为同秒可能 tie）
since_id = prev_marker.id if prev_marker else None
until_id = last_marker.id

messages = conv.get_messages_between(
    user_id, persona_id,
    since_id=since_id, until_id=until_id, limit=1000,
)
```

### 7.2 Prompt 时间戳本地化 ⭐⭐⭐

```python
def _utc_ts_to_local(ts_str, tz_offset_hours):
    """SQLite UTC 时间字符串 → 用户本地时区。"""
    dt_utc = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    local_dt = dt_utc.astimezone(timezone(timedelta(hours=tz_offset_hours)))
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")
```

**为什么关键**：之前 LLM 看到 11:38（UTC）但用户是 UTC+8 → LLM 以为自己也是 UTC，导致 "我发这条消息的时间"答错。

### 7.3 Chat LLM 的 prompt 渲染（system 5 段 + 独立历史 role 消息）⭐⭐⭐

**system prompt（5 段）**：
```
## 当前时间信息
- **现在（这是用户发本条消息的时间）**：2026-09-29 周二 19:48 (UTC+8)
- 用户上次对话的起始时间（注意：这不是当前消息时间，是历史）：2026-09-29 19:38

## 人设基础
```json
{"name": "柳如烟", "personality": ["温柔","热情"]}
```

## 当前状态
### 助手运行时状态
```json
{"current_topic": "秋招焦虑"}
```
### 用户画像
```json
{}
```
### 关系进展
```json
{}
```

## 相关记忆
- (profile_fact, importance=0.95) 用户叫周艺科

## 行为指引
- 你必须严格按 persona_basics 中定义的角色与用户互动
...
```

**messages 列表（历史以原生 role 消息出现，不在 system prompt 里）**：
```python
[
  {"role": "system", "content": "<上面 5 段>"},
  {"role": "user",      "content": "[2026-09-29 19:30:00] 你好，我叫周艺科"},
  {"role": "assistant", "content": "[2026-09-29 19:30:05] 你好呀周艺科~"},
  {"role": "user",      "content": "[2026-09-29 19:31:00] 我今天有点累"},
  {"role": "assistant", "content": "[2026-09-29 19:31:05] 那就早点休息~"},
  {"role": "user",      "content": "<当前 user_input>"},
]
```

**踩过的坑 ⭐⭐⭐**：早期版本同时做了两件事——① 把历史渲染进 system prompt 的
`## 对话历史` 文本段 ② 又把历史作为 role 消息追加到 messages。
结果**每条历史被送给 LLM 两次**（浪费一半 token，且 LLM 可能误解为"对话重复了"）。
**修复**：历史只以原生 role 消息形式出现，system prompt 不再渲染历史段。
回归测试见 `tests/test_prompt_no_duplicate.py`。

**为什么用原生 role 消息而不是文本段**：
- 模型的 chat template 原生支持多轮，天然知道"谁说了什么"
- 对角色扮演 companion 尤其重要——LLM 能明确区分"用户说的"和"自己之前说的"
- 文本段是无 multi-turn 支持时的 workaround，我们支持

### 7.4 Marker 的 turn_index 自动分配

```python
def _next_marker_turn_index(self, user_id, persona_id):
    """session_close marker 的 turn_index = max(all_known) + 1。"""
    recent = conv_repo.get_recent_messages(...)      # 最新 real turn
    markers = conv_repo.get_last_n_session_closes(n=1)  # 最新 marker
    all_known = recent + markers
    return max(m.turn_index for m in all_known) + 1 if all_known else 0
```

让 real turns 保持 `1, 2, 3...` 单调连续；session_close markers "夹" 在其中但不污染编号。

### 7.5 Persona Template 自动替换用户名 ⭐

```python
# hot_girlfriend 模板里 `background` 写死"男朋友是周艺科"
# CLI setup 时自动替换为实际 user_id
def _substitute_user_in_template(fields, user_id):
    substitutions = {
        "周艺科(用户)": f"{user_id}(用户)",
        "周艺科": user_id,
    }
    # 递归替换 dict / list / str
    ...
```

避免 LLM 把 user 当成固定角色（即使是模板）。

---

## 8. 完整调用链（一次对话）

```
[CLI] companion-cli chat --user yike --persona 柳如烟
    ↓
[chat_workflow] graph.invoke({"user_id": "yike", "persona_id": "柳如烟", "user_input": "..."})
    ↓
chat_node:
    1. prompt_service.build(query=user_input)
       ├─ _get_recent_messages(limit=50)  ← 跨 session 历史
       ├─ block_service.get_blocks()       ← 当前 3 个 Block
       ├─ note_service (persona_basics)    ← 人设 JSON
       ├─ retrieval_service.retrieve()     ← FTS5 召回
       └─ _render_system_prompt()           ← 5 段拼装（不含历史）
    2. messages = [system, *history, user_input]
    3. llm.chat(messages)                  ← Chat LLM
    4. conversation_service.log_turn() × 2 ← user + assistant
    ↓
[should_close 路由]
    ├─ user_input != "/quit" → END
    └─ user_input == "/quit" → close_node
         ↓
[close_node]
    └─ SessionCloseService.close_session()
         ├─ log_session_close()            ← 写 sentinel marker
         └─ extraction_workflow.invoke()
              ├─ load: get_last_n_session_closes(n=2) + get_messages_between(id range)
              ├─ extract: llm.chat(EXTRACTION_SYSTEM + 对话) → facts + blocks
              └─ persist:
                  ├─ note_service.create_note × N (per-fact 容错)
                  └─ block_service.update_block_fields × N (merge 语义)
```

---

## 9. 数据流（按时间线）

### 单 session 内
```
user msg1 → chat_workflow → Chat LLM → assistant reply1
   ↓ log_turn(user) + log_turn(assistant)
user msg2 → chat_workflow → Chat LLM（看到 msg1 + reply1）→ reply2
   ↓ log_turn × 2
...
user msgN → ... → replyN
/quit → log_session_close → extraction_workflow
   ├─ load: 读 [Session 开始, marker] = msg1..msgN + replies
   ├─ extract: LLM 输出 facts + blocks
   └─ persist: 写 notes / 更新 blocks
```

### 跨 session
```
Session 1 (上面)
Session 2 开始:
  user msg1' → chat_workflow
    prompt.build:
      ├─ persona_basics (从 note 表，PERSONA_BASICS 类型)
      ├─ 3 个 Block（session 1 的 extraction 更新过）
      ├─ history: session 1 的所有 msg + replies（cap 在 50 条）
      ├─ retrieval: 召回 session 1 的 notes（profile_fact 等）
      └─ footer
  → Chat LLM 看到完整上下文 → reply
```

---

## 10. 关键文件位置

| 文件 | 作用 |
|---|---|
| `src/companion_ever/app.py` | 组合根 `build_app()`，所有依赖按拓扑序构造 |
| `src/companion_ever/config.py` | AppConfig + LLMConfig + RetrievalConfig + `.env` 自动加载 |
| `src/companion_ever/domain/` | 纯数据：enums / entities（@dataclass）/ persona_templates |
| `src/companion_ever/infrastructure/persistence/` | SQLite + repos（4 个 repo）+ db + mappers |
| `src/companion_ever/infrastructure/ai/` | LLM 客户端协议 + OpenAI 兼容实现 |
| `src/companion_ever/application/authz.py` | Authorizer（集中授权）|
| `src/companion_ever/application/llm_json.py` | `strip_thinking` + `clean_response`（LLM 响应清理）|
| `src/companion_ever/application/chat/` | chat / conversation / prompt / retrieval / extraction / session_close services |
| `src/companion_ever/application/persona/` | persona / persona_edit / block services |
| `src/companion_ever/application/memory/` | note service |
| `src/companion_ever/workflows/chat_workflow.py` | LangGraph StateGraph：chat + close |
| `src/companion_ever/workflows/extraction_workflow.py` | LangGraph StateGraph：load + extract + persist |
| `src/companion_ever/prompts/*.txt` | LLM 提示词（外部化）|
| `src/companion_ever/cli/__init__.py` | companion-cli 入口 |
| `setup.sh` / `setup.bat` / `setup.ps1` | 一键安装（venv + deps + editable install + .env）|
| `pyproject.toml` | 项目元数据 + 依赖 + 入口点 |
| `.env.example` | 环境变量模板（真实 .env gitignored）|
| `TESTING.md` | 全链路测试指南 |

---

## 11. 高频面试问答 ⭐⭐⭐

### Q1: 一句话介绍项目

> 基于 SQLite + LangGraph 的对话式长期记忆系统。Chat LLM 只管对话（拼 prompt + 调 LLM + 写 conversation_log），Extraction LLM 只管记忆（session 关闭时从对话抽取 facts + 更新 3 个 Block）。两个 LLM 靠 conversation_log 中的 `session_close` sentinel marker 衔接。

### Q2: 为什么用 LangGraph

> 3 个原因：①状态集中（TypedDict state schema）②可视化（LangGraph Studio / Mermaid 图）③可扩展（每个节点是独立函数，加 checkpoint 容易）。如果不用 LangGraph，手写 if-else 状态机会在节点超过 5 个后失控。

### Q3: 为什么 Chat LLM 和 Extraction LLM 分离

> 3 个原因：①**职责清晰**——一个负责对话、一个负责记忆 ②**prompt 干净**——Chat LLM 不会被"请同时抽取 facts"的指令污染 ③**触发时机不同**——Chat LLM 每轮都跑，Extraction 只在 session 关闭跑。

### Q4: session_close sentinel 是什么

> 在 `conversation_log` 表里写一行 `role='session_close'` 的特殊行，作为 session 边界。**不需要新表 / 新字段**。Extraction LLM 读到 marker 后，按 `[prev_marker.id+1, last_marker.id]` 范围取对话进行抽取。

### Q5: 为什么用 id 边界而非时间戳边界

> SQLite `CURRENT_TIMESTAMP` 是秒级。**同一秒内连续触发 /quit + extraction，marker 时间戳可能相同**，按时间戳过滤会丢消息。`id` 是 `INTEGER PRIMARY KEY AUTOINCREMENT`，单调递增，绝不冲突。**id 边界天然鲁棒**。

### Q6: FTS5 中文怎么处理

> 用 `tokenize='trigram'`（三字符片段）匹配中文。但 FTS5 query 语法对特殊字符敏感（如 `,` `*`），所以用 `_escape_fts5_query` 把整个 query 包成双引号 `"..."` 当字面字符串处理。失败时回退到 LIKE 兜底，不阻断调用方。

### Q7: prompt 里为什么要本地化时间戳

> SQLite `CURRENT_TIMESTAMP` 返回 UTC。如果直接展示 UTC 时间给 LLM，**用户和 LLM 看到的"现在"会差 8 小时**（中国时区）。`tz_offset_hours` 配置项控制本地时区，渲染 prompt 时统一把 UTC 转成本地。

### Q8: 为什么 persona_basics 模板里要替换用户名

> `hot_girlfriend` 模板的 `background` 写死"男朋友是周艺科"。如果实际 user 是 `yike`，LLM 顺着模板会把 user 当成"周艺科"——**人设错误传递**。CLI 在 setup 时递归替换模板里的"周艺科"为实际 user_id。

### Q9: 为什么不把 history 也通过 retrieval 召回

> 之前的设计就是这样（session 边界截断 + retrieval 召回）。**问题**：extraction 抽不到的东西 = 永久丢失。**现在的设计**：chat LLM 直接看到全部历史（cap 在 50 条），retrieval 是补充。这样即使 extraction 漏了某件事，LLM 仍然能从历史里看到。

### Q10: 记忆系统的边界是什么

> 三层边界：①session 内靠 conversation_log（无限长度）②session 间靠 notes/blocks（compact 后存储）③长期靠 retrieval（按需召回）。**不在范围内**：跨用户的共享记忆（每个 user_id 隔离）、多模态记忆（图片/语音，当前只支持文本）。

### Q11: 项目里最难的部分

> ①**Sentinel marker 状态机设计**——既要承担 Extraction 边界，又要避免污染 Chat 历史。②**LLM 提示词的细节**——比如"现在"vs"上次对话"混了导致 LLM 答错用户问题。③**FTS5 中文标点**——query 解析报错后用字面字符串 + LIKE 兜底。

### Q12: 项目 trade-off

> ①prompt 体积 vs 上下文完整度（cap 在 50 条）②extraction 抽取质量 vs 自动化（LLM 可能漏抽）③双 LLM 分离 vs 单 LLM 简单（前者架构更清晰，后者 prompt 更乱）④DB 字段 vs 表设计（CSV 存 persona_ids 简单但查询不优雅）⑤session_close 优雅 vs 复杂状态机（现在简单，但 50+ 条后 cap 是个隐患）。

### Q13: 如果让你重新设计会改什么

> ①**向量检索作为补充**（FTS5 对长尾 recall 不够好）②**自动 extraction 质量评估**（用一致性指标判断 extraction 是否成功）③**多 persona 切换**（一个 user 多个 persona，记忆按 persona 隔离但可共享基础偏好）④**流式 extraction**（长 session 不一次性抽取，而边聊边抽）⑤**Web 前端 + PWA**（已规划，让用户能在手机上用）。

### Q14: 为什么 conversation_log 表里 turn_index 是必须的

> UNIQUE 约束是 `(user_id, persona_id, turn_index, role)`。**user 和 assistant 在同一 turn_index 下可以共存**（同一轮对话的 user msg 和 assistant reply 用同一个 turn_index）。这让每轮对话天然成对，查询时 `turn_index` 即可定位一轮。

### Q15: session_close marker 用什么 turn_index

> 用 `max(all_known_turn_index) + 1`。包括 real turns + 历史 markers。这样 real turns 保持 `1, 2, 3, ...` 单调连续，marker 插入其中不污染编号。

### Q16: Block 更新为什么是 merge 而不是 replace

> Extraction LLM 输出"增量 fields"（比如 `{"current_topic": "秋招焦虑"}`），如果 replace 会丢掉其他字段。`update_block_fields` 是 merge 语义：`merged = {**existing, **new}`，保证不丢上下文。

### Q18: 怎么测试

> 4 个测试套件 + CLI 端到端：
> - `test_persona_service.py`：persona 创建 / 权限
> - `test_sentinel_flow.py`：marker 边界 + extraction 流程
> - `test_chat_workflow.py`：LangGraph 工作流
> - `test_prompt_no_duplicate.py`：回归测试——历史上下文不能重复送入
> - CLI：`companion-cli setup / chat / show` 直接用真实 LLM 跑

### Q19: 怎么保证历史上下文不会被重复送入 ⭐⭐⭐

> **曾经的真实 bug**：prompt_service.build() 同时做了两件事——① 把历史渲染进 system prompt 的 `## 对话历史` 文本段 ② 又把历史作为 role 消息追加到 messages 列表。结果每条历史被送给 LLM 两次，浪费一半 token，且 LLM 可能误解为"对话重复了"。
>
> **修复**：历史只以**原生 role 消息**形式出现在 messages 里，system prompt 不再渲染历史段。
>
> **为什么原生 role 消息更好**：
> - 模型的 chat template 原生支持多轮，天然知道"谁说了什么"
> - 对角色扮演 companion 尤其重要——LLM 能明确区分"用户说的"和"自己之前说的"
> - 文本段是无 multi-turn 支持时的 workaround，我们支持就不该用
>
> **回归防护**：`tests/test_prompt_no_duplicate.py` 断言 ① system_prompt 不含任何历史内容 ② messages 里每条历史只出现一次 ③ system prompt 仍含其余 5 段。
>
> **教训**：prompt 组装要单一职责原则——**同一段数据只能有一个出处**。

---

## 12. 速记数字 ⭐

| 数字 | 含义 |
|---|---|
| 2 个 LLM | Chat LLM + Extraction LLM（职责分离）|
| 5 张表 | user / note / note_link / memory_block / conversation_log |
| 14 种 note.type | 9 用户侧 + 5 人设侧 |
| 3 种 block_type | persona_runtime / user_profile / relationship_state |
| 3 个角色 | 'user' / 'assistant' / 'session_close'（含 sentinel）|
| 5 段 prompt | 时间 / 人设 / 当前状态 / 相关记忆 / 行为指引（历史单独走 role 消息）|
| 50 | `history_max_messages` 默认 cap（chat LLM 历史条数）|
| 20 | `recent_message_limit` 默认（其他用途）|
| 10 | `retrieval.top_k` 默认 |
| 5 | `extraction blocks_update` 维度权重（weights dict）|
| 8 | UTC+8（中国时区默认）|

---

## 13. 自检清单（模拟面试前最后一遍）

- [ ] 能 30 秒讲清楚"双 LLM + Sentinel"的核心架构
- [ ] 能讲清楚 session_close sentinel 的三层语义（marker / extraction 边界 / 状态机）
- [ ] 能讲清楚 Chat LLM 5 段 system prompt 的每一段是什么（+ 历史走独立 role 消息）
- [ ] 能讲清楚"历史只送一次"这个坑（system prompt 不渲染历史段）
- [ ] 能讲清楚为什么用 id 边界而非时间戳
- [ ] 能讲清楚 FTS5 trigram + LIKE 兜底
- [ ] 能讲清楚时间戳本地化的原因
- [ ] 能讲清楚 Authorizer 集中授权（消灭 6 处重复）
- [ ] 能讲清楚 extraction_workflow 的 3 个节点
- [ ] 能讲清楚跨 session 记忆的两种路径（直接 history + retrieval）
- [ ] 能讲清楚 .env → pyproject.toml → setup.sh 的工程化链条
- [ ] 数字能脱口而出（5 张表 / 14 种类型 / 3 种 block / 50 条 cap）
- [ ] 知道项目边界（已实现 vs 计划实现：Web 前端）

---

## 14. 工程化清单 ⭐

| 项 | 实现状态 |
|---|---|
| pyproject.toml | ✅ 已写，依赖 + 入口点 + 包结构 |
| requirements.txt | ✅ 已写（openai / pydantic / langgraph / python-dotenv）|
| .env.example | ✅ 已写（OPENAI_API_KEY / BASE_URL / COMPANION_LLM_MODEL）|
| .gitignore | ✅ 已写（.env / .venv / __pycache__ / *.db）|
| setup.sh / setup.bat / setup.ps1 | ✅ 三平台一键安装（venv + editable install）|
| companion-cli 入口点 | ✅ 已注册，可直接调 `companion-cli setup / chat / show` |
| README.md | ✅ 项目说明 + 安装 + 快速上手 |
| TESTING.md | ✅ 全链路测试指南 |
| PROJECT_KB.md | ✅ 本文（项目知识库，可发给 LLM）|

---

## 15. 未来发展

| 方向 | 优先级 | 描述 |
|---|---|---|
| Web + PWA 前端 | ⭐⭐⭐ | FastAPI + WebSocket 流式 + 单文件 HTML |
| 向量检索 | ⭐⭐ | 补充 FTS5（用 ChromaDB / sqlite-vss）|
| 流式 extraction | ⭐ | 长 session 边聊边抽 |
| 多 persona 切换 | ⭐ | 一个 user 多个角色，共享基础偏好 |
| 自动 quality check | ⭐ | extraction 一致性评估 |