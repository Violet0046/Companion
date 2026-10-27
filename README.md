# Companion Ever Memory

> **刚回来？先看 [START_HERE.md](START_HERE.md)** —— 有当前进度盘点、启动命令、以及"下一步该做什么"。
> 也可直接看 [PROJECT_KB.md](PROJECT_KB.md)（项目知识库 / 架构决策）。

基于 SQLite + LangGraph 的对话式长期记忆系统。

## 特性

- **两个 LLM 职责分离**：Chat LLM 只生成对话回复；Extraction LLM 在 session 关闭时抽取 facts + 更新 blocks
- **Sentinel session 边界**：每次 `session_close` 在 `conversation_log` 写一行 `role='session_close'` 作为 session 边界
- **prompt 干净**：Chat LLM 只看到当前 session 的对话历史；历史 session 的信息通过 retrieval 从 notes 取回
- **LangGraph 工作流**：`chat_workflow` + `extraction_workflow` 用 LangGraph StateGraph 编排
- **集中授权**：`Authorizer` 单一入口，消除 6 处重复的 `_check_permission`
- **配置层**：`.env` 文件 + `python-dotenv` 自动加载

## 目录结构

```
src/companion_ever/
├── __init__.py
├── app.py                       # 组合根（build_app）
├── config.py                    # 配置层（.env / 环境变量）
│
├── domain/                      # 纯数据 + 类型 + 模板
│   ├── enums.py                 # NoteType / BlockType
│   ├── entities.py              # @dataclass User / Note / Block / ...
│   ├── errors.py
│   └── persona_templates.py
│
├── infrastructure/              # 外部关注点
│   ├── persistence/             # SQLite + repos
│   └── ai/                      # LLM 客户端
│
├── application/                 # 用例服务（按子域）
│   ├── authz.py                 # Authorizer（集中授权）
│   ├── llm_json.py              # LLM 响应清理
│   ├── persona/                 # 人设子域
│   ├── memory/                  # 记忆子域
│   └── chat/                    # 对话子域
│
├── workflows/                   # LangGraph 工作流
│   ├── chat_workflow.py         # chat + close 节点
│   └── extraction_workflow.py   # load + extract + persist
│
├── prompts/                     # LLM 提示词（.txt）
│
├── utils/                       # 横切工具
│
└── cli/                         # 命令行工具
    └── __init__.py              # setup / chat / show 子命令
```

## 安装

### 一键 setup（推荐）

```bash
# Linux / macOS
chmod +x setup.sh
./setup.sh

# Windows (PowerShell)
.\setup.ps1

# Windows (cmd)
setup.bat
```

> **Windows 三种启动方式**：
> | 命令 | 适用 |
> |---|---|
> | `.\setup.ps1` | **PowerShell（最可靠）** |
> | `setup.bat` | cmd.exe |
> | `cmd /c setup.bat` | PowerShell 中转 cmd |
>
> **PowerShell 中跑 `setup.bat` 会乱码**，因为 PS 会尝试解析 .bat。直接用 `.\setup.ps1`。

会自动：
- 创建 `.venv/` 虚拟环境
- 安装 `requirements.txt` 中的依赖
- 把 `.env.example` 复制为 `.env`（如果还没有）

### 手动 setup

```bash
python -m venv .venv
source .venv/bin/activate            # Linux/macOS
# .venv\Scripts\activate            # Windows
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY / OPENAI_BASE_URL / COMPANION_LLM_MODEL
```

`.venv/` 已经在 `.gitignore` 里——每个人各自创建。

## 快速上手

```bash
# 1. 创建用户 + 角色
PYTHONPATH=src python -m companion_ever.cli setup \
    --user alice --persona 柳如烟 --template hot_girlfriend

# 2. 启动对话
PYTHONPATH=src python -m companion_ever.cli chat \
    --user alice --persona 柳如烟
#   > 你好，我叫周艺科
#   > /quit   ← 触发 session close + extraction

# 3. 查看抽取结果
PYTHONPATH=src python -m companion_ever.cli show \
    --user alice --persona 柳如烟
```

详见 [TESTING.md](TESTING.md)。

## 诊断：trace（看模型究竟看到了什么）

```bash
# 列出最近的 trace
companion-cli trace list --user yike --persona 柳如烟 --last 10

# 看某一轮的完整链路
companion-cli trace show <trace_id>

# 看模型实际收到的完整请求（system prompt + 历史 + 召回的记忆）
companion-cli trace show <trace_id> --request
```

trace 落盘在 `memory/traces/<日期>/`，包含元数据（链路步骤 + 耗时 + token）
和产物（实际发出的请求 / 实际收到的回复）。
内置的廉价不变量会自动检查 prompt 组装是否正常（历史重复 / 历史取错 /
超预算 / 召回失效等），违规会在 `trace show` 里高亮。

详见 [START_HERE.md](START_HERE.md)。

## 测试

```bash
PYTHONPATH=src python tests/test_persona_service.py
PYTHONPATH=src python tests/test_sentinel_flow.py
PYTHONPATH=src python tests/test_chat_workflow.py
```

## License

Internal project.