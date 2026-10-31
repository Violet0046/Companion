# 排查后台记忆与摘要

`memory/traces/YYYY-MM-DD/` 默认记录实际调用的输入、原始输出与校验结果。`COMPANION_TRACE=0` 关闭。路径随数据库移动，默认在数据库同级的 `traces/`。这些文件包含私人原话，仅保存在本地、受 Git 忽略。

## 先区分触发与执行

- `kind=extraction`：触发或查询记忆任务，不保证调用了模型。`context.job_id` 对应持久任务；`context.attempt_trace_id` 指向实际执行记录。
- `kind=memory_attempt`：一次实际记忆执行，有独立 `job_id`、`attempt_id` 和触发方 `parent_trace_id`。
- `kind=summary`：一次摘要执行，也有 `job_id`、`attempt_id`。

`task_attempt` / `task_event` 保存次数、范围、阶段、心跳和结果等运行元数据，其中 `stage=trace` 的事件给出文件 `trace_id`。文件保存详细内容；通过同一任务和执行 ID 关联，不把全部私人内容复制进任务状态接口。

触发记录在任务失败时标为 `error`，尚在排队或被其他执行者处理时为 `waiting`，没有任务时为 `noop`；不再一律写成 `ok`。重复查询已有失败任务会链接已有执行，不伪装成一次新模型调用。

## 每次执行的文件

| 文件后缀 | 内容 |
|---|---|
| `.json` | 状态、任务/执行/父 trace ID、阶段时间、`llm_calls`、产物索引 |
| `.input_case.txt` | 实际选中的新消息、参考前文、候选笔记；摘要则是旧摘要、合并后的实质变化和当前有效 fact |
| `.llm-01.request.txt` | 第一次调用的完整 system/user 消息与参数，可直接阅读 |
| `.llm-01.payload.txt` | 协议请求体快照，包括模型、消息、实际映射后的模型参数；客户端超时/重试预算单独列出 |
| `.llm-01.response.txt` | 模型原始正文；保留 JSON/思考标签等原样内容 |
| `.llm-01.reasoning.txt` | 服务商单独返回的 reasoning 字段，仅在实际提供时存在 |
| `.llm-01.error.txt` | 调用异常，隐去密钥与服务地址 |
| `.validation-01.parsed.txt` | 从原始回复解析出的 JSON；解析失败是 `null`，原始内容仍在 response 中 |
| `.validation-01.error.txt` | 结构、逐字引用、覆盖、kind/关系等校验的完整错误路径与原因 |
| `.llm-02.*` / `.validation-02.*` 等 | 工具交互或定向纠错后的独立请求、原始回复与校验结果；序号对应实际模型调用 |
| `.tool-01.request.txt` / `.tool-01.result.txt` 等 | 每次只读工具的名称、参数及实际返回给模型的结果；容量错误也保留 |
| `.input_case.final.txt` | 写入时使用的来源与完整候选集合，包含工具补查后加入的笔记 |
| `.write_result.txt` | 记忆成功写入的计数与建议 ID→正式 note ID 映射 |

无模型调用时不会编造 request/response。摘要仅调整证据或最后一个事实被删除时，可能直接校验/清空摘要而不调用模型。`.request.txt` / `.response.txt` 兼容旧查看方式，始终表示最后一次调用；分析多次纠错应使用带 `llm-01` / `llm-02` 的文件。

`task.input_ready` 说明来源数量与字符数，`llm.request`/`llm.result` 说明调用，`validation.failed`/`validation.passed` 说明校验，`db.commit` 说明已完成原子提交。`detail.at_ms` 表示距本次执行开始的时间，`llm_calls.elapsed_ms` 是该次调用耗时。模型调用成功不等于证据校验成功，也不等于已写库。

记忆图另记录 graph.node 和 tool.result，沿 load_input/prefetch/model/validate/tools/repair/commit 对照实际分支。ToolRequest 的 validation.passed 只表示只读请求结构合法，不能当作最终 Draft 通过或写入成功。初始 input_case 与最终 input_case.final 的候选集合可能不同，补查材料同时出现在下一次模型请求中。

摘要图记录 load_input/decide/reuse/clear/model/validate/repair/commit。复用和清空没有 llm_calls；不能因为有图节点就视为产生模型费用。

三层逐次调用在 llm_calls[].usage 统一记录 input_tokens、output_tokens、cached_input_tokens、uncached_input_tokens、reasoning_tokens 和 source，并保留 elapsed_ms。OpenAI 兼容接口的输入计数包含缓存读取；reasoning_tokens 是输出的细分项，不另加一次。未返回的字段为 null，不默认成 0；旧的 tokens_in_est/tokens_out_est 仍明确是字符粗估，不参与实际合计。

流式默认请求 include_usage；不支持的服务可设 COMPANION_STREAM_USAGE=0，不因统计参数被拒绝而自动再发收费请求。trace list/show 合计逐次调用并显示各字段覆盖次数；缓存命中率只在所有调用都返回输入及缓存计数时计算。金额须另按实际服务费率计算，代码不写死价格。

摘要 task.input_ready 的 journal_events 是原始范围内操作数，changes 是合并后的实质变化数。原始逐条流水通过 fact-changes 或数据库核对，不再把所有证据和重复快照发送给模型。零净变化复用仍有任务/提交记录，但没有模型请求文件。

请求在调用模型前落盘。接收正文或独立思考字段时，约每 0.5 秒保存一次快照；正常结束和异常退出时补存最后内容。`response_state=partial` 表示异常前收到的部分输出，`none` 表示没有正文，`complete` 表示完整返回；进行中的调用使用 `waiting`/`streaming`。进程硬退出时文件可能仍标 `running`，需结合执行表后续的中断/接管记录判断，不能当作成功，也不能恢复尚未落盘的最后分片。

文件更新使用临时文件替换，避免读到半个 JSON。诊断目录写入失败会在服务日志警告一次，并继续业务流程。请求中的授权头、API key 等选项脱敏；原始业务文本保留以便核查，不对来源正文做摘要。

临时文件名每次独立，Windows 短暂占用目标文件时最多短暂重试替换；持续失败仍告警且不影响业务。评测报告标记 trace_write_error，出现时不把已保存的调用合计当作完整账单。

## 怎么查看

```powershell
companion-cli trace list --last 10
companion-cli trace show <trace_id>
companion-cli trace show <attempt_trace_id> --artifact llm-01.payload
companion-cli trace show <attempt_trace_id> --artifact llm-02.response
companion-cli trace show <attempt_trace_id> --artifact validation-01.error
```

`trace show` 列出关联执行、每次模型调用及产物的绝对路径。可直接打开文本文件；使用 `--request` / `--response` 可看最后一次调用。非默认目录使用 `companion-cli trace --dir <目录> show <trace_id>`，这些命令只读文件，不启动任务、连接数据库或调用模型。

正常页面不展示诊断信息。网址添加 `?debug` 后可查看后台执行记录；API 的 `task-attempts` 只返回当前授权角色的运行元数据和 trace ID，不开放原始请求文件。

## 旧记录的边界

`20261030-123122-e343.json` 和 `20261030-123305-5bcf.json` 是旧版触发/查询记录，分别看到任务 running 和 failed，没有捕获实际模型调用，`artifacts` 为空。原来将正常返回的查询标为 `ok`，不能据此判断整理成功。当时未保存的模型输入、原始回复和逐次纠错内容无法事后还原；保留旧文件原样，新执行按本契约记录。

`tools/verify_background_recovery.py --model` 在临时数据库副本自动重放，报告旁的 `<报告名>.traces/` 保留完整诊断，不随临时数据库删除。正式任务的 trace 仍位于 `memory/traces/`，二者分开。

该开发工具还用 SQLite backup API 保留 `<报告名>.db` 测试快照。修复后可用 `--resume-db memory/reviews/.../旧报告.db --output memory/reviews/.../新报告.json` 在另一临时副本继续验证，原测试副本和正式数据库均只读；这是显式开发重放，会重排测试副本的失败任务，不能算作生产环境自动绕过永久失败。
