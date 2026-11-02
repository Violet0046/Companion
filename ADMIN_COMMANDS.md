# 同行管理命令手册

本文用于日常管理账号、额度、用量和备份。命令以当前版本为准；普通账号管理不会调用模型。服务器与本地数据库相互独立，本地创建的账号不会自动出现在服务器。

## 1. 在哪里执行

### Linux 服务器

SSH 登录服务器后执行：

```bash
cd /opt/tongxing

# 临时快捷命令，仅在当前终端会话有效；以运行应用的系统用户执行。
ce() { runuser -u tongxing -- /opt/tongxing/.venv/bin/python -m companion_ever "$@"; }

ce account list
```

以下示例中的 `ce` 均使用这个快捷命令。退出 SSH 后重新定义即可。如果你的部署路径或服务用户不同，请相应替换。命令默认读取当前项目 `.env` 的 `COMPANION_DB_PATH`，不会自动选择其他机器上的数据库。

### Windows 本地

PowerShell 在项目根目录执行，使用同一个临时快捷命令名：

```powershell
function ce { & .\.venv\Scripts\python.exe -m companion_ever @args }
ce account list
```

本机浏览器通过 `http://127.0.0.1:8000` 测试时，本地 `.env` 配置为：

```dotenv
COMPANION_WEB_SECURE_COOKIE=0
COMPANION_WEB_ORIGIN=http://127.0.0.1:8000
```

修改后重新启动本地服务：

```powershell
ce serve --host 127.0.0.1 --port 8000
```

服务器正式 HTTPS 入口保持 `COMPANION_WEB_SECURE_COOKIE=1`，Origin 使用真实外部 HTTPS 地址。

## 2. 先分清三个名字

| 名称 | 用途 |
|---|---|
| 登录名，例如 `friendname` | 好友在登录页输入的账号，也是 `--username` 使用的值 |
| 内部用户 ID | 数据归属标识；`account list` 第一列。绑定旧数据、`show` 和 `proactive --user` 使用它 |
| 系统用户 `tongxing` | Linux 上运行服务、读写数据库的用户，与网页登录账号分别管理 |

新好友的内部用户 ID 自动生成，不必和登录名相同。登录名支持 3–32 位字母、数字、点、下划线或短横线，会统一为小写；密码支持 6–128 个字符。

## 3. 创建好友账号

```bash
ce account create --username friendname
```

按提示输入两遍初始密码，输入不回显，不要把密码写进命令参数。

这个命令会创建一个独立的空白用户。把登录地址、登录名和初始密码交给好友后，好友的使用流程是：

1. 使用初始密码登录。
2. 强制进入修改密码页，输入当前密码和不同的新密码。
3. 改密成功后，用新密码重新登录。
4. 在通讯录中创建自己的角色，开始聊天；需要通知时，在「我 → 消息通知」开启。

给好友创建时省略 `--bind-user` 和 `--permanent`。前者用于绑定已有数据，后者会跳过首次改密。

## 4. 查看账号与绑定旧数据

```bash
ce account list
```

输出依次为内部用户 ID、登录名、启用状态。登录名显示「未绑定」时，表示该旧用户尚未关联登录账号。

仅在为自己的旧用户首次建立登录账号时，核对第一列 ID 后执行：

```bash
ce account create --username ownername --bind-user EXISTING_USER_ID --permanent
```

将 `EXISTING_USER_ID` 替换成确认过的旧用户 ID。原角色、聊天、笔记继续归属同一个用户；`--permanent` 表示你正在设置正式密码，无需首次改密。

已绑定的账号修改密码应使用下一节命令。不要通过重新 `setup` 或再次创建账号来覆盖原用户。

## 5. 重置密码、撤销登录、停用与启用

| 操作 | 命令 | 结果 |
|---|---|---|
| 重置密码 | `ce account reset-password --username friendname` | 隐藏输入新的临时密码；旧登录和通知订阅失效，下一次登录必须改密 |
| 撤销全部登录 | `ce account revoke-sessions --username friendname` | 密码不变；撤销全部登录和通知订阅 |
| 停用账号 | `ce account disable --username friendname` | 保留数据，拒绝访问，撤销登录和通知，并使未完成的生成与整理执行失效 |
| 重新启用 | `ce account enable --username friendname` | 恢复账号访问，符合条件的整理任务可继续；旧登录、通知订阅和旧聊天生成不会自动恢复 |

这些操作平时不需要重启应用。终端操作与 Web 服务属于不同进程，旧 WebSocket 会在下一次收发或最多约 30 秒的检查中断开；每次发送数据前仍校验登录会话。

撤销后需重新登录、重新开启设备通知。用户也可以在网页「我」中修改密码、退出登录或退出所有设备。

## 6. 查看和调整账号额度

查看当前生效额度：

```bash
ce account limits --username friendname
```

同时调整每日新消息数与回复并发数：

```bash
ce account limits --username friendname --daily-messages 100 --concurrent-replies 1
```

也可以只改其中一项，另一项保持原设置：

```bash
ce account limits --username friendname --daily-messages 300
```

设为不限，例如自己的账号：

```bash
ce account limits --username ownername --daily-messages 0 --concurrent-replies 0
```

清除该账号的两项覆盖值，恢复环境配置的默认额度：

```bash
ce account limits --username friendname --reset
```

`--reset` 与指定数值不能同时使用。单账号调整立即生效；下调并发不会中断已运行的回复，待执行任务在有空位后继续。

全局默认值在 `.env` 中设置，修改后重启服务：

```dotenv
COMPANION_DAILY_MESSAGES=200
COMPANION_CONCURRENT_REPLIES=2
COMPANION_TZ_OFFSET_HOURS=8
```

所有额度都是整个账号跨角色合计。`0` 表示不限；未单独覆盖的账号跟随全局默认值。

计数规则：

- 每日额度按配置时区的自然日重置，默认 UTC+8 的零点。
- 消息被服务器成功接收后计数，之后模型失败也不会退回这条新消息额度。
- 同一消息的重复提交、失败回复重试不重复计入新消息数；重试产生的模型调用仍计入用量记录。
- 同一会话的新输入可替换旧回复；主动联系也受回复并发约束，不消耗用户新消息额度。
- 整理与摘要沿用各自调度，不占回复并发额度；三层模型调用均纳入用量统计。
- 达到额度后，新消息不会保存到服务器，也不会取消原回复；网页在当前浏览器保留未发送文本，供用户之后手动重试。换设备不能恢复这些文本，退出登录会清理本地待发送内容。

## 7. 查看账号用量

查看今天：

```bash
ce account usage --username friendname
```

查看指定日期，日期使用 `YYYY-MM-DD`：

```bash
ce account usage --username friendname --date 2026-11-01
```

输出 JSON 包含：

| 字段 | 含义 |
|---|---|
| `date`、`resets_at` | 统计日期与该自然日的结束时间 |
| `messages` | 已接收新消息数、有效额度、剩余额度；不限时 `remaining=null` |
| `replies` | 当前正在回复、当前等待任务数，以及有效并发额度 |
| `calls` | 已记录调用数、状态、输入/输出/缓存输入 token、累计模型耗时，包含群协调 |
| `layers` | 聊天 `chat`、整理 `extraction`、摘要 `summary`、群协调 `coordination` 各自的调用统计 |
| `recording_enabled`、`write_error`、`read_error` | 日志是否开启、当前进程是否观察到写入失败、此次是否读取失败 |

指定过去日期时，消息和模型调用按该日期统计，`replies` 仍表示当前任务状态，额度仍为当前设置。

统计只包含日志中已有的有效调用记录，不是完整账单。Token 为 `null` 表示没有上报，不能当作零；`*_reported_calls` 表示有对应计数的调用数量。输入 token 已经包含缓存输入部分，不要再把两者相加。`elapsed_ms` 的单位是毫秒，网页换算为秒；它是各次模型请求的累计值，可能包含并行调用。

用户自己的网页「我 → 今日用量」也可查看账号汇总。日志关闭或写入失败不影响数据库中的消息额度计数。

## 8. 数据库备份与更新

```bash
ce maintain backup
```

命令使用 SQLite 在线备份，包含 WAL 中已提交的数据，并检查备份完整性、外键。成功后打印备份路径；将重要副本安全保存到服务器外。不要在服务运行时只复制主 `.db` 文件，也不要随意删除 `-wal`、`-shm` 文件。

要明确指定另一份已经存在的数据库，全局 `--db` 必须放在子命令前：

```bash
ce --db /absolute/path/companion.db account list
```

账号管理命令发现数据库文件不存在时会报错，不会为你新建数据库。

当前为 schema v15。v11 增加账号额度，v12 引入群会话与引用，v13 增加群自动联系、按角色记忆进度和通知定位，v14 增加交棒及手动续聊来源，v15 统一角色主动机会选路。迁移会自动创建一致性备份，包括 `.bak-before-proactive-routing-...`。旧版未指定角色的待执行群主动任务会停止，需指定角色重新触发。首次运行新版账号命令也可能触发迁移，因此升级前先主动备份，再停服更新；正常日常管理无需停服。

服务器更新示例，在确认代码已提交、工作区无冲突后执行：

```bash
cd /opt/tongxing
git status --short
ce maintain backup
systemctl stop tongxing
git pull --ff-only
runuser -u tongxing -- .venv/bin/python -m pip install -e .
systemctl start tongxing
systemctl status tongxing --no-pager
curl -fsS --max-time 10 http://127.0.0.1:8000/api/health
```

启动可能需要几秒，再查看健康检查。不要复制覆盖生产 `.env`，也不要执行重新初始化数据的命令。更新后的库不能直接交给只支持旧 schema 的代码使用；回退前同时确认代码和数据库版本。

## 9. 主动联系的服务器检查与测试

这里的 `--user` 使用内部用户 ID，先从 `account list` 第一列取得。将示例中的 `USER_ID`、`PERSONA_ID`、`TASK_ID` 替换为实际值。

```bash
# 只检查条件，不调用模型
ce proactive check --user USER_ID --persona PERSONA_ID

# 15 秒后测试主动联系，会实际调用模型、保存消息，并尝试投递已开启的通知
ce proactive trigger --user USER_ID --persona PERSONA_ID --test --delay 15

# 用触发命令返回的任务 ID 查看状态
ce proactive status --task TASK_ID
```

角色需要在私聊或允许的群内与用户有过互动。上面的命令只指定获得机会的角色，由她在一次调用中选择私聊或最近活跃的一个允许群，不经过协调助手。测试模式跳过角色开关、免打扰和每日触发次数限制，但仍遵守权限、回复并发和上下文检查；普通候选群仍需开启群开关。实际开始时间还受后台扫描与模型耗时影响。相同 `--request-id` 避免重复任务，状态查询完成后会显示实际 destination。

如需限定群聊测试，同时指定 `--persona` 和 `--conversation-id`，角色必须仍在群内。先在网页创建群并发送过消息，再查询群 ID：

```bash
# 只列出群 ID、名称、成员和开关，不调用模型
ce proactive groups --user USER_ID

# 按真实自动规则检查；关闭开关、免打扰、未读或等待回复时会说明原因
ce proactive check --user USER_ID --persona PERSONA_ID --conversation-id GROUP_ID

# 显式测试：指定角色只在这个群主动发言，一次调用生成，不经协调助手
ce proactive trigger --user USER_ID --persona PERSONA_ID --conversation-id GROUP_ID --test --delay 15 --request-id group-smoke-001

# 查询保存的消息 ID 和通知投递结果
ce proactive status --task TASK_ID
```

运行中的新版服务执行这些任务。协调者可返回空邀请，角色也可选择不发言；任务完成但没有消息是允许的结果，不会为凑消息自动重调。`--test` 不消耗日常自动联系机会；重复同一个 `--request-id` 返回原任务，测试新一轮时换一个 ID。

验证通知时，先在手机主屏幕应用开启消息通知，再离开该群或锁屏。前台查看会暂缓通知，消息已读、用户继续发言、角色退出或任务取消会跳过；通知点击应打开对应群。通知状态 `accepted` 只表示推送服务接受，手机实际显示仍需真机验证。群自动开关在群右上角「⋯」，默认关闭，与私聊共用每日自动联系上限。


### 9.1 手动整理聊天并更新摘要

现有 CLI 没有独立的整理子命令。下面的终端函数直接调用同一套服务：固定本次消息范围，整理后同步检查 fact 摘要；已有任务运行时不抢占，失败时停止，不持续追赶新聊天。会实际调用模型、保存笔记及摘要。

```bash
ce_record() {
  runuser -u tongxing -- /opt/tongxing/.venv/bin/python - "$@" <<'PY'
import json
import sys
from companion_ever.entrypoints.cli import _build_app

if len(sys.argv) not in (3, 4):
    raise SystemExit("用法: ce_record USER_ID PERSONA_ID [GROUP_ID]")

user, persona = sys.argv[1:3]
scope = {"conversation_id": int(sys.argv[3])} if len(sys.argv) == 4 else {}
ctx = _build_app(None)
try:
    ctx.services.conversation.authorizer.require_persona(user, persona)
    jobs = ctx.services.extraction.jobs
    horizon = jobs.latest_message_id(user, persona, **scope)
    settled = True
    while (job := jobs.enqueue(user, persona, until_id=horizon, **scope)) is not None:
        if job["status"] == "failed":
            jobs.retry(job["id"])
        result = ctx.services.extraction.run({"user_id": user, "persona_id": persona, **scope})
        print(json.dumps({"phase": "memory", "job_id": job["id"],
            **{k: result.get(k) for k in ("job_status", "facts_created", "notes_updated", "errors")}},
            ensure_ascii=False), flush=True)
        if result.get("job_status") != "succeeded":
            settled = False
            break
    if settled and not jobs.busy(user, persona):
        summary = ctx.services.summary
        horizon = summary.repo.latest_change(user, persona)
        while (job := summary.repo.enqueue(user, persona, manual=True, until_id=horizon)) is not None:
            result = summary.run(job["id"])
            print(json.dumps({"phase": "summary", "job_id": job["id"],
                "status": result.get("status"), "error": result.get("last_error")},
                ensure_ascii=False), flush=True)
            if result.get("status") != "succeeded":
                break
        print("本次整理与摘要检查结束；没有新内容时不会调用模型。", flush=True)
    else:
        print("当前任务未完成或另有记忆任务在执行，请查看上面的状态。", flush=True)
finally:
    ctx.close()
PY
}

# 私聊：整理这个角色尚未处理的聊天，并更新两个摘要
ce_record USER_ID "PERSONA_ID"

# 群聊：仅为群内指定角色整理，其他角色需分别执行
ce_record USER_ID "PERSONA_ID" GROUP_ID
```

函数同 ce 一样仅在当前终端有效；需先进入 /opt/tongxing，使用同一份 .env。它不会伪造新用户消息，也不关闭或重置会话；pending/running 表示已有任务尚未完成，此时不会重复调用模型。

## 10. 常见情况

| 情况 | 处理 |
|---|---|
| 账号已绑定，不能再次创建 | 改用 `reset-password`；不要覆盖已有用户 |
| 找不到数据库 | 核对工作目录、`.env` 和全局 `--db`，不要通过 setup 补建生产数据 |
| 本地 HTTP 提示需要 HTTPS | 检查本地 Secure Cookie 配置和 Origin，重启本地服务；正式 HTTPS 保持 Secure 开启 |
| 反复输错后暂时无法登录 | 当前账号连续失败 5 次会暂时限制新登录约 15 分钟；同一来源 15 分钟达到 20 次登录尝试也会限流。等待后再试，账号密码问题可由管理员重置 |
| 改密码后收不到通知 | 重新登录，再到「我 → 消息通知」开启 |
| 已用消息数超过新设置额度 | 已有消息保留，之后的新消息等待次日或由管理员调整额度 |
| 网页显示额度不足 | 等额度恢复或调整账号额度，再点击未发送消息的「重试发送」 |
| Token 用量显示缺失或不完整 | 查看日志状态及 `*_reported_calls`，不要把缺失部分当作零消耗 |

不要把密码、登录 Cookie、模型密钥、推送订阅、原始数据库或未脱敏的私人日志提交 Git 或分享给别人。
