# 同行（Companion Ever Memory）

带长期记忆的 AI 伴侣原型：Python 服务、SQLite 本地存储、Web/PWA 界面，联网调用配置的模型。支持多角色私聊、基础群聊、消息引用、多条回复、后台恢复、两层笔记、独立摘要及私聊主动联系。当前仍依赖电脑或服务器；独立手机 App 和语义检索尚未实现。

Web 端应用名叫「同行」，界面里一律用「TA」指代角色——角色可以是任何性别，界面不该替它假定。

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
.\.venv\Scripts\python.exe -m companion_ever account create --username alice --bind-user alice --permanent
# 仅本机 HTTP 开发；服务器 HTTPS 保持默认安全 Cookie
$env:COMPANION_WEB_SECURE_COOKIE="0"
.\.venv\Scripts\python.exe -m companion_ever serve
```

已有数据升级账号版本时，先备份、更新依赖，再用 `account create --bind-user 原用户ID` 绑定原用户；不要重新 setup。配置说明见 [.env.example](.env.example)。依赖只在 [pyproject.toml](pyproject.toml) 声明，安装直接使用 pip。

## 账号与数据隔离

完整管理命令和操作步骤见 [管理命令手册](ADMIN_COMMANDS.md)。

Web 始终要求独立账号登录，共享 PIN 已停用。现有 `user` 表保存登录名和 Argon2id 密码哈希，只新增 `auth_session` 一张表保存可撤销会话。登录凭据使用 HttpOnly Cookie，30 天无活动过期、最多 90 天重新登录；浏览器不保存密码或在 URL 中传凭据。普通账号只可访问自己的角色、聊天、记忆、后台任务、通知及调用记录。

服务器终端管理账号，密码由隐藏输入交互提供，不放进命令参数：

```bash
# 先确认原用户 ID，再明确绑定本人现有数据
.venv/bin/python -m companion_ever account list
.venv/bin/python -m companion_ever account create --username yourname --bind-user 原用户ID --permanent
# 好友使用独立空白用户；初始密码首次登录必须修改
.venv/bin/python -m companion_ever account create --username friendname
.venv/bin/python -m companion_ever account reset-password --username friendname
.venv/bin/python -m companion_ever account revoke-sessions --username friendname
.venv/bin/python -m companion_ever account disable --username friendname
.venv/bin/python -m companion_ever account enable --username friendname
# 查看账号当天用量；可加 --date YYYY-MM-DD
.venv/bin/python -m companion_ever account usage --username friendname
# 单独设置好友额度，0 表示不限；--reset 恢复默认额度
.venv/bin/python -m companion_ever account limits --username friendname --daily-messages 100 --concurrent-replies 1
```

登录名为 3–32 位字母、数字、点、下划线或短横线，密码为 6–128 个字符。账号管理不启动模型或后台任务。停用账号保留数据并拒绝访问与后续执行；重新启用不会恢复旧登录。修改或重置密码撤销全部旧登录和通知订阅，需要重新登录并开启通知。「我」页面提供改密、退出和退出所有设备。

HTTPS 部署保持 `COMPANION_WEB_SECURE_COOKIE=1`；反向代理时可将 `COMPANION_WEB_ORIGIN` 设置为实际 HTTPS 入口（不带路径），供来源校验使用。只有本机 HTTP 开发显式设 `COMPANION_WEB_SECURE_COOKIE=0`。切换账号时清理页面与待发送缓存，首次从旧 PIN 版本登录需要重新开启设备通知。数据仍存储于部署者服务器，聊天内容按配置发送给模型提供商；账号隔离不提供端到端加密。

「我 → 今日用量」汇总当前账号全部角色的聊天、记忆整理、摘要和群聊协调调用，包含输入/输出及缓存 token、累计模型耗时和失败次数。统计只包含已记录调用，缺失 token 显示未上报，不视为零；输入 token 已包含其中的缓存部分。消息额度直接读取数据库，不依赖日志是否启用。

默认每个账号每天最多接收 200 条新用户消息或手动群续聊请求，同时最多启动 2 个有效回复任务。默认值由 `COMPANION_DAILY_MESSAGES`、`COMPANION_CONCURRENT_REPLIES` 配置，单账号可通过上述命令覆盖。自然日按 `COMPANION_TZ_OFFSET_HOURS` 重置；已接收的请求即使模型失败仍计数，重复提交及失败回复重试不重复计算，重试的实际调用仍进入用量统计。同一会话的新输入可替换旧回复；主动联系同样受回复并发约束，整理与摘要沿用各自调度。

额度不足时不保存新消息、不取消原回复，前端保留未发送文本并提供手动重试，刷新或重连不会反复提交被拒消息。下调并发额度不终止已运行任务，待执行任务在有空位后继续。schema v11 仅增加用户额度覆盖字段与查询索引，不新增表；从旧库升级前自动生成 `.bak-before-account-limits-...` 一致性备份，保留原账号与数据。部署前仍先主动备份，旧代码不能直接读取更新版本的库。

基础群聊使用 schema v12，升级前自动生成 `.bak-before-groups-...` 一致性备份，保留旧消息 ID、已读位置、笔记证据和思考记录。会话容器与发言角色分开存储，群记录不会混入私聊历史或交给旧私聊记忆整理器。

消息页右上角可选择2至8个已有角色建群；群内输入 `@` 从底部选择成员，一次最多指定两位，按出现顺序回应并完全跳过协调模型。没有指定成员时，协调模型最多调用一次，返回有顺序的邀请名单，也可以选择无人接话。每位角色的全部气泡保存后才调用下一位，后者重新读取最新群历史。私聊的 `@` 是普通字符。

群内「继续聊聊」按钮可手动邀请成员续聊，不伪造用户发言；正在回复时不可重复触发，每次占用一条日额度。角色可通过同次 JSON 回复中的 `handoff: {"persona_id":"成员ID","message_index":0}` 交棒，程序校验后为指定气泡补上真正的 @，并将该消息作为下一位的直接触发来源。普通正文或引用中的 @ 不触发调度。用户指定顺序优先，已在队列中的成员只合并邀请；每轮最多尝试三位不同角色、最多四次模型调用，无交棒就结束。空闲自动主动联系仍最多邀请一位。

群聊右上角「⋯」可开启群成员主动联系，默认关闭。群与私聊共用每账号每天一次的自动联系机会，先检查空闲、免打扰、未读和并发，再让协调者决定是否邀请一位角色。没有合适话题可结束，不会每个扫描周期反复询问模型。消息保存后才加入通知，同一轮多个气泡合并提醒，点击打开对应群；前台正在查看时暂缓，已读或用户已继续发言时不再提醒。

群记忆沿用自动整理门槛，按「会话、角色」分别推进，失败只重试该角色；笔记仍只有 episode/fact，列表通过证据显示「来自群聊」。各角色 fact 继续进入原有用户画像与关系摘要链路。群提示词仅使用本角色、来源完整属于本群的笔记与摘要，私聊或混合来源内容不自动公开。群转私聊暂未实现。

schema v13 在 v12 会话底座上补群主动设置、可见状态、按角色的记忆任务及通知会话定位。当前 schema v14 增加任务触发来源、手动请求标识及交棒原消息引用，不新增表；升级前创建 `.bak-before-group-handoffs-...` 一致性备份，保留原消息、笔记、摘要与任务。交棒与回复在同一事务中保存，失败重试沿用未完成队列，不重新生成已经送达的角色回复。

私聊和群聊都可长按已发送气泡（电脑右键或菜单键）选择复制、引用，支持引用自己的发言。发送前可取消引用；发送后点击引用卡片定位原消息。引用原文由服务端按会话读取，不因聊天窗口截断而丢失；断线重发、失败重试保留引用与 @ 信息。

## 当前行为

- Web 界面里可以新建角色：选内置模板或自己填字段；角色资料页可以删除角色——删除只是停用，记忆和聊天记录都留着，能在「已删除的角色」里恢复。
- 「TA 的记忆」里可以直接编辑或删除笔记；编辑保留旧版本，删除退出有效记忆并保留聊天记录，fact 变更继续驱动后台摘要。
- 消息先保存，服务器独立生成；手机切后台后，返回页面可恢复正式回复。
- 「我 → 消息通知」可为当前设备开启或关闭 Web Push；主动消息保存后通过在线事件及系统通知触达，普通回复暂未接入系统通知。点击通知优先唤起运行中的页面并切换会话，未响应时再导航启动。
- 角色资料中的「主动联系」默认关闭；开启后在本地时区 10:00—22:00、距最近用户输入至少四小时且上一条主动消息已读时才考虑联系，每个用户每天最多安排一次。模型可以选择不联系。
- 主动提示词复用人设、行为规则、有限真实历史和两个成功摘要，专用说明在 `proactive_system.txt`；首版不从历史抽词检索，不伪造用户输入。正常聊天提示词不加入主动专用章节。
- 回复优先按 `<message_break>` 分气泡；没有分隔符时，纯文本空行兼容拆分，单换行与 Markdown 代码、列表、表格保留排版，最多三泡。
- 账号密码通过校验后建立服务器会话，再次打开保持登录；HTTP 与 WebSocket 按登录用户校验资源归属，写操作校验 CSRF，登录失败限流。
- 页面为脚本和样式附加内容版本号，部署后刷新即可获取新版本；访问凭据失效时返回登录页，保留未发送消息。
- 记忆自动整理：积累 12 条用户新消息，或空闲 10 分钟且至少 3 条；少量积压最长约 24 小时兜底。
- 整理链路由 LangGraph 编排，模型可只读检索旧笔记及其证据/关系，再统一校验和原子写入。
- 摘要同样由 LangGraph 编排复用、清空、生成及有限纠错；两条后台链路共享原有任务与事务机制。
- 模型调用按日记录基本信息、实际 token、缓存计数与耗时；群聊协调也计入账号用量，不保存提示词、原始回复或节点产物。
- 用户画像和关系概况从 fact 操作独立演化：首次自动生成，后续累计 8 条实质变化或等待 6 小时；涉及摘要引用的纠正优先，纯证据/元数据变化免模型处理。
- 摘要更新期间或失败时，聊天沿用上次成功版本；近期用户的明确纠正优先，新摘要成功后整体替换。
- 用户画像与关系概况只用于聊天上下文，普通页面不再展示内部摘要或“待核实”结论；后台任务状态保留在调试入口。
- 正常页面隐藏维护按钮；网址添加 `?debug` 可手动触发及查看任务诊断。
- `?debug` 下「我 → 模型调用记录」可查看、筛选和下载轻量记录；顶部 token 与累计调用耗时跟随当前筛选变化。
- `kind=episode/fact` 是唯一记忆分类；`reality` 独立表示用户自述、共同想象或尚未核实。
- 固定笔记功能已移除，笔记按相关性检索；旧库启动时迁至 schema v7，仅移除固定标记，保留笔记内容及关联数据。

旧笔记迁移时的 `reality` 保留为 `uncertain`，摘要可能因此显示“待核实”。这不表示任务尚未完成，也不会通过自动迁移猜测旧内容的现实情境。

## 手机通知与主动联系

更新依赖后，在服务器 `.env` 设置 `COMPANION_PUSH_SUBJECT`，值为实际 HTTPS 网站地址或 `mailto:你的邮箱`；留空时通知不开启。重启应用后，iPhone 从主屏幕打开同行，在「我」中点击开启消息通知并允许系统权限，随后可发送测试通知。推送服务接受请求不等于手机已经显示，需实际验证后台、锁屏与点击跳转。打开会话仍需能访问部署的 HTTPS 入口。

推送密钥首次启用时自动生成，和设备订阅一起存入现有私有数据库；备份数据库即可一并保留。schema v9 以加列及新表扩展生成来源、已读位置和通知待发记录，保留现有消息、笔记和推送密钥。不要把密钥或完整订阅地址放入日志。

服务器可单独触发主动联系测试，网页关闭后仍由运行中的服务执行：

```bash
cd /opt/tongxing
.venv/bin/python -m companion_ever proactive trigger --user yike --persona "角色ID" --test --delay 15
# 用上面返回的任务 ID 查询结果：
.venv/bin/python -m companion_ever proactive status --task "任务ID"
```

`--test` 跳过自动触发的开关、免打扰和每日限制，实际调用模型并保存聊天；角色权限、待处理任务和用户插话检查仍然生效。该角色需至少有过一次用户发言。`--delay` 为 0—3600 秒，实际开始时间还受约 15 秒的扫描间隔影响。可加 `--request-id` 防止重复提交；相同 ID 返回原任务。`proactive check --user ... --persona ...` 只检查条件，不调用模型。通知失败仅重试通知，不重新生成；已读、用户已发来新消息或过期时跳过提醒。模型调用列表中主动联系仍归聊天层，标为「主动聊天」。

## Review 入口

| 文件 | 内容 |
|---|---|
| [架构与阅读顺序](docs/ARCHITECTURE.md) | 当前调用链、表与代码职责 |
| [记忆规则](docs/MEMORY_POLICY.md) | 笔记、证据、关系、摘要与召回契约 |
| [后台任务](docs/BACKGROUND_TASKS.md) | 自动触发、消息恢复、重试、迁移与维护 |
| [调用诊断](docs/TRACING.md) | 轻量调用记录、筛选统计与任务执行状态 |
| [验证与工具](docs/MEMORY_EVALUATION.md) | 测试、评测、迁移检查及保留工具的用途 |
| [待 review 项](REVIEW_NOTES.md) | 仍存在的问题和未决定的方向 |

## 验证与备份

```powershell
.\.venv\Scripts\python.exe tools/check.py
.\.venv\Scripts\python.exe -m companion_ever maintain backup
Get-Content memory/calls/*.jsonl -Tail 10
```

测试使用临时数据库和假模型；前端回归需要 Node.js。常规验证不调用收费 API。`memory/` 保存私人数据库、备份、轻量调用记录和评测产物，`.env` 保存私有配置。旧 trace 的生成与查看逻辑已删除，已有历史文件保留，新记录位于数据库同级 `calls/`。
