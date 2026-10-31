# 架构与重构方案

当前数据库 schema v6。应用通过 `build_app(AppConfig)` 装配；角色、会话、记忆业务及各自仓库归 `characters/`、`conversations/`、`memory/`，公共执行层归 `runtime/`，模型通信、数据库连接与诊断归 `infrastructure/`，装配与生命周期归 `bootstrap/`，HTTP/WebSocket、CLI、网页归 `entrypoints/`。LangGraph 承担记忆整理和摘要的步骤与条件循环，SQLite 任务层负责持久状态、租约和恢复。

## 业务边界与迁移方案（2026-10-31）

采用按业务模块组织的单体应用。继续使用现有 SQLite、模型客户端和两条后台图；重构以明确所有权和依赖为目的，不增加另一套调度、重试或图持久化机制。下文的目标目录分阶段迁入，未完成部分仍按后面的现行链路阅读。

| 模块 | 拥有的业务与数据 | 对外能力 |
|---|---|---|
| characters | 角色定义、用户的有效角色列表、创建/编辑/停用/恢复、角色访问检查 | 创建与读取设定、更新设定、列举有效角色、停用与恢复、校验访问 |
| conversations | 会话、参与者、消息、回复生成、取消、历史与已读、聊天上下文组装 | 接收消息、运行/恢复生成、查询消息与状态；向整理提供固定范围原话 |
| memory | note、证据、版本、关系、fact 流水、检索、整理与两个摘要 | 读写笔记、提供聊天记忆上下文、整理固定批次、演化摘要、判断业务触发条件 |
| runtime | 持久任务执行、尝试记录、租约/心跳、并发限制与故障分类 | 执行已有任务、恢复/续租；不决定消息或 fact 是否值得整理 |
| infrastructure | 模型通信、用量归一、数据库连接/事务、trace 存储 | 技术适配，不依赖 API、网页或某个业务服务 |
| entrypoints / bootstrap | HTTP/WS/CLI 输入输出；配置、装配、生命周期 | 接口只转换输入输出并调用用例；仅装配入口创建和连接服务 |
| domain | 跨模块共用的值对象与枚举（User、Note、Block、ConversationMessage、MemoryKind、LinkType、BlockType） | 只被多个业务模块引用的数据结构；无业务规则、无数据库访问 |
| utils | 横切工具：文本清洗与词法关键词抽取、时间格式化、DB UTC 转本地 | 纯函数工具；不含业务规则 |
| prompts | 运行时提示词模板（.txt） | 被记忆整理、摘要、角色编辑按名加载 |

目标目录为 `characters/`、`conversations/`、`memory/`、`runtime/`、`infrastructure/`、`entrypoints/` 与 `bootstrap/`，另有三个共享层：`domain/`（共享内核）、`utils/`（横切工具）、`prompts/`（提示词数据）。业务自己的仓库随模块放置；公共连接和迁移仍集中管理。简单模块无需再机械拆成 controller/service/repository/interface 四层。离线评测归开发工具，不作为应用服务参与装配。

依赖规则：

- 外部入口调用业务公开入口，不直接写 SQL、不创建服务、不启动业务工作线程。
- 聊天上下文可以读取角色配置及记忆的公开查询；记忆整理通过明确的原话读取能力获取输入，不反向依赖 ChatService 或 HTTP/WS。
- 业务规则可以使用 runtime 与基础设施。runtime 不依赖 AppContext，不引用具体业务服务；模块之间不借另一模块的连接查询其私有表。
- 共享 SQLite 下，固定批次、证据校验和提交快照需要在持久化适配器中关联原话与笔记；这些受控跨表操作保留在 Repo/Writer 的事务边界。外部业务通过明确的查询能力读取原话，不把提交期的来源校验移到事务外。
- 公共授权以 user.persona_ids 为有效角色依据；保留的 persona_definition 不能绕过停用。笔记归属检查在记忆业务内完成，角色模块不依赖 NoteRepo。
- 模型请求位于事务外；创建角色及生命周期变化、记忆提交、摘要提交各自保持原子性。任务状态和租约仍以数据库为准，LangGraph 只编排单次执行。
- 迁移一个模块后同步更新所有调用方并删除旧入口，不长期保留平行实现或转发壳。先用具体类和显式参数，仅在确有替换需求的边界引入接口。
- 共享层有准入边界：`domain/` 只放两个及以上业务模块共用的值对象与枚举；只属于单个模块的规则留在该模块（关系方向契约在 `memory/relations.py`，角色模板在 `characters/templates.py`）。`utils/` 只放无业务规则的纯函数工具。共享包不得变成新的垃圾桶。

三层调用保持原有行为：聊天完成使原话积累；记忆模块以消息数、空闲和最大等待判断整理；fact 流水以变化内容、数量和时间判断摘要。完成回调用于及时检查，持久流水与扫描用于补偿；不额外建立事件总线。重构不顺带修改提示词布局、模型调用预算、自动检索规则或摘要注入政策。

### 分阶段实施与验收

| 阶段 | 具体改动 | 完成标准 |
|---|---|---|
| 1. 角色边界 | PersonaService、PersonaEditService、角色仓库与角色授权归入 characters；设定不再经 NoteService 读写；创建事务化；停用保留数据、拒绝访问并使在途结果失效，恢复显式重新授权 | API/CLI/提示词均使用同一角色入口；跨用户隔离、创建回滚、停用/恢复、迟到结果保护通过测试；旧导入和 NoteService 设定方法移除 |
| 2. 会话执行边界 | ChatService、ConversationService、PromptService 及相关仓库归入 conversations；生成启动、恢复、取消从 Web 路由移入应用用例；路由只负责请求与订阅 | HTTP/WS/CLI 共用接收和执行语义；断线恢复、防重、插话、已保存气泡及已读逻辑不变 |
| 3. 记忆边界 | 整理/检索/摘要及其仓库、图按功能归入 memory；用例与 SQL 分开；任务触发条件归记忆业务，后台扫描改为明确入口 | 两条图、工具权限与调用上限不变；原子提交、过期租约、固定批次恢复及 6 组已认可跨轮契约通过 |
| 4. 执行与入口收敛 | 公共执行代码归 runtime；模型/连接/诊断归 infrastructure；装配与生命周期收敛；离线评测移出生产 services；清除剩余旧目录依赖 | 每个状态只有一处所有者；业务不依赖 Web；装配不需要回填内部服务字段；全量回归通过，无重复框架或兼容层 |

每阶段先保持可运行，再检查剩余依赖，不进行一次性全量搬家。以 `tools/check.py` 的现有脚本集、已确认的跨轮记忆契约为回归基线；新增测试聚焦事务、撤权和模块边界的真实风险。默认使用临时数据库与假模型，不访问正式 memory 数据、不产生模型费用。目录迁移本身不变更数据库；确需约束或结构变更时单独版本化，明确重复数据处理与回滚策略。

### 群聊与手机端的预留边界

角色是可复用的 AI 身份，会话是消息容器，参与者记录谁在其中。未来 conversations 增加会话类型与成员，统一以 conversation_id 查询历史，并负责群聊回复策略；characters 负责创建和编辑角色。真实发送者由 sender_kind/sender_id 表达，不能用模型协议的 user/assistant 代替参与者身份。当前会话仍绑定 user/persona，群聊结构尚未实现，本轮不新增空表或群聊接口。

memory 需要显式区分私聊与群聊的来源、可见范围和关系对象。现有用户画像与关系摘要继续按用户/角色隔离，不因目录重构自动全局共享记忆。启用角色与加入会话也必须是两个不同操作。

未来 `mobile/ios` 使用同一 Git 仓库、独立原生工程，在具备签名安装验证条件后创建。Python 与 Swift 复用业务契约、提示词协议、评测样本及数据含义，不假设可直接复用 Python 服务或 LangGraph 运行时。完成关键边界后尽早验证原生安装、聊天和后台恢复，不以重写全部 Python 代码为手机开发的前置条件。

实施状态：阶段 1～4 已全部落地。目录迁移后旧 `services/`、`storage/`、`api/`、`cli/`、`llm/`、`workflows/`、`web/` 已删除，不保留转发壳；离线评测归 `tools/evaluation/`，不参与应用装配。下文保留现行交互链路，已同步更新路径与职责。

### 已落地的角色入口与一致性约定

- `characters/service.py` 的 PersonaService 是配置与生命周期入口；`characters/editing.py` 的 PersonaEditService 只读取和更新该入口，模型调用在事务外。
- `characters/access.py` 的 Authorizer 只依赖用户有效角色列表，不依赖 NoteRepo，不按留存设定兜底授权。笔记归属与角色有效性共同由 NoteService 检查；NoteService 的设定读写方法已删除。
- API 总览、CLI 展示及 PromptService 统一从 PersonaService.get_definition 读取设定。旧 services/persona_*、services/authz 和 storage/persona_repo 路径已删除；共享 JSON 响应解析移至 infrastructure/llm/json_output.py，角色编辑不反向依赖业务模块。
- create_persona 在一个事务中注册角色、写设定、初始化两个空摘要；停用的同名角色必须通过 restore 恢复，不能以重新创建覆盖旧设定。
- deactivate 在同一事务中移除有效角色、递增设定 revision、取消待执行/在途聊天，并使待执行、在途和待重试记忆/摘要任务失去租约。已保存的气泡、历史、笔记、设定和摘要保留，原先已经终止的失败任务不重置。
- restore 显式重新授权，不清空任何内容、不重发聊天；仅因本次停用中止的记忆/摘要固定范围重新排队，后续正常扫描可用新租约恢复，旧 token 始终不能提交。模型辅助编辑使用捕获的 revision 作提交前校验，停用后立即恢复也拒绝迟到编辑。
- 角色模块通过装配时传入的明确事务钩子初始化摘要、失效或重新排队任务，不导入记忆/会话实现，不建立通用事件总线。钩子全部使用同一数据库连接、禁止模型或网络调用；任一失败回滚整个生命周期操作。
- schema v6 添加 persona_definition.revision 与有效设定唯一索引。旧库只增加字段与约束，不删除、归档或猜测选取设定；发现重复有效行时回滚 v6 步骤并报告，需先人工确认重复设定。可用 tools/verify_migration.py 在只读源库的临时副本上验证。

验收入口：tests/test_persona_service.py 覆盖跨用户隔离、创建/停用失败回滚、直接笔记访问撤权、保留恢复、三层模型迟到结果与编辑竞争、旧租约拒写、v5→v6 迁移和重复约束回滚。其他交互链路继续由完整脚本集验证。

### 已落地的会话入口与执行约定

- `conversations/chat.py` 的 ChatService 保留接收、防重、提示词调用、分条保存及同步聊天语义；`messages.py` 查询原话，`context.py` 组装聊天输入，两个仓库分别负责消息和 generation 数据。角色、召回与摘要仍通过已注入的业务查询访问，提示词内容和预算不因移动而改变。
- `conversations/execution.py` 的 GenerationService 是后台执行入口：submit 接收并启动、snapshot/latest 查询、retry 显式重试、cancel 取消未完成输出、recover 恢复 queued 并标记过期 running。它只依赖会话对象、设置及公共事务，不依赖 FastAPI、WebSocket、AppContext 或页面事件循环。
- HTTP 与 WS 共用同一个 GenerationService；调试 CLI 直接调用同一个 ChatService，不再编译聊天图。应用装配时直接注入 SessionCloseService，不再为聊天先传 None 再回填。LangGraph 只用于两条记忆图。
- 聊天输入没有退出或整理命令，`/quit`、`/exit` 均按普通文本保存并回复；调试终端在 EOF 或 Ctrl+C 时退出，不写会话标记、不强制整理。Web 宿主与 CLI 共用 bootstrap 的 ApplicationLifecycle：两者都启动同一套扫描与恢复，退出时以 close() 停止，不以退出补偿宿主调度。
- 回执返回前已启动执行；重复 queued 回执也会触发恢复，避免输入已保存但原调用方尚未启动线程。进程内执行注册表合并同一 generation 的重复启动和在线订阅，SQLite claim 仍是执行权依据，不引入另一份持久状态。
- 在线订阅合并限于同一应用实例；其他进程中正在执行的回复通过持久快照恢复，不承诺跨进程实时转发。没有为此新增消息代理。
- 订阅回调不控制任务：投递异常、页面切后台或取消订阅只停止投递，生成继续并保存完整气泡。新输入和显式 cancel 才取消未完成输出；已保存消息保留。每个订阅者收到独立事件副本，不能修改其他订阅者的结果。
- stream_filter.py 负责正文增量过滤，reply.py 负责正式正文/思考拆分；可投递事件不包含 raw 模型原文。快照与流式事件均遵守 show_thinking 设置，隐藏仅影响投递，不删除思考存档。HTTP/WS 的时间字符串格式转换留在传输层，不回写业务事件。
- entrypoints/web/routes/chat.py 只为会话操作做请求转换、错误映射与 WS 订阅投递，不创建生成线程、不查询生成表、不直接访问 GenerationRepo。记忆任务查询/重试已归 entrypoints/web/routes/memory.py 并调用记忆用例。
- 未读统计 SQL 归 ConversationRepo，授权入口为 ConversationService.unread_count；路由传入本机已读游标并返回结果，不改前端的可见/到底判断或跨设备政策。
- 恢复不重发 expired running：失败保留来源和部分完整回复；无部分回复时允许显式 retry，使用新 generation_id 拒绝旧写入。不同用户/角色不能查询、重试或取消别人的 generation。

验收入口：tests/test_conversation_execution.py 无 Web 验证订阅断开不影响生成、重复启动仅调用一次模型、排队回执恢复、原文/思考投递边界、跨角色拒绝、取消保留已存消息、显式重试和过期生成拒绝静默重发；test_chat_reconnect.py、test_durable_flow.py 继续验证真实 HTTP/WS 的断线、丢回执、防重与插话。

### 已落地的记忆边界与调度约定

| 目录 | 内容与职责 |
|---|---|
| memory/notes | NoteService、NoteRepo、NoteLinkRepo：显式笔记操作、固定记忆、版本字段与关系持久化 |
| memory/retrieval | RetrievalService 与 MemorySearch：词法种子、关系扩展、排序与公开只读检索契约 |
| memory/extraction | ExtractionService、Draft、两个只读工具、MemoryWriter、MemoryJobRepo、SessionCloseService 与整理图 |
| memory/summaries | SummaryService、操作合并、BlockService、BlockRepo、SummaryRepo 与摘要图 |
| memory/scheduling.py | MemoryScheduler：消息积累、空闲、最大等待、fact 净变化、来源失效优先级等触发规则 |
| tools/evaluation | 离线单批/跨轮评测，依赖生产模块验证契约；生产模块不反向导入它 |

- 两条图随业务迁入 extraction/workflow.py 与 summaries/workflow.py，节点名、预算、只读工具范围、一次纠错和整批恢复语义保持原样。旧 workflows 包、旧 services 记忆入口和旧 storage 记忆仓库已移除，不保留转发壳。
- 业务服务、调度和记忆路由不执行 SQL。笔记/证据/固定记忆查询归 NoteRepo；任务范围、恢复范围与状态归 MemoryJobRepo；fact 流水、摘要进度与结果持久化归 SummaryRepo。MemoryWriter 保留提交期校验和跨笔记原子写入，SummaryService._commit 保留事务中的快照校验，通过 SummaryRepo.save_result 保存 checkpoint 与任务成功。
- MemoryScheduler 显式接收配置、授权、会话查询、整理执行和摘要服务；不持有 AppContext。会话通过 pending_input 提供消息计数、时间和生成状态，任务仓库提供整理进度。已有阈值、空闲与最长等待条件不变，不为决定触发额外调用模型。
- SessionCloseService.resume_pending 与 SummaryService.resume_pending 恢复已有固定范围；是否创建新工作由调度判断。关闭自动触发仍恢复显式任务，永久失败不被普通入队重置。成功回调重新检查摘要，定时扫描兜底，继续共用原 TaskRuntime、尝试记录和后台模型名额。
- 记忆 API 只校验传输输入并调用 recent_jobs/status/retry_job/fact_changes/task_attempts 等授权用例；不直接查询连接或仓库，不创建摘要服务。HTTP 路径、响应字段、跨角色 404、无权角色 403 和不可重试 409 保持原有语义。
- build_app 一次性构造摘要服务、图、调度器及整理完成回调，PromptService 在构造时获得摘要查询。API 总览和摘要状态不会补建另一个服务或替换图；测试装配同步遵守该约定。
- 离线评测从 services 移到 tools/evaluation，现有 evaluate_memory.py/evaluate_retrieval.py 入口与夹具不变。模型 payload 不包含评测预期。评测结果与提示词版本绑定：responses 记录 prompt_sha256（模板文本 + 载荷键集合），提示词改了而复用旧响应会被拒绝。

验收入口：tests/test_memory_boundaries.py 约束模块导入方向、SQL 所有权和调度无容器依赖，并验证诊断查询隔离、既有维护路由、重试、停用撤权和装配实例复用。test_cost_aware_schedule、test_memory_graph、test_summary_evolution、test_task_runtime 与六组跨轮契约分别验证触发成本、真实图、有界纠错、原子回滚和旧执行拒写。

### 已落地的记忆整理 Agent 协议

记忆整理是 LangGraph agent（`load_input → prefetch → model → validate → {tools|repair|commit}`），不是一次性调用。模型每一轮收到的内容分两层：

| 层 | 内容 | 何时变化 |
|---|---|---|
| 案例 | id、new_message_ids、required_user_message_ids、relation_rules、messages（本批原话） | 整个任务不变，**只在首轮发出一次** |
| 进展 | round、existing_notes（合并后的完整候选）、tool_results（只含本轮）、requires_search_before_write、remaining_tool_rounds、remaining_tool_calls、available_tools | 每轮变化，**形状固定** |

- 静态协议全部在 `prompts/memory_draft_system.txt`（可用工具、空召回规则、调用预算、输入载荷说明），Python 只填数据。工具说明、预算、空召回规则不再硬编码在 `draft.py`/`service.py` 里。
- `existing_notes` 每轮完整回灌合并后的候选清单，使"target_id 必须引用候选笔记"在每一轮都成立；候选投影带 `time_basis`/`revision`/`is_archived`，模型不必自己编时间依据、也能看出笔记是否已归档。
- `requires_search_before_write` 的语义是"必须成功执行过一次 search_notes"——只调 get_note_context 不解禁，工具报错也不解禁。
- `tool_results` 只含本轮执行的结果；上一轮的结果仍在上一条消息里，`call_id` 跨轮连续编号。
- `MAX_INPUT_CHARS` 是护栏而非目标，必须高于批次/候选/工具结果各上限之和，否则合法输入会硬失败——超预算抛 ValueError、不可重试，那一批消息就永远不会被整理。

验收入口：tests/test_memory_payload_contract.py 逐轮核对形状、案例只发一次、候选回灌、工具结果只含本轮、关系规则与代码一致，以及各上限允许的最坏情况装得进预算。

### 已落地的摘要演化协议

摘要演化没有工具循环（`load_input → decide → {reuse|clear|model} → validate → {repair|commit}`），所以只有一种载荷形状，不需要像整理层那样分「案例/进展」：

| 载荷键 | 内容 |
|---|---|
| previous_summaries | 上次提交的摘要（受 schema 上限约束） |
| changes | 按序合并后的事实操作记录（≤24 条 / 12000 字符） |
| current_facts | 有效事实正文，受单条与总量预算约束（引用最多 160 字，故每条至少留够该长度） |
| unavailable_note_ids | 旧摘要引用过或本批涉及、但已失效（删除/归档/取代）的事实 ID |
| output_limits | 各输出字段的最大条数 |

- 载荷的每个键都在 `prompts/summary_system.txt` 的 `## 输入` 节有交代 —— 模型不该收到不认识的键。`unavailable_note_ids` 尤其重要：它告诉模型哪些旧结论的依据已经没了。
- 纠错用 `prompts/repair_citation.txt`，与 `## 引用` 节的规则互补：首次生成时讲规则，重问时再点一次"这条不能松"。
- 摘要预算同样是护栏：MAX_PAYLOAD_CHARS / MAX_INPUT_CHARS 高于 schema 与 change_window 允许的最坏情况。

### 三层提示词的共同约定

- 规则型模板统一 `##` 小节 + `-` 列表，章节标题与 `【X】` 引用同源；`tests/test_prompt_structure.py` 守住风格、引用可解析、占位符已知、小节顺序固定。
- 载荷的每个键都要在对应模板里有交代：整理层的 12 个键、摘要层的 5 个键全部有覆盖。
- 行为规则不留在 Python 里。聊天侧的"旧摘要不能否定用户最新陈述"在 `chat_system.txt`，不由摘要服务塞进正文。
- 聊天模型看到的笔记元数据有词汇表：`kind`（fact 长期事实 / episode 具体经历）、`reality`（user_report 自述现实 / shared_fiction 共同想象 / uncertain 待核实）、`event_time`、`importance` —— 不解释的话模型只能猜。

### 已落地的执行与入口收敛约定

| 目录 | 内容与职责 |
|---|---|
| runtime | tasks.py（TaskRuntime：名额、租约、心跳、执行、停止）、repository.py（TaskRepo 与尝试记录）、structured.py（结构化输出与纠错）、invariants.py（提示词/预算不变量）、errors.py（故障分类） |
| infrastructure | database/（连接、事务、schema 与迁移）、llm/（协议客户端、用量归一、JSON 解析）、tracing.py（trace 落盘） |
| bootstrap | app.py（build_app 装配）、lifecycle.py（ApplicationLifecycle：启动、扫描、停止） |
| entrypoints | web/（FastAPI 路由、schema、PWA 静态资源）、cli/（setup、chat、show、备份、trace 命令） |

- 公共执行代码不再放在业务模块里，但执行机制与业务判断的分工不变：TaskRuntime 只负责名额、租约、心跳、重试分类和停止，"什么值得记住"仍由 memory 的调度规则决定。
- runtime 不依赖 AppContext，也不 import 任何业务模块；infrastructure 只做技术适配，不引用 API、网页或业务服务。两者都不创建业务服务。
- ApplicationLifecycle 是唯一的启动/扫描/停止入口。Web 在 FastAPI lifespan 中调用 ctx.lifecycle.start()，CLI 在 cmd_chat 中调用同一个 start()，两者退出时都经 AppContext.close() 停止。Web 不再有 _sweep_services/_session_sweeper 私有扫描，CLI 也不再直接关闭数据库。
- 一次扫描按"回复恢复 → 记忆调度 → 空闲边界"顺序执行，各链路单独捕获异常：单条链路故障不阻断其他恢复入口。每轮扫描结束关闭当前线程连接。
- 停止时先关闸再等待：generations.shutdown() 与 tasks.shutdown() 在返回前完成 fence，运行中的旧执行不能在重启后写回结果；线程 join 有上限，不等待模型返回。
- 装配集中在 build_app：仓库、模型客户端、业务服务、工作流和生命周期都在此连接一次。角色生命周期钩子以显式回调注入，不在装配后回填服务内部字段。
- 目录迁移不改变数据库结构、提示词、模型调用预算、自动检索规则或摘要注入政策。旧 services/、storage/、api/、cli/、llm/、workflows/、web/ 已删除，不保留兼容层或转发壳。
- 共享层同步收口：domain/errors.py（零引用占位符）与 domain/entities.py 的 PersonaBasics（角色设定改由 persona_definition 承载后遗留）已删除；domain/memory.py 的关系契约归 memory/relations.py，domain/persona_templates.py 归 characters/templates.py。domain/ 只剩跨模块共用的值对象与枚举。
- 提示词协议同步收口：聊天与记忆整理的静态协议（章节标题、行为指引、工具说明、调用预算、空召回规则、载荷说明）全部收进 `prompts/` 的 .txt 模板，Python 只填数据。聊天侧章节标题与 `【X】` 引用同源于一个模板，记忆整理侧每轮载荷形状固定、案例只发一次。详见[记忆整理 Agent 协议](#已落地的记忆整理-agent-协议)与 `prompts/chat_system.txt`。

验收入口：tests/test_application_lifecycle.py 覆盖排队任务恢复、停止后重启、旧回复与旧记忆任务在重启后不能写回（换新租约）、停止后仍等待的旧执行被中断、close 幂等与模型客户端只关闭一次，并以依赖方向守卫约束 runtime/infrastructure 不反向依赖业务模块；test_task_runtime.py 覆盖心跳失败、租约抢占、丢租约拒写、进程重启与迁移回滚；test_background_recovery.py、test_durable_flow.py 覆盖断线恢复与自动整理；test_web_assets.py 与 tools/check.py 覆盖新目录下的网页资源与整体回归。

## 阅读方法与顺序

先看 `src/companion_ever/bootstrap/app.py` 的 build_app，了解服务如何连接；随后按一条用户操作从入口跟到结果，再检查失败分支。每经过一个函数，记录：输入是什么、读了什么、写了什么、何时算成功、失败后由谁恢复。以下路径默认相对 `src/companion_ever/`。

| 顺序 | 链路 | 完成这轮阅读时应能解释 |
|---|---|---|
| 1 | 聊天正常完成 | 用户输入何时落库，模型收到什么，回复何时成为正式消息 |
| 2 | 聊天断线、重发与插话 | 丢连接后谁继续执行，如何防重、恢复和拒绝旧结果 |
| 3 | 聊天记录整理为 note | 哪些原话进入本批，如何决定新增/确认/修改，如何原子写入 |
| 4 | fact 操作演化为摘要 | 增删改如何影响摘要，摘要何时可重新进入聊天 |
| 5 | 后台公共运行层 | 谁触发、如何续租，永久失败与可恢复故障如何区分 |
| 6 | 其他入口与迁移 | 角色编辑、固定记忆、历史查询及旧库兼容是否遵守同一范围约束 |

提示词集中在 `prompts/`，按场景分文件：`chat_system.txt`（聊天）、`memory_draft_system.txt`（记忆整理）、`summary_system.txt`（摘要演化）、`repair_evidence.txt` 与 `repair_citation.txt`（按场景区分的纠错来源约束）。规则型模板统一用 `##` 小节 + `-` 列表，章节标题与引用同源。回归入口为 `tools/check.py`；仍需讨论的问题见 [REVIEW_NOTES](../REVIEW_NOTES.md)。

## 三条业务链

**聊天：** HTTP/WebSocket → GenerationService.submit → ChatService 接收用户消息和 generation → 会话执行线程 → PromptService 组装设定、上次成功摘要、近期聊天和检索记忆 → 模型 → 逐条保存完整助手消息 → 订阅推送或 snapshot 恢复。CLI 同步调用同一个 ChatService。

**长期笔记：** MemoryScheduler → SessionCloseService → ExtractionService 领取固定范围任务 → LangGraph 加载/预取/模型/工具/校验 → MemoryWriter 同事务提交 note、证据、版本、关系及任务成功。

**派生摘要：** fact 写入触发操作流水 → 独立 summary_job → 上次摘要、操作差量及相关有效 fact → 模型整理/校验 → 两个摘要、引用、checkpoint 和任务成功同事务提交。

模型调用在事务外。两种后台任务共用一个模型执行名额，取得名额后才领取租约；聊天独立执行。摘要不反写 note，避免摘要自证循环。

| 职责 | 当前唯一实现 | 边界 |
|---|---|---|
| 应用装配 | app.build_app | 各建一个 ChatService、GenerationService、ExtractionService、SummaryService；调试 CLI 与后台生成复用同一个 ChatService |
| 何时触发 | MemoryScheduler、SessionCloseService | 代码判断阈值/时间/净变化，完成回调与扫描共用判断；不调用 LLM 决定调度 |
| 一次任务内部步骤 | memory/extraction/workflow.py、memory/summaries/workflow.py | 有界模型/工具/纠错，图状态仅后端使用 |
| 写入与恢复 | MemoryWriter、SummaryService._commit、SQLite jobs、TaskRuntime | 事务、租约、心跳与恢复继续共用原实现；图不另建持久状态或重试层 |
| 模型与诊断 | OpenAICompatibleClient、TracedLLMClient、TraceStore | 一处适配协议，一处归一用量；不硬编码价格，不将估算充当实际计数 |
| 开发评测 | 现有五个 tools 入口、tools/evaluation/memory.py、flows.py | 临时库与虚构输入；预期只用于本地检查，不进入模型输入或运行时提示词 |

旧摘要单函数执行路径及 CLI 聊天图均已移除，没有新增数据库或模型客户端。

### 记忆图编排设计与当前实现

记忆整理已从单一 extract 节点拆为 load_input → prefetch → model → validate，validate 分支到 tools、repair 或 commit；tools/repair 回到 model，commit 后结束。模型可直接返回原 Draft，也可返回严格的 JSON tool_calls 请求；这是现有文本客户端上的明确工具协议，不依赖服务商原生 function calling。

search_notes(query,kinds,limit) 和 get_note_context(note_id) 只读取服务端绑定的用户/角色范围。工具读取的新候选加入本次校验集合及版本快照，关系写入仍由 MemoryWriter 统一原子提交。工具查询本身不强化访问次数、不修改事实。陌生/越权 ID 不返回内容，过大的结果明确报告而不截断证据。

库中有未归档笔记、但预取候选为空时，不能把空召回当作没有旧记忆；任何保存建议都须先完成一次 search_notes。该要求同时出现在模型输入和校验中，无记忆的 skipped 结果不强制检索。其余场景由模型判断是否补查。检索仍为词法检索，不能保证消除所有语义重复。

单次执行最多 2 轮工具、4 次只读调用、4 次模型调用（含最多一次校验纠错），共用 3×整理超时的截止时间。无新记忆仍校验跳过理由并完成原任务；无本批用户消息时可直接完成。所有调用与节点、工具输入/结果分别进入已有 trace。

ExtractionService 负责领取任务、租约/心跳、执行图和记录结果，图负责步骤与条件循环；App 与 SessionCloseService 共用一个服务实例。现有 SQLite 任务/事务继续作为业务状态依据，不新增图检查点或后台重试层。聊天主路径与调试 CLI 直接使用 ChatService。

正常业务入口为 ExtractionService.run；ctx.workflows.extraction 是该服务内部的编译图，执行所需的已领取 job、截止时间和运行上下文由 run 提供。进程退出后重新执行同一固定批次，不承诺从模型/工具节点中间续跑。

## 1. 聊天：从发送到正式回复

主线：`send → ws_chat / receive_message → GenerationService.submit → ChatService.receive → GenerationRepo.receive → GenerationService._run → reply_events → PromptService.build → LLM → save_message → handleWs`。

| 阶段 | 代码入口 | Review 问题 |
|---|---|---|
| 点击发送 | [web/app.js](../src/companion_ever/entrypoints/web/static/app.js) 的 send、readOutbox、writeOutbox | client_message_id 是否稳定？临时气泡是否标记 pending？断线后是否仍有可重送的内容？ |
| 接收与授权 | [api/routes/chat.py](../src/companion_ever/entrypoints/web/routes/chat.py) 的 ws_chat、receive_message；[conversations/chat.py](../src/companion_ever/conversations/chat.py) 的 receive | WS 与 HTTP 是否使用相同用户/角色范围、校验和业务接收入口？ |
| 输入落库 | [conversations/generation_repository.py](../src/companion_ever/conversations/generation_repository.py) 的 receive | 用户消息与 queued generation 是否同事务保存？重复请求是否返回同一记录？相同 ID 不同正文如何处理？ |
| 后台执行 | conversations/execution.py 的 submit/_start/_run；ChatService.reply_events | 执行是否先于确认启动？重复执行能否成功 claim 两次？页面断开后是否仍有执行者？ |
| 构造上下文 | [conversations/context.py](../src/companion_ever/conversations/context.py) 的 build；[memory/retrieval/service.py](../src/companion_ever/memory/retrieval/service.py) 的 search | 当前输入是否恰好一次？历史是否截止到本次输入？固定笔记、角色设定、相关记忆和上次成功摘要分别从哪里来？ |
| 模型通信 | [llm/openai_client.py](../src/companion_ever/infrastructure/llm/openai_client.py)、[traced_client.py](../src/companion_ever/infrastructure/llm/traced_client.py) | 实际 messages 是否与预期一致？思考字段、空输出、截断流与异常如何处理？ |
| 分条与保存 | ChatService.reply_events；[conversations/reply.py](../src/companion_ever/conversations/reply.py)；GenerationRepo.save_message | 分隔符跨分片时是否正确？正文与思考是否分开？每条完整回复是否只保存一次？已取消/过期生成是否仍能写入？ |
| 前端展示 | app.js 的 handleWs、renderChat、markVisibleChatRead | delta 是否只是草稿？message_done 是否替换为正式 ID？完成消息是否去重？可见且滚到最新时才标已读吗？ |

几个事件的含义：received 确认用户消息已接收；delta 是生成中的临时内容；message_done 对应已保存的完整助手消息；done 表示这次生成结束。Review 要分别核对这些时刻与实际数据库状态。

PromptService 保留原结构：时间、角色设定、召回资料、行为规则及上次成功摘要组成 system，历史以原生 role 消息出现，当前输入恰好一次。按用户决定，暂停布局降费试验；本轮为试验引入的额外参考消息包装已回退，保留实际缓存用量记录。

聊天按当前 query 召回，不按“后来呢、那次”等词自动附加最近用户原话。短指代由现有聊天历史承接；底层 MemorySearch 的 context_query 仍可由明确调用方提供。词法改述漏召回仍待更大样本验证。

核心持久数据：conversation_log、generation、thinking_log。对应测试：`tests/test_chat_service.py`、`test_durable_flow.py`、`test_thinking_split.py`、`test_web_ws_flow.py`、`test_web_unread.py`。

## 2. 聊天异常：断线、重发、插话和重启

沿聊天主线分别替换一个条件，检查状态变化及恢复入口：

| 情况 | 重点代码 | 应核对的结果 |
|---|---|---|
| 服务端已收消息，received 丢失 | app.js 的 flushOutbox/recoverReply；GenerationRepo.receive | 沿用原 client_message_id 重送，不新增用户消息或重复生成 |
| 页面切后台，WS 断开 | conversations/execution.py 的 _run；api/routes/chat.py 的 _stream_reply；app.js 的 visibilitychange/pageshow、recoverReply、loadHistory | 推送订阅结束后生成继续；回前台通过 HTTP 恢复已保存消息，旧历史响应不覆盖新回复 |
| 回复 A 尚未结束，又发送 B | GenerationRepo.receive、active、save_message；handleWs | A 的已保存消息保留，未完成草稿取消；A 的迟到事件不能覆盖 B |
| 模型失败后重试 | GenerationRepo.retry；app.js 的 retryReply | 复用原用户消息，新执行 ID 隔离迟到回调；已有部分正式回复时不静默重生成 |
| 服务进程重启 | [bootstrap/lifecycle.py](../src/companion_ever/bootstrap/lifecycle.py) 的 start/tick；GenerationService.recover、GenerationRepo.recover | queued 可继续执行；过期 running 明确失败，已保存回复仍可查 |

聊天 generation 的状态为 queued → running → completed / failed / cancelled。这里的恢复政策与后台记忆任务不同，分别检查。

对应测试：`tests/test_chat_reconnect.py`、`test_web_reconnect.py`、`test_web_ws_flow.py`、`test_durable_flow.py`。读测试时查看它制造了什么故障、断言了什么状态；仅正常返回的断言不足以验证恢复。

## 3. 记录整理：从原话到 note

主线：`定时扫描 → MemoryScheduler.consider_memory → close_session_background → memory_job → ExtractionService.run → load_input → prefetch → model → validate ↔ tools/repair → commit → MemoryWriter.apply → record_done`。

| 阶段 | 代码入口 | Review 问题 |
|---|---|---|
| 自动触发 | [bootstrap/lifecycle.py](../src/companion_ever/bootstrap/lifecycle.py) 的 tick；[memory/scheduling.py](../src/companion_ever/memory/scheduling.py) 的 tick/_memory | 消息阈值、空闲条件、未完成任务和正在生成的聊天是否正确处理？ |
| 固定批次 | [memory/extraction/jobs.py](../src/companion_ever/memory/extraction/jobs.py) 的 enqueue | 起点来自哪里？范围是否固定？新消息是否留给后续批次？超长单条是否完整保留？ |
| 启动与连续处理 | [memory/extraction/sessions.py](../src/companion_ever/memory/extraction/sessions.py) 的 close_session_background/_run | 有积压时谁继续下一批？marker 与成功进度是否分开？ |
| 图与准备材料 | [memory/extraction/workflow.py](../src/companion_ever/memory/extraction/workflow.py)；[memory/extraction/service.py](../src/companion_ever/memory/extraction/service.py) 的 _load_input/_prefetch | 本批新消息、参考前文和候选是否明确区分？候选检索是否受角色范围、数量和字符预算约束？ |
| 生成建议/请求工具 | _model；[memory/extraction/draft.py](../src/companion_ever/memory/extraction/draft.py) 的 draft_messages；[memory_draft_system.txt](../src/companion_ever/prompts/memory_draft_system.txt) | 模型能提哪些动作？Draft 与工具请求是否互斥？跳过和不确定内容如何表示？ |
| 只读工具 | _tools；[memory/extraction/tools.py](../src/companion_ever/memory/extraction/tools.py) | 模型能否改变授权范围？补查的正文/证据/关系是否完整可核查？新候选是否进入校验快照？超量与循环如何停止？ |
| 校验和纠错 | _validate/_repair；validate_draft；[structured.py](../src/companion_ever/runtime/structured.py) 的 correction_messages | 引文是否逐字属于本批用户原话？用户新消息是否覆盖？目标/关系是否合法？纠错与工具交互是否共享预算？ |
| 原子写入 | [memory/extraction/writer.py](../src/companion_ever/memory/extraction/writer.py) 的 apply | 是否重新核对实际来源、候选版本和 token？任一条失败时，笔记/证据/关系/成功状态是否全部回滚？ |
| 更新页面 | chat.py 的 notify_record_done；app.js 的 record_done | 通知来自已提交结果吗？多批处理中是否被误报为全部完成？漏推送后是否仍能重新查询？ |

写入器重点跟四个分支：create 的事实精确防重、confirm 追加证据、append 保留经历旧版本、revise 创建新事实并建立 supersedes。核对同一批的多条建议最终是否意外落在同一个 note ID。

核心持久数据：memory_job、note、note_evidence、note_revision、note_link。fact 写入还会产生操作流水，连接下一条链路。

对应测试：`tests/test_memory_graph.py`、`test_kind_storage.py`、`test_extraction_guards.py`、`test_memory_pipeline.py`、`test_background_service.py`。其中图测试实际走检索→读证据/关系→确认旧 fact+新 episode→原子写关系，另测范围、调用预算、空召回必查、超大结果与并发变更；原子回滚测试会在任务标成功后注入异常，检查整批仍被撤回。

## 4. 摘要演化：从 fact 操作回到聊天上下文

主线：`fact 写入事务 → fact_change → MemoryScheduler.consider_summary → summary_job → SummaryService.run → load_input → decide → reuse/clear/model → validate → commit → summary_updated → PromptService.build`。validate 失败可经 repair 回到 model，仅一次纠错、最多两次模型调用，共用总时限。

| 阶段 | 代码入口 | Review 问题 |
|---|---|---|
| 记录操作依据 | [summary_schema.sql](../src/companion_ever/infrastructure/database/summary_schema.sql) 的触发器 | 新增、修改、删除、归档、证据和替代变化是否都记录？事务回滚是否也撤回流水？硬删除后是否还有撤回依据？ |
| 决定何时整理 | MemoryScheduler.consider_summary/memory_settled；[memory/summaries/repository.py](../src/companion_ever/memory/summaries/repository.py) 的 change_window/enqueue | 首次、实质变化阈值、等待时间、被引用事实失效如何触发？原始操作很多时是否仍推进？是否与同角色记忆任务错开？ |
| 图与增量输入 | [memory/summaries/workflow.py](../src/companion_ever/memory/summaries/workflow.py)；[memory/summaries/changes.py](../src/companion_ever/memory/summaries/changes.py)；[memory/summaries/service.py](../src/companion_ever/memory/summaries/service.py) 的 _load_input/_decide | 是否先合并净变化？原始流水是否保留？复用/清空分支是否免模型？有效 fact 与来源快照是否一致？ |
| 模型整理与校验 | [summary_system.txt](../src/companion_ever/prompts/summary_system.txt)；_citation_errors/_validate | quote 是否来自 fact.content？旧操作和聊天引文是否被误当当前证据？reality、条数和字段容量是否保持约束？ |
| 提交 | SummaryService._commit 的 transaction | 模型运行期间来源或摘要变化时是否拒绝旧结果？两个摘要、引用版本、checkpoint、任务成功是否一起提交？ |
| 持续用于对话 | SummaryService.status/prompt_text；PromptService.build | 排队、运行中或更新失败时是否仍注入上次成功摘要？新批次提交后是否整体替换？是否明确近期用户纠正优先？ |
| 页面更新 | chat.py 的 notify_summary_updated；app.js 的 summary_updated、openRelation | 页面是否可查看来源？显示中的历史摘要与模型实际使用的摘要是否区分清楚？ |

额外检查两条无需模型的分支：净变化为零且旧结论仍有效时复用摘要；没有任何可用依据时清空摘要。后台续批只覆盖启动时已有的积压，执行中新变化重新受调度门槛约束。角色基础设定不在摘要写入范围中，摘要也不创建新 note。

核心持久数据：fact_change、summary_job、summary_checkpoint、memory_block。对应测试：`tests/test_summary_evolution.py`、`test_background_service.py`、`test_task_transport.py`。

## 5. 三条链路共同检查的内容

- **范围与权限：** 同一 user/persona 是否贯穿请求、候选、来源、关系、写入、状态查询与推送？入口校验之后，写入是否仍检查实际数据？
- **成功边界：** 模型返回、校验通过、事务提交、页面收到通知是四个时刻；状态和计数应对应实际提交结果。
- **并发与恢复：** [tasks.py](../src/companion_ever/runtime/tasks.py) 的 slot/execute/_heartbeat/shutdown；检查先取得名额再 claim、独立续租、旧 token 拒写、中断预算和失败重试分类。
- **可观测性：** 将 trace 的 input_case、llm-01.payload、原始 response、validation 和 db.commit 串起来；用 job_id、attempt_id 定位执行，不把触发查询当成模型执行。方法见 [TRACING](TRACING.md)。
- **测试证据：** `tests/test_task_runtime.py` 覆盖续租异常、写入前丢租约与进程重启；`test_task_traces.py` 覆盖调用前落盘、部分回复和纠错保留。检查实际断言能否发现你担心的错误。

## 6. 用一个案例贯穿 Review

在临时数据库中跟踪“我长期喜欢茶，不喜欢咖啡”：先找到用户 message ID 和 generation，再找到处理这句话的 memory_job、note_evidence、fact_change、summary_job，最后查看下一次聊天的实际 payload。

再加入“更正，我现在更喜欢咖啡，不喜欢茶了”：检查新事实、supersedes、旧事实的保留及检索有效性、摘要撤回/重建。分别在模型响应前、校验后写入前、提交后推送前制造中断，观察哪些数据已经正式保存、重试是否重复、进度是否越过失败范围。

每轮可按下表记自己的结论，定位到具体函数或 SQL：

| 入口/触发 | 本批输入与 ID | 写入及事务边界 | 成功/失败/恢复 | 已核对测试或 trace |
|---|---|---|---|---|
| 用户操作或后台条件 | 来源、候选、固定范围 | 涉及表、版本/token | 状态变化、重试责任方 | 文件与具体断言/产物 |

## 数据职责

| 数据 | 职责 |
|---|---|
| `user`、`persona_definition` | 用户归属与角色基础设定；基础设定只由显式创建/编辑修改 |
| `conversation`、`conversation_log` | 私聊范围、迁移起点与原始消息 |
| `generation`、`thinking_log` | 一次回复生成及独立思考内容 |
| `note`、`note_evidence`、`note_revision` | 两层长期笔记、逐字原话证据及修改前版本 |
| `note_link` | supports、continues、supersedes 与依据 |
| `memory_job` | 记忆处理固定范围及持久状态 |
| `fact_change` | 事实增删改、归档、替代、证据变化的事务内流水 |
| `memory_block`、`summary_checkpoint`、`summary_job` | 用户画像/关系概况、来源、操作游标及独立任务 |
| `task_attempt`、`task_event` | 每次后台执行、阶段、心跳和结果；详细内容通过 trace ID 关联文件 |

`persona_runtime` 和笔记旧 `type` 已删除。短期场景直接使用近期聊天。`kind` 与 `reality` 是两个独立维度，具体规则见 [记忆规则](MEMORY_POLICY.md)。

## Review 时注意的边界

- API 校验 PIN，服务和写入器检查用户/角色归属；关系遍历、来源和重连结果都受范围限制。
- 新用户输入取消旧生成的未完成输出，已保存消息保留。断线本身不取消生成。
- 数据一致性依赖 SQLite 事务、SAVEPOINT 和执行 token；旧执行者不能提交。
- 页面已读状态属于本机：可见会话滚到最新才推进游标，后台消息保持未读。
- 迁移保留旧消息和笔记，不会自动重抽全部历史。迁移与故障恢复见 [后台任务](BACKGROUND_TASKS.md)。
