# 同行（Companion Ever Memory）

带长期记忆的 AI 伴侣应用：Python 服务、SQLite 数据库、Web/PWA 界面，联网调用配置的聊天模型。支持多角色私聊、群聊协调与交棒、消息引用、多条回复、后台恢复、系统通知、笔记与独立摘要，以及角色主动联系。

手机主屏幕应用仍是 PWA：服务端负责模型调用、数据保存和后台任务，手机本地缓存加速显示。当前没有独立原生 App，也尚未接入 Embedding 向量召回或 Rerank。

Web 端应用名叫「同行」，界面里一律用「TA」指代角色——角色可以是任何性别，界面不该替它假定。

业务模块已收敛为 `accounts/`、`characters/`、`conversations/`、`memory/`、`notifications/` 等；`bootstrap/` 装配依赖，`runtime/` 负责后台运行，`infrastructure/` 提供数据库、模型客户端和日志等基础设施，`entrypoints/` 提供 Web 与 CLI 入口。Web 调用业务入口并订阅执行结果。

## 启动

需要 Python 3.10 或更高版本。Windows PowerShell，在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# 首次配置时执行；已有 .env 时保留原文件
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 编辑 .env 中的模型配置，再创建示例用户和角色
.\.venv\Scripts\python.exe -m companion_ever setup --user alice --persona shushu --template hot_girlfriend
.\.venv\Scripts\python.exe -m companion_ever account create --username alice --bind-user alice --permanent
# 仅本机 HTTP 开发；服务器 HTTPS 保持默认安全 Cookie
$env:COMPANION_WEB_SECURE_COOKIE="0"
.\.venv\Scripts\python.exe -m companion_ever serve
```

浏览器打开 `http://127.0.0.1:8000`，使用刚创建的账号登录。模型配置仍是占位值时无法正常生成回复。已有数据升级账号版本时，先备份、更新依赖，再用 `account create --bind-user 原用户ID` 绑定原用户；不要重新 setup。配置说明见 [.env.example](.env.example)，依赖统一在 [pyproject.toml](pyproject.toml) 声明。

配置从工作目录的 `.env` 加载，已设置的进程环境变量优先；相对数据库路径也以工作目录为基准。修改模型、通知或调度参数后重启应用。公网部署使用自己的 HTTPS 入口；仅本机或反向代理时建议将 `COMPANION_WEB_HOST` 显式设为 `127.0.0.1`。应用不依赖 Tailscale。

使用 `MiniMax-M3.1-Flash` 时，统一请求封装默认显式发送 `reasoning_effort: "low"`，避免省略参数后使用供应商的最高思考档位。可通过 `COMPANION_REASONING_EFFORT` 改为 `low`、`medium`、`high`、`xhigh` 或 `max`，覆盖私聊、群聊、主动联系、群协调、整理及摘要的调用。该模型不支持关闭思考，`COMPANION_BACKGROUND_THINKING` 不控制它；其他模型不会自动携带此强度参数。网页「显示思考过程」仅控制展示，关闭它不会减少模型推理。调整后的速度、用量与回答质量仍需实际聊天验证。

## 账号与数据隔离

本页保留通用命令。含私人部署信息的 `ADMIN_COMMANDS.md` 仅保留在本地，不再跟踪 Git，新克隆仓库不包含它。

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

登录名为 3–32 位字母、数字、点、下划线或短横线，密码为 6–128 个字符。账号管理需要已有数据库，但不启动模型或后台任务。好友首次改密后可在通讯录自行创建角色，没有额外的新用户引导流程。停用账号保留数据并拒绝访问与后续执行；重新启用不会恢复旧登录。修改或重置密码、撤销会话都会使旧登录和通知订阅失效，需要重新登录并开启通知。「我」页面提供修改密码和退出登录。

HTTPS 部署保持 `COMPANION_WEB_SECURE_COOKIE=1`；反向代理时可将 `COMPANION_WEB_ORIGIN` 设置为实际 HTTPS origin（不带路径），供来源校验使用。该参数不会自动建立 HTTPS 或反向代理。只有本机 HTTP 开发显式设 `COMPANION_WEB_SECURE_COOKIE=0`。退出登录时清理页面、该账号会话缓存与待发送缓存。数据存储于部署者服务器，聊天内容按配置发送给模型提供商；账号隔离不提供端到端加密。

「我 → 今日用量」汇总当前账号全部角色的聊天、记忆整理、摘要和群聊协调调用，包含输入/输出及缓存 token、累计模型耗时和失败次数。统计只包含已记录调用，缺失 token 显示未上报，不视为零；输入 token 已包含其中的缓存部分。消息额度直接读取数据库，不依赖日志是否启用。

调用日志的文件日期与新写入的 `started_at` 都使用 `COMPANION_TZ_OFFSET_HOURS`（默认北京时间 `+08:00`），不依赖服务器系统时区。已有 UTC 日志文件不重写，查看接口与导出会统一转换时区；例如 `2026-11-02T16:35:00+00:00` 属于北京时间 11 月 3 日 00:35，归入 `2026-11-03.jsonl`。整理、摘要由服务器独立调度，关闭网页后仍可能处理先前积累的消息与事实变化。

默认每个账号每天最多接收 200 条新用户消息或手动群续聊请求，同时最多启动 2 个有效回复任务。默认值由 `COMPANION_DAILY_MESSAGES`、`COMPANION_CONCURRENT_REPLIES` 配置，单账号可通过上述命令覆盖。自然日按 `COMPANION_TZ_OFFSET_HOURS` 重置；已接收的请求即使模型失败仍计数，重复提交及失败回复重试不重复计算，重试的实际调用仍进入用量统计。同一会话的新输入可替换旧回复；主动联系同样受回复并发约束，整理与摘要沿用各自调度。

额度不足时不保存新消息、不取消原回复，前端保留未发送文本并提供手动重试，刷新或重连不会反复提交被拒消息。下调并发额度不终止已运行任务，待执行任务在有空位后继续。

## 群聊、引用与角色主动联系

会话容器与发言角色分开存储，群记录不会混入私聊历史。消息页右上角菜单提供「新建角色」「发起群聊」，通讯录也可发起群聊，每群选择 2–8 个已有角色；群气泡显示角色名区分发言者。群内输入 `@` 从底部选择成员，一次最多指定两位，按出现顺序回应并完全跳过协调模型；选中的提及作为整体删除。没有指定成员时，协调模型每轮最多调用一次，至少选择一位成员并返回有顺序的邀请名单。每位角色的全部气泡保存后才调用下一位，后者重新读取最新群历史。私聊的 `@` 是普通字符。

群内「继续聊聊」按钮可手动邀请成员续聊，不伪造用户发言；正在回复时不可重复触发，每次占用一条日额度。角色可通过同次 JSON 回复中的 `handoff: {"persona_id":"成员ID","message_index":0}` 交棒，程序校验后为指定气泡补上真正的 @，并将该消息作为下一位的直接触发来源。普通正文或引用中的 @ 不触发调度。用户指定顺序优先，已在队列中的成员只合并邀请；每轮最多尝试三位不同角色、最多四次模型调用，无交棒就结束。群设置面板支持点击面板外关闭。

主动联系以角色为主体：角色资料中的开关允许该角色获得发言机会；群聊右上角「⋯」的开关允许主动消息发到本群，默认关闭。自动任务共用每账号每天一次的机会，先检查空闲、免打扰、未读和并发，再让角色在一次 JSON 输出中选择去处并生成正文，不调用群协调助手、不交棒。没有合适理由可静默，不会每个扫描周期反复询问模型。

首版候选为私聊和最近活跃的一个允许主动联系的群，历史最多各取12条。有群候选时只提供该群历史与群内来源摘要，私聊历史、私聊专属画像和笔记不进入这次调用；仅有私聊候选时才提供私聊上下文。模型只能选择给定目标。发送前重新核对权限、群成员状态和目标是否出现新消息；消息与通知原子保存，按最终会话更新未读和推送，失败不改发另一个会话。主动消息仍进入对应会话的正常记忆整理链路。

群记忆沿用自动整理门槛，按「会话、角色」分别推进，失败只重试该角色；笔记仍只有 episode/fact，列表通过证据显示「来自群聊」。各角色 fact 继续进入原有用户画像与关系摘要链路。群整理与群摘要只使用本群来源；普通群回复可召回该角色本群和本人私聊范围内、证据来源完整的笔记，不因此读取其他角色私聊或其他群的笔记。普通群回复的交棒仍限定在本群，主动选路调用保持前述更窄的输入范围。

交棒与回复在同一事务中保存，失败重试沿用未完成队列。当前数据库 schema 为 v15，升级与备份说明见文末。

私聊和群聊都可长按已发送气泡（电脑右键或菜单键）选择复制、引用，支持引用自己的发言。引用预览最多两行，发送前可取消；发送后点击引用卡片定位原消息，再通过浮动按钮返回引用前的位置。引用原文由服务端按会话读取，不因聊天窗口截断而丢失；断线重发、失败重试保留引用与 @ 信息。

## 当前行为

- Web 界面里可以新建角色：选内置模板或自己填字段；角色资料页可以删除角色——删除只是停用，记忆和聊天记录都留着，能在「已删除的角色」里恢复。
- 「TA 的记忆」里可以直接编辑或删除笔记；编辑保留旧版本，删除退出有效记忆并保留聊天记录，fact 变更继续驱动后台摘要。
- 消息先保存，服务器独立生成；手机切后台后，返回页面可恢复正式回复。
- 「我 → 消息通知」可为当前设备开启或关闭 Web Push；普通私聊、群聊回复及主动消息均可在后台通过系统通知触达。完整气泡与待发通知一同保存，同一轮回复结束后合并为一条通知；已读、取消或用户继续发言时跳过。点击通知优先唤起运行中的页面并切换会话，未响应时再导航启动。
- 角色资料中的「主动联系」默认关闭；开启后在本地时区 10:00—22:00、距最近用户输入至少四小时且上一条主动消息已读时才考虑联系，每个用户每天最多安排一次。模型可以选择不联系。
- 主动提示词复用人设、行为规则、有限真实历史和两个成功摘要，专用说明在 `proactive_system.txt`；首版不从历史抽词检索，不伪造用户输入。正常聊天提示词不加入主动专用章节。
- 回复优先按 `<message_break>` 分气泡；没有分隔符时，纯文本空行兼容拆分，单换行与 Markdown 代码、列表、表格保留排版，最多三泡。
- 账号密码通过校验后建立服务器会话，再次打开保持登录；HTTP 与 WebSocket 按登录用户校验资源归属，写操作校验 CSRF，登录失败限流。
- 页面为脚本和样式附加内容版本号，部署后刷新即可获取新版本；访问凭据失效时返回登录页，清理当前账号页面与消息缓存。
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
- 固定笔记、分类 `type` 和 `persona_runtime` 设计已移除；笔记按相关性检索，已有证据、版本与关系继续保留。

旧笔记迁移时的 `reality` 保留为 `uncertain`，摘要可能因此显示“待核实”。这不表示任务尚未完成，也不会通过自动迁移猜测旧内容的现实情境。

## 当前记忆上下文与检索边界

| 内容 | 用途 |
|---|---|
| 近期聊天原文 | 承接当前话题与短期上下文，默认最多 50 条消息 |
| 跨会话实时原文 | 补充同角色在其他会话中、成功整理进度之后尚未沉淀的消息 |
| `episode` / `fact` | 保存经历与事实，关联来源证据、版本及笔记关系 |
| 用户画像与关系概况 | 从 fact 操作独立演化，成功版本持续用于聊天上下文 |

当前检索使用 FTS5、中文短词 LIKE 和有限 `note_link` 扩展，没有向量索引或语义重排。无笔记或无相关结果时，聊天仍使用人设、近期原文及可用摘要。第二层可以复用、补充已有笔记，也可以判定本批无需新增记忆；笔记整理不等于机械切分聊天记录。

触发任务不代表笔记已经生成：只有成功提交才推进整理进度，跨会话原文以成功进度为界。原文条数预算复用 `COMPANION_MEMORY_TRIGGER_MESSAGES`，最多 `min(40, 2 × 触发条数)` 条、正文共 4000 字符。`COMPANION_HISTORY_MAX` 控制模型近期历史长度，单位是消息条数，不是对话轮数；兼容项 `COMPANION_RECENT_MESSAGE_LIMIT` 不控制当前聊天历史长度。

已讨论的下一步是为 note 增加 Embedding 和 Rerank，尚未实现，本次更新不改变检索行为，模板不包含尚未生效的向量 API 参数。聊天原文继续保留为证据，不因后续检索升级而删除。

## 手机本地缓存

会话点击后立即进入，先展示手机本地已有消息，后台同步服务器最新记录；角色资料页先显示已有资料，后台更新字段。历史恢复不重播整屏气泡动画，新消息保留入场动画。同步失败保留内容并提供重试，群资料和消息并行读取。

浏览器 IndexedDB 保存最近 32 个会话的基本资料和每个最多 50 条完整消息，缓存条目 7 天未更新后过期，总量上限约 8 MiB；运行中同时使用内存缓存。私聊、群聊与账号分别隔离，退出登录时清理该账号缓存，删除角色时清理对应会话。存储不可用时退回内存，不影响正常联网聊天。记忆列表仍按需请求，未完成回复与登录凭据不进入消息缓存。界面缓存窗口与模型输入的 `COMPANION_HISTORY_MAX` 是独立限制。

首次启动仍需联网验证登录，首次打开未缓存的会话也需要读取服务器。缓存属于可淘汰的副本，不代替云端数据库备份；不同网站地址、浏览器或主屏幕应用可能使用各自的存储空间。

## 手机通知与主动联系

更新依赖后，在服务器 `.env` 设置 `COMPANION_PUSH_SUBJECT`，值为实际 HTTPS 网站地址或 `mailto:实际联系邮箱`；留空时服务器通知不开启。重启应用后，iPhone 从主屏幕打开同行，在每台设备的「我」中打开消息通知开关并允许系统权限，再通过下面的命令触发主动消息验证通知。网页已移除测试通知按钮。推送服务接受请求不等于手机已经显示，需实际验证后台、锁屏与点击跳转。打开会话仍需能访问部署的 HTTPS 入口。

推送密钥首次启用时自动生成，和设备订阅、通知队列一起存入现有私有数据库；备份数据库即可一并保留。通知失败只重试通知，不重新生成聊天。

服务器可单独触发主动联系测试，网页关闭后仍由运行中的服务执行：

```bash
# 在项目目录运行，检查命令不调用模型
.venv/bin/python -m companion_ever proactive groups --user alice
.venv/bin/python -m companion_ever proactive check --user alice --persona "角色ID"
# 测试会调用模型、写入聊天并尝试通知
.venv/bin/python -m companion_ever proactive trigger --user alice --persona "角色ID" --test --delay 15
# 用上面返回的任务 ID 查询结果：
.venv/bin/python -m companion_ever proactive status --task "任务ID"
```

`--persona` 必须指定获得机会的角色，模型执行时选择私聊或允许的群；加 `--conversation-id 群ID` 可将本次机会限定到该群。`--test` 跳过角色自动开关、免打扰和每日限制，并要求生成消息；限定群的测试也可绕过该群开关，角色权限、成员身份、待处理任务和用户插话检查仍生效。该角色需在私聊或允许的群里与用户有过互动。`--delay` 为 0—3600 秒，实际开始时间还受约15秒的扫描间隔影响。可加 `--request-id` 防止重复提交，相同 ID 返回原任务。`check`、`groups`、`status` 不触发模型；`trigger` 保存任务，由运行中的服务执行。`proactive status` 在完成后显示实际 destination，模型调用列表仍归聊天层，标为「主动聊天」。

## 代码阅读与本地开发资料

| 目录 | 内容 |
|---|---|
| [accounts](src/companion_ever/accounts/) | 登录会话、账号权限、用量与额度 |
| [characters](src/companion_ever/characters/) | 角色定义、模板与访问权限 |
| [conversations](src/companion_ever/conversations/) | 私聊、群聊、提示词上下文、生成任务与主动联系 |
| [memory](src/companion_ever/memory/) | 笔记、证据、关系、检索、整理、摘要及业务触发规则 |
| [notifications](src/companion_ever/notifications/) | 设备订阅、通知待发记录和发送 |
| [prompts](src/companion_ever/prompts/) | 聊天、群协调、主动联系、整理与摘要的提示词协议 |
| [entrypoints](src/companion_ever/entrypoints/) | Web/PWA 与 CLI 请求适配 |
| [runtime](src/companion_ever/runtime/) / [bootstrap](src/companion_ever/bootstrap/) | 后台运行、调度与应用装配 |
| [infrastructure](src/companion_ever/infrastructure/) | SQLite、模型客户端、轻量日志与推送传输 |

按项目约定，`docs/`、`tests/`、`tools/`、私人管理手册与部署交接文档只保留在本地，不跟踪 Git；新克隆的仓库不包含这些文件。本地架构说明、记忆规则和评测工具可继续用于开发，公开 README 不依赖它们才能说明启动方式。

## 验证与备份

更新已有 Linux 部署时，在项目目录先备份，再更新代码和依赖，最后按已有部署方式重启应用进程：

```bash
.venv/bin/python -m companion_ever maintain backup
git pull --ff-only
.venv/bin/python -m pip install -e .
```

保留服务器原有 `.env` 与数据库，按模板检查新增配置，不用示例配置覆盖私人配置。当前 schema v15，启动时执行需要的迁移；升级路径在改动数据库前创建一致性备份，旧代码不应直接打开更高版本数据库。

`maintain backup` 使用 SQLite 备份接口，将已提交的 WAL 内容一起保存，并检查完整性与外键；输出位于原数据库旁的 `.bak-...` 文件。服务运行时不要删除 `.db-wal`、`.db-shm`，也不要仅复制主 `.db` 作为完整备份。

Windows 本地维护与验证：

```powershell
.\.venv\Scripts\python.exe -m companion_ever maintain backup
# 以下脚本仅在保留了本地开发工具的工作区可用
.\.venv\Scripts\python.exe tools/check.py
Get-Content memory/calls/*.jsonl -Tail 10
```

测试使用临时数据库和假模型；前端脚本回归需要 Node.js，浏览器验收另需本地浏览器与 Playwright。常规验证不调用收费 API。`memory/` 保存私人数据库、备份、轻量调用记录和评测产物，`.env` 保存私有配置。旧 trace 的生成与查看逻辑已删除，已有历史文件保留，新记录位于数据库同级 `calls/`。严格提示词诊断仅在 `COMPANION_INVARIANT_STRICT=1` 时启用，字符预算检查不会自动裁剪输入。
