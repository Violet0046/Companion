# 全链路测试指南

> 目标：亲手创建用户 → 创建角色 → 启动对话 → 退出 → 看到抽取 LLM 把对话变成 notes / blocks

---

## 0. 前置准备

### 0.1 一键 setup（推荐）

```bash
# Linux / macOS
chmod +x setup.sh
./setup.sh

# Windows (cmd)
setup.bat

# Windows (PowerShell) ← 推荐用 PowerShell 版
.\setup.ps1
```

> **Windows 三种启动方式**：
> | 命令 | 适用 |
> |---|---|
> | `setup.bat` | cmd.exe 启动后运行 |
> | `cmd /c setup.bat` | PowerShell 中转 cmd |
> | `.\setup.ps1` | **PowerShell 原生**（最可靠） |
>
> 如果用 PowerShell 跑 `setup.bat` 报 `'锛?REM' 不是内部或外部命令`，说明 PowerShell 在解析 .bat 时挂了——直接改用 `.\setup.ps1`。

脚本自动：
- 创建 `.venv/` 虚拟环境
- 安装 `requirements.txt` 中的依赖
- 把 `.env.example` 复制为 `.env`

激活 venv（可选）：

```bash
# Linux / macOS
source .venv/bin/activate

# Windows (cmd)
.venv\Scripts\activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

不激活也可以：直接用 `.venv/bin/python` 或 `.venv\Scripts\python.exe`。

### 0.2 或者手动安装依赖

```bash
python -m venv .venv
source .venv/bin/activate            # Linux/macOS
# .venv\Scripts\activate            # Windows
pip install -r requirements.txt
```

依赖：`openai`、`pydantic`、`langgraph`、`python-dotenv`

### 0.3 配置 `.env`（推荐）

把 `.env.example` 复制为 `.env`，填入真实值：

```bash
cp .env.example .env       # Linux / macOS
copy .env.example .env     # Windows
```

`.env` 内容示例：

```ini
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
COMPANION_LLM_MODEL=gpt-4o-mini

# 可选
# COMPANION_DB_PATH=./test.db
# COMPANION_TZ_OFFSET_HOURS=8
# COMPANION_RECENT_MESSAGE_LIMIT=20
```

CLI 启动时会**自动加载** `.env`（通过 `python-dotenv`），无需手动 `export`。

### 0.4 或者手动 export 环境变量

如果不想要 `.env` 文件：

```bash
# Windows PowerShell
$env:OPENAI_API_KEY="sk-..."
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:COMPANION_LLM_MODEL="gpt-4o-mini"

# Linux / macOS
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
export COMPANION_LLM_MODEL="gpt-4o-mini"
```

> **优先级**：命令行参数 > os.environ 已设置值 > .env 文件 > 内置默认值
> （`.env` 不会覆盖已经在 shell 里 export 过的同名变量）

### 0.5 进入项目目录

```bash
cd "d:/研文档/代码相关/companion ever memory"
```

---

## 1. 创建用户 + 角色（setup）

```bash
PYTHONPATH=src python -m companion_ever.cli setup --user alice --persona 柳如烟 --template hot_girlfriend
```

或自定义 `definition`：

```bash
PYTHONPATH=src python -m companion_ever.cli setup --user alice --persona 柳如烟 --definition '{"name": "舒舒", "identity": "温柔倾听者", "personality": ["温柔","耐心"]}'
```

**预期输出：**
```
[OK] Created user 'alice'
[--] Using template 'hot_girlfriend'
[OK] Created persona '柳如烟' for user 'alice'
```

**说明：**
- 不指定 `--template` 或 `--definition` 时默认用 `hot_girlfriend` 模板
- DB 文件默认在 `./companion.db`，可用 `--db ./test.db` 指定其他路径
- `setup` 是**幂等**的：user / persona 已存在会跳过，不会重复创建

---

## 2. 启动对话（chat）

```bash
PYTHONPATH=src python -m companion_ever.cli chat --user alice --persona 柳如烟
```

进入 REPL 循环：

```
=== Companion Chat ===
  user:    alice
  persona: 柳如烟
  输入 '/quit' 退出并触发 session 抽取。

[柳如烟] > 你好，我叫周艺科

[柳如烟] 你好呀，我叫周艺科~

[柳如烟] > 我最近秋招压力好大

[柳如烟] 嗯嗯，我能感受到你的焦虑...

[柳如烟] > /quit

=== Session Closed ===
  marker_id: 7
  conversations_processed: 4
  facts_created: 2
  blocks_updated: 1
```

**说明：**
- **每轮对话 = 一次 Chat LLM 调用**（拼 prompt + 生成回复 + 写 conversation_log）
- 输入 `/quit` 触发 session close：写 sentinel marker + 启动 extraction_workflow
- extraction_workflow 调**另一个 LLM**（同样用 `OPENAI_*` 变量）抽取 facts + 更新 blocks
- 退出后自动返回 shell

---

## 3. 查看抽取结果（show）

```bash
PYTHONPATH=src python -m companion_ever.cli show --user alice --persona 柳如烟
```

**预期输出：**

```
User 'alice' owns 1 persona(s): ['柳如烟']

--- Notes (3) ---
  [persona_basics] importance=0.90
    {"name": "柳如烟", "identity": "热辣女友", "personality": ["温柔", "热情", "活泼"]}
  [profile_fact] importance=0.95
    用户叫周艺科
  [observation] importance=0.50
    用户最近秋招压力较大

--- Blocks (3) ---
  [persona_runtime]
    {
      "current_topic": "秋招焦虑"
    }
  [user_profile]
    {}
  [relationship_state]
    {}

--- Conversation ---
  total messages: 4
  session_close markers: 1
```

**说明：**
- `persona_basics` note：人设（创建时就写入）
- `profile_fact` / `observation` 等：抽取 LLM 从对话中提取的事实
- 3 个 Block：运行时会话状态，extraction 会更新
- 第二次会话启动时，prompt 中只会看到 session_close 之后的新对话，**旧对话不会污染**

---

## 4. 完整流程验证清单

跑完上面的 1-3 步后，依次确认：

- [ ] `conversation_log` 有 user + assistant 配对的对话
- [ ] `conversation_log` 有 1 个 `session_close` 行（role='session_close'）
- [ ] `note` 表有 `persona_basics`（创建时写入）+ 至少 1 条 `profile_fact` 或 `observation`（抽取写入）
- [ ] `memory_block` 表的 `persona_runtime` 有 `current_topic` 字段（抽取写入）
- [ ] 第二次 `/quit` 之前：show 看 `session_close markers: 1`
- [ ] 第二次 `/quit` 之后：show 看 `session_close markers: 2`

如果全部 ✅，说明**链路工作正常**。

---

## 5. 验证"对话-抽取分离"

**目的**：确认 Chat LLM 的 prompt 是干净的（只看到当前 session 历史 + 历史 session 抽取出的 notes）。

```bash
# Step A: 第一次会话 → 退出 → 抽取
PYTHONPATH=src python -m companion_ever.cli chat --user alice --persona 柳如烟
> 你好
> /quit

# Step B: 看抽取结果（profile_fact 出现了）
PYTHONPATH=src python -m companion_ever.cli show --user alice --persona 柳如烟

# Step C: 第二次会话（新 session）
PYTHONPATH=src python -m companion_ever.cli chat --user alice --persona 柳如烟
> 我又来了
> /quit

# Step D: 验证
# - prompt 中只包含 [step C 的对话]
# - 检索 notes 召回 [step A 抽取出的 facts]
```

---

## 6. 故障排查

### 报错 `ModuleNotFoundError: No module named 'langgraph'`

```bash
pip install langgraph
```

### 报错 `OPENAI_API_KEY` 未设置

```bash
echo $env:OPENAI_API_KEY    # PowerShell
echo $OPENAI_API_KEY         # bash

# 设置后重新启动 CLI
```

### 报错 `OPENAI_BASE_URL` 不合法 / 连接失败

如果你用的是 OpenAI 兼容服务（DeepSeek / Qwen / 智谱 / 自建等），确认：
- `OPENAI_BASE_URL` 是完整的 base URL（含 `https://`）
- `OPENAI_MODEL` 是该服务支持的模型名

### 报错 `User 'alice' 不存在` 或 `不拥有 persona '柳如烟'`

先跑 `setup`：

```bash
PYTHONPATH=src python -m companion_ever.cli setup --user alice --persona 柳如烟 --template hot_girlfriend
```

### 报错 `User 'alice' 已拥有 persona '柳如烟'`

`setup` 是幂等的——已存在的 user / persona 会跳过，不会重复创建。可以直接进入 `chat` / `show`。

### 抽取 LLM 没有写入任何 notes

可能原因：
- 对话太短（如只说了 "你好"），LLM 没有抽取到事实
- 用的模型对中文 prompt 支持弱——换一个模型试试

---

## 7. 命令速查

```bash
# 创建用户 + 角色
PYTHONPATH=src python -m companion_ever.cli setup --user <USER> --persona <PERSONA> [--template <TEMPLATE> | --definition <JSON>]

# 启动对话
PYTHONPATH=src python -m companion_ever.cli chat --user <USER> --persona <PERSONA>

# 查看状态
PYTHONPATH=src python -m companion_ever.cli show --user <USER> [--persona <PERSONA>]

# 自定义 DB 路径
PYTHONPATH=src python -m companion_ever.cli setup --user alice --persona 柳如烟 --db ./test.db
```

模板名（`--template`）：`hot_girlfriend`、`philosophical_mentor`

---

## 8. 进阶：直接看 DB

如果你想直接看 SQLite 表里的内容：

```bash
sqlite3 ./companion.db "SELECT id, role, substr(content, 1, 50) FROM conversation_log;"
sqlite3 ./companion.db "SELECT id, type, substr(content, 1, 50), importance FROM note;"
sqlite3 ./companion.db "SELECT block_type, fields_json FROM memory_block;"
```

`session_close` 行在 conversation_log 里 `role='session_close'` 且 `content='[session_close]'`，可作为 session 边界。