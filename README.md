# Companion Ever Memory

带长期记忆的 AI 伴侣原型：Python 服务、SQLite 本地存储、Web/PWA 界面，联网调用配置的模型。支持多角色私聊、流式多条回复、后台恢复、两层笔记及独立摘要。当前仍依赖电脑或服务器；独立手机 App、群聊、主动聊天和语义检索尚未实现。

按业务模块的重构方案与进度见 [架构](docs/ARCHITECTURE.md)。角色、会话和记忆已分别迁入 `characters/`、`conversations/`、`memory/`；笔记、检索、整理、摘要和业务触发规则在记忆模块内分开组织。Web 只调用业务入口和订阅结果，离线评测位于 `tools/evaluation/`；公共运行层与基础设施继续收敛。

## 启动

Windows PowerShell，在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# 首次配置时执行；已有 .env 时保留原文件
Copy-Item .env.example .env
# 编辑 .env 中的模型配置，再创建示例用户和角色
.\.venv\Scripts\python.exe -m companion_ever setup --user alice --persona shushu --template hot_girlfriend
.\.venv\Scripts\python.exe -m companion_ever serve
```

已有数据时直接启动服务即可。配置说明见 [.env.example](.env.example)。依赖只在 [pyproject.toml](pyproject.toml) 声明，安装直接使用 pip。

## 当前行为

- Web 界面里可以新建角色：选内置模板或自己填字段；角色资料页可以删除角色——删除只是停用，记忆和聊天记录都留着，能在「已删除的角色」里恢复。
- 消息先保存，服务器独立生成；手机切后台后，返回页面可恢复正式回复。
- 记忆自动整理：积累 12 条用户新消息，或空闲 10 分钟且至少 3 条；少量积压最长约 24 小时兜底。
- 整理链路由 LangGraph 编排，模型可只读检索旧笔记及其证据/关系，再统一校验和原子写入。
- 摘要同样由 LangGraph 编排复用、清空、生成及有限纠错；两条后台链路共享原有任务与事务机制。
- 三层模型调用在原 trace 中记录实际 token、缓存计数与耗时；当前保留原聊天提示词结构。
- 用户画像和关系概况从 fact 操作独立演化：首次自动生成，后续累计 8 条实质变化或等待 6 小时；涉及摘要引用的纠正优先，纯证据/元数据变化免模型处理。
- 摘要更新期间或失败时，聊天沿用上次成功版本；近期用户的明确纠正优先，新摘要成功后整体替换。
- 正常页面隐藏维护按钮；网址添加 `?debug` 可手动触发及查看任务诊断。
- `kind=episode/fact` 是唯一记忆分类；`reality` 独立表示用户自述、共同想象或尚未核实。

旧笔记迁移时的 `reality` 保留为 `uncertain`，摘要可能因此显示“待核实”。这不表示任务尚未完成，也不会通过自动迁移猜测旧内容的现实情境。

## Review 入口

| 文件 | 内容 |
|---|---|
| [架构与阅读顺序](docs/ARCHITECTURE.md) | 当前调用链、表与代码职责 |
| [记忆规则](docs/MEMORY_POLICY.md) | 笔记、证据、关系、摘要与召回契约 |
| [后台任务](docs/BACKGROUND_TASKS.md) | 自动触发、消息恢复、重试、迁移与维护 |
| [链路诊断](docs/TRACING.md) | 完整模型请求、原始回复、校验与执行记录 |
| [验证与工具](docs/MEMORY_EVALUATION.md) | 测试、评测、迁移检查及保留工具的用途 |
| [待 review 项](REVIEW_NOTES.md) | 仍存在的问题和未决定的方向 |

## 验证与备份

```powershell
.\.venv\Scripts\python.exe tools/check.py
.\.venv\Scripts\python.exe -m companion_ever maintain backup
.\.venv\Scripts\python.exe -m companion_ever trace list --last 10
```

测试使用临时数据库和假模型；前端回归需要 Node.js。常规验证不调用收费 API。`memory/` 保存私人数据库、备份、trace 和评测产物，`.env` 保存私有配置。
