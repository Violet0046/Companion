# Companion Ever Memory

一个带长期记忆的 AI 伴侣原型。当前可通过命令行或 Web/PWA 聊天；目标是 **iPhone 本地保存数据和执行业务逻辑，联网调用模型 API**，并考虑 Android。

**先读 [开发盘点与路线图](docs/PROJECT.md)，再按 [代码阅读指南](docs/ARCHITECTURE.md) review。**
旧开发日志已从工作目录移除，可在 Git 历史中查看，不代表当前状态。

正在进行的[两层记忆审阅实验](docs/MEMORY_REVIEW.md)只生成建议，不修改正式记忆。真实样本的本地入口是 `memory/reviews/2026-10-29/START_HERE.md`。

## 当前能做什么

- 单用户、多角色的私聊；WebSocket 流式回复与聊天历史查询。
- 角色设定、用户画像、关系摘要、记忆浏览和历史日期检索。
- 手动或空闲后抽取长期事实；精确重复事实幂等写入。
- SQLite FTS5 + 中文短词检索；召回去重与关键词相关候选优先。
- [统一记忆检索与关系扩展](docs/RETRIEVAL.md)：按需召回，支持空结果、关联来源和后续进展。
- 模型返回的思考字段与正文分开保存；trace 可检查实际模型请求。
- 用户消息先保存，支持连续输入、取消未完成回复、一次生成多条独立消息。
- 记忆整理使用固定范围任务，支持失败重试和重启恢复。详见[消息与任务链路](docs/MESSAGE_JOBS.md)。

尚未实现：独立手机 App、群聊、后台主动聊天、语义去重、自动记忆关联、短期状态层。
当前 Web/PWA 仍依赖运行 Python 服务的电脑或服务器。

## 启动（Windows PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
# 首次配置；已有 .env 时不要覆盖
Copy-Item .env.example .env
# 编辑 .env 中的模型 API 配置
.\.venv\Scripts\python.exe -m companion_ever setup --user alice --persona shushu --template hot_girlfriend
.\.venv\Scripts\python.exe -m companion_ever serve
```

也可使用现有 `setup.ps1` / `setup.bat` / `setup.sh`。依赖统一声明在 `pyproject.toml`。
CLI 私聊：`companion-cli chat --user alice --persona shushu`；查看记忆：`companion-cli show --user alice --persona shushu`。

## 目录

```text
src/companion_ever/
  app.py / config.py    装配与配置
  services/            聊天、抽取、召回、角色等业务规则（平铺）
  storage/             SQLite 表、查询与写入
  llm/                 模型协议、客户端与 trace 包装
  domain/              实体、枚举与角色模板
  api/                 HTTP / WebSocket 适配
  cli/                 命令行与维护命令
  web/                 现有网页界面
  prompts/             提示词
  workflows/           现有 LangGraph 调用接口的薄适配层
  utils/ / trace.py    文本、时间与诊断
```

`tests/` 是回归验证；`tools/` 是开发工具；`design/mockup/` 是设计参考，不参与运行。
`memory/` 是本地数据与备份，`.env` 是私有配置，不要当作过程文件删除。

## 验证与维护

```powershell
.\.venv\Scripts\python.exe tools/check.py
companion-cli maintain backup
companion-cli trace list --user alice --persona shushu --last 10
```

测试逐个运行脚本、使用临时数据库和假模型，不调用收费 API。部分旧测试是脚本 `main()`，只运行 pytest 不能覆盖全部验证。
备份使用维护命令读取 SQLite 的完整状态，不能仅复制 WAL 模式下的主数据库文件。
