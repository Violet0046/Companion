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
