# START_HERE — 隔一段时间后回来，先看这个

> 最后一次代码改动：2026-09-29。核心功能全部完成且可运行，**只差前端**。
> 本文件于 2026-10-27 补写（用于快速恢复上下文）。

---

## 1. 一句话现状

**后端已经完整跑通了**：双 LLM 分离 + Session Sentinel + LangGraph 工作流 + FTS5 检索 + 3 个 Memory Block。
真实数据已经在库里（user `yike` / persona `柳如烟` / 33 条记忆 / 5 次会话）。
**没有任何 HTTP 层**——所以现在只能在终端里聊天，手机上用不了。

---

## 2. 怎么启动（重要）

### 环境

```powershell
# 一次性初始化（venv + 依赖 + .env）
.\setup.ps1
```

> 已装过就不用再跑。`.venv` 里已有 Python 3.13 + openai + langgraph。

### 三种等价启动方式（现在都能用了）

```powershell
# 方式 A：绝对省事，推荐
.\.venv\Scripts\companion-cli.exe show --user yike --persona 柳如烟

# 方式 B：包入口
.\.venv\Scripts\python.exe -m companion_ever.cli show --user yike --persona 柳如烟

# 方式 C：顶层入口
.\.venv\Scripts\python.exe -m companion_ever show --user yike --persona 柳如烟
```

> 2026-10-27 修复：之前 `python -m companion_ever.cli` 会报
> `No module named companion_ever.cli.__main__`（cli 是包但缺 `__main__.py`）。
> 已补 `cli/__main__.py` + `companion_ever/__main__.py`。

### 三个子命令

```powershell
# 1) 建用户 + 角色
companion-cli setup --user alice --persona 舒舒 --template hot_girlfriend

# 2) 开始对话（输入 /quit 触发 session close + 记忆抽取）
companion-cli chat --user yike --persona 柳如烟

# 3) 查看状态（记忆 / 关系状态 / 对话统计）
companion-cli show --user yike --persona 柳如烟
```

### 现有角色清单

```powershell
companion-cli show --user yike
# → User 'yike' owns 1 persona(s): ['柳如烟']
```

### 跑测试

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe tests/test_persona_service.py   # persona 创建 / 权限
.\.venv\Scripts\python.exe tests/test_sentinel_flow.py      # session 边界 / 抽取流程
.\.venv\Scripts\python.exe tests/test_chat_workflow.py     # LangGraph 工作流
```

> 三个都是手写 assert 脚本（不是 pytest，pytest 没装）。当前全绿。

---

## 3. 数据在哪

| 东西 | 位置 |
|---|---|
| 数据库 | `memory/companion.db`（SQLite，gitignored，**别删**） |
| 配置 | `.env`（API Key / base_url / 模型名，gitignored） |
| 模型 | `MiniMax-M3` @ `https://api.MiniMax.chat/v1` |

表结构 5 张：`user` / `note` / `note_link` / `memory_block` / `conversation_log`（+ `note_fts` 虚表）。

---

## 4. 已知未完成项（2026-10-27 盘点）

| 项 | 状态 | 说明 |
|---|---|---|
| **Web 前端 / PWA** | ❌ 完全没写 | 项目里 0 行 HTTP 代码。这是"手机上和 AI 伴侣对话"目标的唯一拦路虎 |
| **`note_link`（笔记关系图）** | ❌ 表建了，代码 0 引用 | schema 里注释写"wiki 的核心差异化能力"，但 `note_link` / `link_type` / `strength` 在 src 里搜不到任何使用 |
| **`decay_score`** | ❌ 永远是 1.0 | schema/entity 有字段，没有任何代码更新它；检索排序用的是 `_recency_score` 的硬编码分段（1/7/30/90 天） |
| **`last_accessed_at`** | ❌ 永远 NULL | 同上，没人写 |
| **`is_archived`** | ⚠️ 只能手动置 | 没有自动归档策略，note 表会无限膨胀 |
| **重复 note** | ⚠️ 无去重 | `extraction_service` 注释里自己写了"无 UNIQUE 约束，多次抽取会产生重复条目" |
| **git 版本控制** | ✅ 已补 | 2026-10-27 `git init` + 首次快照（commit `098db82`） |
| **`python -m companion_ever.cli`** | ✅ 已修 | 补 `__main__.py` |

---

## 4.5 trace 诊断系统（2026-10-27 新增）⭐

**回答一个问题：这一轮模型究竟看到了什么、为什么这么答、花了多少。**

### 用法

```powershell
# 1) 列出最近的 trace（耗时、输入字符数一眼看到）
companion-cli trace list --user yike --persona 柳如烟 --last 10

# 2) 看某一轮的完整链路
companion-cli trace show <trace_id>

# 3) 看模型实际收到的完整请求（system prompt + 全部历史 + 召回的记忆）
companion-cli trace show <trace_id> --request

# 4) 看模型实际返回的内容
companion-cli trace show <trace_id> --response

# 5) 清理 7 天前的 trace
companion-cli trace prune --days 7
```

### 存储位置

```
memory/traces/2026-10-27/
  ├──<trace_id>.json          # 元数据 + 链路步骤
  ├── <trace_id>.request.txt   # 实际发给 LLM 的完整请求  ← "模型究竟看到了什么"
  └── <trace_id>.response.txt  # 实际收到的回复
```

### 开关

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `COMPANION_TRACE` | `1`（开） | 关掉就完全不写 trace |
| `COMPANION_TRACE_STRICT` | `0`（关） | 开启后不变量违规直接抛异常（开发期用）|
| `COMPANION_PROMPT_BUDGET_CHARS` | `40000` | prompt 字符预算，超了报违规 |

### 两种 trace

| kind | 什么时候产生 | 关键步骤 |
|---|---|---|
| `chat` | 每轮对话 | `retrieval.retrieve` → `prompt.build` → `invariants.check` → `llm.chat` → `db.log_turn` |
| `extraction` | `/quit` 触发抽取 | `db.log_session_close` → `llm.chat`（抽取）→ `extraction.workflow` |

### 内置的廉价不变量（`application/invariants.py`）

trace 里 `invariants.check` 这一步会自动检查，违规会在 `trace show` 里高亮：

| 规则 | 抓什么 |
|---|---|
| `history_not_duplicated` | 历史被送了两遍 |
| `history_is_recent` | 历史取到了**最早**的 N 条而不是最近的 N 条 |
| `prompt_within_budget` | prompt 超过预算 |
| `retrieval_not_empty` | 有记忆却一条都召不到（检索静默失效）|
| `facts_not_exploded` | LLM 抽取数量爆炸（幻觉）|
| `notes_unique` | 写入了重复 note |

### 它已经抓到的真实 bug

见下面第 4 节 —— 这三个 bug 都是 trace 上线后立刻暴露的，
**日志系统抓不到，只有"看实际发了什么"才能发现**。

## 4.6 已修复的严重 bug（2026-10-27）

trace 系统上线后立刻暴露了三个"看不见"的 bug，已全部修完并加上回归测试（`tests/test_bugfixes.py`）：

| # | 问题 | 修法 | 实测影响 |
|---|---|---|---|
| 1 | `get_messages_between` 无边界时 `ORDER BY id ASC` → prompt 装的是**最早**的 50 条，模型完全看不到刚聊了什么 | 改为 `ORDER BY id DESC` 取最近 N 条再反转 | 上下文里的对话从"一个月前"变成"刚刚" |
| 2 | `/quit` 会**先跑一次完整 LLM 调用**，还把 "/quit" 和模型对它的回答写进日志 | 路由挪到 chat 节点**之前**，`/quit` 直接进 close | 每次退出省一次完整推理调用（约 8.3K token） |
| 3 | 检索的 FTS5 和 LIKE 两条路径都要求**整句原样出现** → 自然语言输入必然 0 命中 | 拆成 2/3 字关键词后 **OR** 组合（`utils/text.py`），精度由"命中关键词数"打分 | `fts_hits` 从 0 变成有效命中 |
| 4（顺带） | assistant 消息存了原始 `<think>` 块（占历史 92%） | 写入时 strip；存量用 `maintain strip-think` 清 | prompt 从 **60,921 → 7,828 字符**（-87%），token_in 8,306 → 5,040（-39%） |

### 数据维护命令

```powershell
# 剥掉历史 assistant 消息里的 <think> 块（推理模型的思考过程）
companion-cli maintain strip-think
```

已执行过：68 条消息，110,344 → 14,233 字符。数据库备份在 `memory/companion.db.bak-*`。

## 4.7 历史污染清理（2026-10-27）

### 为什么要清

conversation_log 有**两个职责**，同一个字段同时服务于两个相反的目的：

| 用途 | 适合保留 think | 事实 |
|---|---|---|
| 1. 存档（给人看） | 有意思，能看到她在想什么 | 保留也行 |
| 2. **最近 50 条送进 prompt**（给模型看） | 纯污染，占历史 92% token | 必须剔掉 |

同一个字段同时服务两个用途，就会出现"对存档友好、对模型有害"的内容。
现在写入路径已经 strip 掉 think 块（存档会少一点，但模型受益更大），
以下命令用于清理历史存量。

### 命令

```powershell
# 清理 conversation_log：删 /quit 轮次 + 剔模仿旧格式的前缀
companion-cli maintain clean-history [--dry-run]

# 清理 note 表里由 /quit 派生的幻觉记忆
companion-cli maintain clean-quit-notes [--dry-run]

# 剔掉 assistant 消息里的 <think> 块
companion-cli maintain strip-think

# 做一份完整备份
companion-cli maintain backup
```

全部支持 `--dry-run`（只报告会改什么，不实际改动）。
`clean-history` / `clean-quit-notes` 在真正改动前会**自动先备份**。

### 已清理的内容（2026-10-27 实际执行）

| 项 | 数量 | 说明 |
|---|---|---|
| `/quit` 轮次 | 9 轮 / 18 行 | 假 user 消息 + 模型对它生成的告别回复 |
| 模仿旧格式的前缀 | 21 行 | assistant 回复开头自己吐出了 `[2026-09-29 11:14:12] assistant:` |
| think 块 | 68 条 | assistant 消息（上一轮已清） |
| note 表 /quit 幻觉 | 4 条删 + 1 条修剪 | 例如"用户习惯在暧昧氛围升温时突然用 /quit 结束对话"（纯编的） |

清理后：`/quit` 行 0、think 块 0、格式前缀 0；
`session_close` 哨兵 10 个完好保留；user / assistant 仍然严格配对（64 / 64）。

### ⚠ 关于备份的一个坑

数据库是 **WAL 模式**，最近写入的数据还躺在 `companion.db-wal` 里，没 checkpoint 到主库文件。

```powershell
Copy-Item memory/companion.db memory/backup.db     # ⚠ 会漏数据（实测漏了 4 行）
```

一定用 `companion-cli maintain backup`（内部用 `VACUUM INTO`，会把 WAL 内容一起写进新文件）。

## 4.8 防污染的写入路径（2026-10-28）

上面的清理只是治标，真正的问题在**写入路径**。

### 模型会学你的 prompt 格式

prompt 里历史消息的格式是 `[时间戳] 内容`，模型看了几百轮之后学会了它，
开始在**自己的回复里也加上前缀**：

```
模型输出： [2026-10-28 00:01:28] 怎么突然问这个啊～当然想啦｜
入库后：   怎么突然问这个啊～当然想啦｜          ← 前缀被剔掉
```

所以 assistant 回复入库前会过两道清理（`chat_service` / `chat_workflow` 的
`_clean_assistant_reply`）：

1. 剔 `<think>...</think>` 块（推理模型的思考过程）
2. 剔模仿旧格式的 `[时间戳] assistant: ` 前缀

实现在 `utils/text.py::strip_format_prefix`（写入路径和维护命令共用）。

### 抽取失败时回滚 marker（防止记忆静默丢失）

抽取的范围是 `[上一个 marker, 本次 marker]`。如果抽取失败（LLM 超时/网络问题）
但 marker 已经写入了，下次抽取只会覆盖 `[本 marker, 下个 marker]`——
**那段内容就永远不会被抽取了，记忆静默丢数据**。

现在的处理：

- LLM 调用失败→ 记录错误（不再把异常抛给用户）
- 回滚 marker（删掉那行）→ 下次抽取的范囸4自动包含它
- CLI 显示 `[记忆抽取失败（会话边界已回滚，下次会自动补抽）]`

### 已实测的一次真实超时

```
状态: error
错误: APITimeoutError: Request timed out.
耗时: 181532 ms
```

耗时 180 秒 = `OPENAI_TIMEOUT` 60s × openai SDK 默认重试 2 次。
**抽取这一步以后应该异步化**（用户不应该等它）。

## 4.9 思考过程分表存储（2026-10-28）

### 核心问题

conversation_log 的内容要**同时满足两个相反的需求**：

| 用途 | 对 think 的态度 | 为什么 |
|---|---|---|
| 送进 prompt（模型看） | **绝对不能要** | 占历史 token 的 90%+，还污染模型上下文 |
| 展示给用户 | **很有价值** | 能看到"她在想什么"，是体验感的一部分 |

一份数据两种用途 → 必须分开存。

### 实现：一张表 + 一张薄表（不是两张完整表）

```
conversation_log   —— 只存干净正文  → 送 prompt + 存档
thinking_log       —— 只存思考过程  → 只给用户看
                     通过 message_id 关联，不重复存 user_id/persona_id/turn_index/created_at
user_settings      —— 存展示偏好（show_thinking）
```

为什么不用第二张完整的 conversation_log：
- 不需要维护两套 UNIQUE 约束
- 不会两路写入失步
- **隐私：一条 SQL 清空所有思考记录**

### 规则：数据两边都存，设置只决定"给不给你看"”

这样用户任何时候切换开关都不会丢历史。

### 接口

```bash
# 读设置（默认 show_thinking=true）
GET /api/settings/{user}/{persona}

# 改设置
PUT /api/settings/{user}/{persona}  {"show_thinking": false}

# 读历史（思考过程默认跟随设置）
GET /api/persona/{user}/{persona}/history?limit=50
GET /api/persona/{user}/{persona}/history?include_thinking=1   # 强制包含
GET /api/persona/{user}/{persona}/history?include_thinking=0   # 强制不包含
```

### 命令

```powershell
# 从备份里恢复历史思考过程到 thinking_log
companion-cli maintain restore-thinking --source memory/companion.db.bak-20261027-232944 --user yike --persona 柳如烟
```

已执行：恢复 **59 条 / 88,528 字符**（跳过 9 条 = 已删的 /quit 轮次）。

### 测试

`tests/test_thinking_split.py` 守着三个约定：
1. conversation_log 里没有 think / 模仿前缀
2. thinking_log 里有思考过程
3. prompt 里绝对没有 think

## 4.10 换模型也不怕（2026-10-28）

思考处理做成了**模型无关**，换任何推理模型都不用改代码。

### 三种情况都正常工作

| 情况 | 处理 |
|---|---|
| **模型没有思考功能**（普通 GPT） | 正文原样入库，**不产生 thinking_log 记录**，prompt 正常 |
| **模型用不同标签**（`<thinking>` / `<reasoning>`） | 统一识别 think / thinking / reasoning 三种，都能剔掉 |
| **模型把思考放在独立字段**（`reasoning_content`） | 从字段里捕获，优先于从正文解析，存进 thinking_log |

测试：`tests/test_model_agnostic.py` 四个用例（无思考 / 别的标签 / reasoning_content / 零宽字符）。

### 关键原则：思考内容绝不能进 prompt

三条路径都只能写入 `thinking_log`，`conversation_log`（送 prompt 的那份）永远是干净的。
测试里有断言守着这条。

### 零宽字符（BOM）清理

不可见字符会进数据库、破坏检索、让模型的分词失效（例如
PowerShell 管道输入带来的 UTF-8 BOM）。

- 写入路径：`strip_invisible()` 清理网络传入的不可见字符
- 保留 ZWJ / ZWNJ（U+200D / U+200C）——它们在 emoji 序列里是有意义的
- 存量：`maintain clean-history` 会一并清掉

已清理：4 行。

## 4.11 时间信息段重构（2026-10-28）

### 事故

模型回复里出现了 `[2026-10-28 00:08:47] 早啊宝宝～...`，并且说出"现在还是 10月28日凌晨"（实际是上午 10:54）。

根因是两个问题串成一条链：

```
50/51 条历史消息都以 [时间戳] 开头        ← 格式太规整
        ↓
模型学会了"每条消息都要带 [ts]"       ← 模仿
        ↓
它照抄上一条消息的时间戳 00:08:47      ← 回答里的时间是错的
同时 "上次对话的起始时间：00:08:47"      → 模型当成了"现在"
```

### 修法（三处）

**1. 时间段重构** —— 删掉误导性那行，改成"现在 + 隔多久 + 这一轮怎么开场"

```
## 当前时间信息
- **现在**：2026-10-28 周三 12:11 (UTC+8)
- **用户上一次说话**：10月28日 11:00，距今 1 小时 9 分钟
- **这一轮**：用户今天之前来过，距上次说话过了 1 小时 9 分钟——
  还是同一天的事，可以紧凑一下但不必重新报到身份。
```

间隔分档：`<10 分钟` 连续 / `<3 小时` 尾声 / `<16 小时` 同一天 / 更长 新对话。

**2. assistant 消息不带时间戳**（实测最优）

用真实 API 实测四种格式：

| 格式 | 模型自己加时间戳的次数 |
|---|---|
| A 全部消息都带 `[绝对时间]`（原现状） | 1/2 |
| **B 只有 user 带时间戳** | **0/2** ✅ |
| C 中文括号 + 相对日期 | 1/2 |
| D `<at ...>` XML 风格 | 1/2 |

原因：模型是在**模仿规律**，只要所有消息都那么开头它就会学。
只留 user 带时间戳时，模型不会当这是"消息应该带的格式"，而且还省 token。

**3. 存档层剔前缀（兜底）**

CLI 打印前和 API 流式输出都剔掉开头的模仿前缀，即使模型偶尔模仿了用户也看不到。

### 实测效果

```
修前：「现在还是 **10月28日凌晨**」  +  回复前缀 [2026-10-28 00:08:47]
修后：「现在是 **12点11分，中午啦**」  +  无前缀
prompt 体积   7,815 → 7,012 字符
tokens_in      5,040 → 4,497
```

## 4.12 Block 重构：读-改-写 + persona_runtime 移出 prompt（2026-10-28）

### 事故

`user_profile` 每次抽取都被**整体重写**，旧的画像不断丢失：

```
00:08  preferences: 喜欢"宝宝""姐姐""爸爸"等称呼形成的互称梗   ← 学到了
12:35  preferences: 喜欢"mua"、亲亲等亲密互动                        ← 互称梗没了
```

**根因**：抽取 LLM 的 prompt 里**没有现有的 profile**（只有本次会话），
它只能从零重写；而应用层的 `merged = {**existing, **new}` 只在**字段级**生效，
**list 内部是整体替换**。所以“merge”是假的。

### 改法 1：读-改-写（主）

抽取时把现有的 Block 内容传给 LLM，让它在旧的基础上改：

```
## 现有状态（上次抽取的结果）
### 用户画像
```json
{...}
```
### 关系进展
```json
{...}
```
```

`extraction_system.txt` 里同时加上规则：仍然成立的保留 / 新确认的追加 / 过时的移除。

### 改法 2：应用层合并策略（兜底，不信任 LLM 的输出格式）

| 策略 | 规则 |
|---|---|
| 里程碑（`key_moments`） | 新旧**取并集**（每次只写本次会话）——“第一次”不会因为时间过去而变假 |
| 关系进度（`stage_progress`） | **只能递增**，不能倒退 |
| 所有 list | 去重 + 限长（personality 8 / preferences 8 / hint 6 / key_moments 12） |

### 改法 3：`persona_runtime` 移出对话 prompt

`persona_runtime` 是**会话级**状态（上次在聊什么、违开多久），它只在 /quit 时刷新，
必然滞后。注入 prompt 会让模型被过时的主题带跑（“不停问正式 offer”）。

**现在**：

- 对话 prompt 只注入 `user_profile` + `relationship_state`，段标题从 `## 当前状态` 改为 `## 你对用户的了解`
- `persona_runtime` 仍存在数据库（**4e0d丢数据**），留给未来的「主动打招呼」用
- 抽取时**不再把 persona_runtime 给 LLM 看**（里面的风格字段是自我强化的产物）
- 抽取输出里的 `persona_runtime` 只保留 `current_topic`（风格字段被应用层过滤）

### 自我强化回环也被切断

```
assistant 输出（爱撒娇、爱抱抱）
    ↓ 抽取 LLM 观察整段对话（含 assistant 的话）
总结成 "speech_style_reminder: 多使用撒娇、抱抴…"   ← 被应用层过滤掉
    ↓
下一轮 prompt 注入 → assistant 照做 → 下一次抽取再观察到更多 → 放大
```

现在三个环节都断了：不注入 prompt / 不给 LLM 看 / 应用层过滤。

## 4.13 会话边界自动管理（2026-10-28）

### 问题

`/quit` 是用户主动触发的。用户可能直接关掉 App、切后台、断网——
**session 边界就没了**。边界没了，摘要、抽取、上下文的边界全乱。

最冻的是：**用户离开后再也不回来** —— 那段对话的记忆永远不会被抽取。

### 解决：服务端自动管理边界

阈值：`COMPANION_IDLE_MINUTES`（默认 120 分钟）。
离上次说话超过阈值 → 上一个 session 算结束。

**三个触发点（都调同一个函数）**：

| 触发点 | 覆盖场景 | 实现 |
|---|---|---|
| 用户发下一条消息时 | 用户离开后回来了 | `ChatService` / `chat_workflow` 开头调 `close_if_stale` |
| 服务器定时任务（每 60 秒） | **用户再也不回来** | `api/server.py::_session_sweeper` |
| 进程启动时 | CLI / 服务器重启 | 上面两处的调用自然覆盖 |

**抽取在后台线程里跑**（`close_session_background`），不阻塞当前消息——
抽取是一次完整 LLM 调用（几十秒），不能让用户等它。

### 底层变化：check_same_thread 默认改 False

后台线程要用主线程建的 SQLite 连接。
SQLite 本身是 serialized 模式（`threadsafety=3`），跨线程安全。

### 新增接口

```python
# 判定并自动关闭（返回是否关了）
session_close_service.close_if_stale(user_id, persona_id, idle_minutes)

# 后台关闭（不阻塞）
session_close_service.close_session_background(user_id, persona_id)

# 列出所有未关闭的 session（定时扫描用）
conversation_service.list_active_sessions()
```

## 4.14 PWA 前端（2026-10-28）

### 怎么跑

```powershell
# 在计算机上
.\.venv\Scripts\companion-cli.exe serve

# 手机连同一个 WiFi，浏览器打开
#   http://<计算机局域网 IP>:8000
```

终端会打印可用的地址（含局域网 IP）。
默认监听 `0.0.0.0:8000`，手机能连。

### 界面

| 视图 | 内容 |
|---|---|
| **对话** | 流式逐字、思考过程可展开、自动重连 |
| **她记得的**（右上抽屉） | 关系阶段 + 进度条 + 里程碑 + 记忆条目（可按类型筛选） |
| **设置** | 显示她的思考过程开关（实时生效，不重载页面） |

### 文件位置

```
src/companion_ever/web/
  index.html            结构
  app.js                逻辑（WebSocket / 拉取 / 渲染）
  styles.css            样式
  manifest.webmanifest  PWA 清单
  sw.js                 Service Worker（离线壳 + 推送）
  icon-192.png / icon-512.png
```

放在 Python 包内所以 `pip install` 会一起装上（pyproject 的 `package-data`已配置）。
零构建步骤，改完刷新即生效。

### 定制点

- **HTTPS 才能完整 PWA**：Service Worker 需要安全上下文，
  `http://192.168.x.x:8000` 下不会注册 → 没有离线缓存、没有推送、Chrome 也不给「安装」按钮。
  解决：**Tailscale Serve**（免费，自动证书）或 Cloudflare Tunnel。
- **iOS 推送**需先「添加到主屏幕」（iOS 16.4+）。
- PIN 默认关闭（`COMPANION_WEB_PIN` 设了就需要输入）。

### 修复了的一个隐开 bug

`ChatService.stream_chat()` 的 assistant 写入路径漏了改，还在调用已删除的 `_clean_assistant_reply` ——
**API 路径只能流式输出、但写库时崩**（用户发送的消息没库存）。
现在两条路径已统一。

## 4.15 微信式历史浏览（2026-10-28）

### 核心思路

微信能“上翻无限、按日期、搜索”，是因为它的历史存在**手机本地**。
你的历史在**服务器上** —— 对“自托管单用户”这个场景，
服务器就等于“本地”。**能力一样，差的只是 API 设计。

### 三个能力的实现

| 微信的 | 本身质 | 实现 |
|---|---|---|
| 一直往上翻 | 游标分页 | `conversation_log.id` 就是天赌的游标 |
| 按日期检索 | 日期分组 + 跳转 | `created_at` 是字符串，`LIKE '2026-10-28%'` 即可 |
| 搜索聊天记录 | 全文检索 | 复用 `utils/text.py` 的关键词抽取（与记忆检索同一套） |

### 新接口

```
GET /api/persona/{u}/{p}/history?limit=&before_id=&after_id=&date=
    不传游标      -> 最新的 limit 条
    before_id=X                -> 紧跟着 X 上面的 limit 条（上翻）
    after_id=X                 -> 从 X 往后的 limit 条（搜索结果跳转）
    date=YYYY-MM-DD            -> 只看某天

GET /api/persona/{u}/{p}/dates       有对话的日期（降序）+ 每天消息数
GET /api/persona/{u}/{p}/search?q=&scope=all|messages|memories
```

`MessageOut` 增加了 `id`（前端要用它当游标）。

### 前端交互

- **消息流里按天插入日期分隔条**（会数据流里自动加）
- **滑到顶部自动加载更旧的**（加载时保持滚动位置，不会跳动）
- **顶栏搜索按钮** → 覆盖层搜索面板，分「聊天记录 / 她记得的」两个 tab
- **搜索结果点击 → 跳转到那条消息**（用 after_id 定位）
- **抽屉里的「按日期回溯」** → 点某一天 → 聊天区切到那天，顶部出现「正在查看历史 · 回到最新」
- 切换到历史视图时会停止自动上翻（避免误解为实时聊天）

### 为什么搜索不用 FTS5

中文可能只有 2 字（“面试”“阳台”），trigram 分词器要求 ≥3 字。
所以直接用 `LIKE` + 关键词 OR（如它在 `note_fts` 里那样）。
数据量大了（十万条以上）再换 FTS5 —— 当时再做，现在扫全表对个人应用可接受。

## 4.16 Web 端体验三件套（2026-10-28）

这一轮把手机端用起来的几个"别扭"的地方收拾了。

### 1. 打开页面自动停在最新消息

**症状**：进页面停在聊天记录顶部，不是最后一条。

**根因**：`styles.css` 里 `.chat { scroll-behavior: smooth; }`。
按 CSSOM-View 规范，`scrollTo({behavior: 'auto'})` 里的 `auto`
**不是"立即滚动"**，而是"由元素自己的 `scroll-behavior` 决定" ——
所以 `scrollBottom(true)` 的 `instant` 参数形同虚设，
打开页面时从顶部平滑滚几千像素，被 `syncViewport()` 改高度打断后停在中间。

**修法**：

- 删掉 CSS 里的 `scroll-behavior: smooth`，要动画的地方在 JS 里显式写 `behavior:'smooth'`
- `scrollBottom(instant)` 的立即分支改成 `box.scrollTop = box.scrollHeight`
- 新增 `anchorBottom()`：渲染后在两帧后再补两次滚（布局稳定后内容可能又撑高）
- `syncViewport()` 高度变化时重新贴底（键盘弹起/收起、app 高度重算）

回归测试：`tests/test_scroll_anchor.py`

### 2. 思考过程：开关打开就能看到，不用刷新

**症状**：点开"显示她的思考过程"后，当前这屏消息不出现折叠块，
必须关掉再打开（或刷新）才看得到。

**根因**：WebSocket 只推正文，**思考过程从来没发到前端**。
前端那条最新消息是本地拼的（`id: Date.now()`），压根没有 thinking 字段。
之前"切开关能看到"是因为切开关会触发 `loadHistory(true)` 重新拉历史 ——
拉到的是库里已经落库的 thinking。

**修法**：

- 服务端：`_stream_reply` 在流结束后用**入库时同一个** `split_assistant_reply()`
  算出思考过程，单独推一条 `{"type":"thinking"}` 事件
- 前端：收到 thinking 存进 `S.streamThinking`，`done` 时挂到那条消息上，
  并写进 `S.messages`（开关打开时立刻能显示）
- 前端：拉历史时**显式**传 `include_thinking=1/0`，不再依赖服务端自己的设置
- 前端：`boot()` 里先 `loadSettings()` 再 `loadHistory()`（原来并行，有竞态）
- 前端：切开关时重拉当前这页并**保持滚动位置**，不再把你甩到底部

验证：真实 MiniMax 下事件序列 = `start → delta×3 → thinking → done`，
thinking 807 字符，正文 109 字符且不含 think 标签。

### 3. 微信式「回到最新」悬浮按钮

- 用户往上翻历史时，右下角出现悬浮按钮
- 期间来了新消息 → 显示"N 条新消息"；自己滑回底部 → 角标清零
- **流式输出时不再把正在翻历史的人拽回底部**：
  以前每个 delta 都无条件 `scrollBottom()`，现在只在"用户本来就在底部"时才跟
- 切到历史视图（按日期/搜索跳转）时按钮自动隐藏

### 4. 开关位置挪到抽屉靠上

原来「显示她的思考过程」在「沉淀的 wiki」下面，几乎找不到。
现在放在「结束这次对话 · 让她记住」下面 —— 打开抽屉第一屏就能看到。

回归测试：`tests/test_web_chat_ux.py`（含 WebSocket 端到端）

## 4.17 时区 / 异步记录 / 未读角标（2026-10-28）

### 时区：DB 存 UTC，出库必须转本地（一个很隐蔽的 bug）

**症状**：资料页「上次记录」显示"8 小时前"（明明刚记过）；
聊天列表的时间也比手机上的钟慢 8 小时。

**根因**：

- SQLite 的 `CURRENT_TIMESTAMP` 返回 **UTC**
- 提示词那边早就用 `_utc_ts_to_local` 转成本地时区了 ✓
- **但 API 层漏了** ✗ —— 前端把 UTC 当本地显示，东八区所有时间都慢 8 小时

**修法**（约定：**DB 存 UTC，出库一律转本地**）：

- 新增 `utils/timeutil.py`：`utc_ts_to_local()` / `tz_modifier()`
- `api/routes/memory.py` 的 `_s(value, tz)` 统一转换
  （消息、笔记、搜索命中、汇总的 `last_conversation_at` / `last_record_at` 全都转）
- `list_dates` / `get_page(date=)` 改成按**本地日期**分组
  （以前 UTC 20:00 的消息在东八区是第二天凌晨 0 点，会被算到前一天）

> 这个 bug 隐蔽在哪：单条测试全绿、界面也"看起来正常"，
> 只有和真实时间对照才会发现差 8 小时。

### 记录（session close）改成异步

抽取是一次完整的 LLM 调用（几十秒）。以前 `/api/session/close` 同步等它，
用户在资料页点「记一次」就得干等着，也没法继续聊天。

现在：

- 后端 `close_session_background()` 丢后台线程，接口 **5ms** 返回
- 加了 `_closing` 守卫，重复点击不会跑两次抽取
- 前端点了就提示"开始记录，你可以继续聊"，然后轮询 `last_record_at`，
  变了就把「上次记录」更新成"刚刚"

### 未读角标

- 会话行的红点**只数她的回复**（一问一答，没分段发消息，未读就是"她回了几条"）
- 底部「消息」Tab 加了角标 = 所有角色未读的总数
- 已读位置记在 `localStorage`（`ce_read_<user>/<persona>` = 读到的最后一条消息 id）

### 顺手修的"还没聊过"

服务器刚重启时，前端请求失败被 `catch` 吞掉，界面错误地显示成"还没有聊过"。
现在：两个请求各自独立失败（`Promise.allSettled`）、失败时显示"加载失败，正在重试…"、
自动重试 5 次、WebSocket 重连时也会刷新列表。

## 5. 下一步建议（按"手机上真能用"倒推）

### 阶段 1：让手机能聊（最高优先级）

后端已经准备好了——`ChatService.stream_chat()` 和 `LLMClient.stream_chat()` **早就写好了**，只是没人用。

需要新增：
1. `src/companion_ever/api/` — FastAPI + WebSocket
   - `POST /api/chat` 或 `WS /ws/chat`：走 `ctx.services.chat.stream_chat()`
   - `POST /api/session/close`：把 CLI 的 `/quit` 哨兵变成一个按钮
   - `GET /api/personas` / `GET /api/notes`：给前端展示"她记得什么"
   - `GET /api/blocks`：关系状态（stage / stage_progress）
2. `web/index.html` + `manifest.webmanifest` + `sw.js` — 单文件 PWA，手机浏览器"添加到主屏幕"
3. 流式输出：`stream_chat` 逐块 `yield`，WebSocket 直接推
4. 一个 PIN / token 鉴权（同一个 WiFi 下别人能访问到你的伴侣）
5. 手机连本机：局域网 IP 即可（`http://192.168.x.x:8000`）；要外网就上 tailscale / cloudflared

> `build_app(AppConfig)` 返回的 `AppContext` 可以直接挂在 FastAPI 的 `app.state` 上，
> CLI 和 Web 共用同一套 service，不用改业务代码。

### 阶段 2：补上 wiki 关系图（你惦记的那块）

1. 定义关系类型枚举：`causes` / `temporal` / `elaborates` / `contradicts` / `references`
2. 让 Extraction LLM 在抽取 facts 的同时输出 `links`（引用已有 note 的 id 或内容摘要）
3. `NoteLinkRepo` + `NoteService.link_notes()`
4. **检索时 1-hop 扩展**：FTS5 召回 note A → 把 A 关联的 note B 也拉进 prompt
   → 这是 wiki 相对纯 RAG 的真正差异化，也是"她真的懂我"的来源
5. 前端画一张关系图（力导向图 / Cytoscape.js），手机上也能看"我们的记忆网络"

### 阶段 3：记忆质量

1. 定期任务重算 `decay_score`（按 importance × 时间衰减）+ 回写 `last_accessed_at`
2. 相似 note 去重 / 合并（Embedding 或 FTS 相似度）
3. 自动归档策略（低 importance + 低 decay + N 天未访问 → `is_archived=1`）
4. 长期向量检索（sqlite-vss / ChromaDB）补 FTS5 的长尾 recall

---

## 6. 其他文档

| 文件 | 内容 |
|---|---|
| `PROJECT_KB.md` | 项目知识库（面试复习卡片，含架构决策与自检清单） |
| `README.md` | 项目说明 + 安装 |
| `TESTING.md` | 全链路测试指南 |
