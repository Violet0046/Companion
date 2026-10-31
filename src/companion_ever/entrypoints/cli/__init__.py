"""cli — 命令行工具。

子命令：
- setup: 创建用户 + 角色
- chat: 开发调试用的普通对话循环
- show: 查看用户 / 角色的状态

使用：
    python -m companion_ever.entrypoints.cli setup --user alice --persona 柳如烟 --template hot_girlfriend
    python -m companion_ever.entrypoints.cli chat --user alice --persona 柳如烟
    python -m companion_ever.entrypoints.cli show --user alice --persona 柳如烟
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from companion_ever.config import AppConfig, LLMConfig
from companion_ever.bootstrap.app import AppContext, build_app
from companion_ever.characters.templates import build_definition, list_templates
from companion_ever.utils.text import strip_format_prefix
from companion_ever.entrypoints.cli.maintain_cmd import cmd_backup
from companion_ever.entrypoints.cli.trace_cmd import (
    cmd_trace_list,
    cmd_trace_prune,
    cmd_trace_show,
    cmd_trace_show_raw,
)


def _build_app(db_path: Optional[str] = None, llm_cfg: Optional[LLMConfig] = None) -> AppContext:
    """构造 AppContext（CLI 用）。

    优先级（从高到低）：
    1. 命令行 --db 参数指定的路径
    2. .env 中 COMPANION_DB_PATH
    3. 默认 ./memory/companion.db

    LLM 配置同理：命令行 > .env > 内置默认值。
    AppConfig.from_env() 自动加载 .env 文件。
    """
    if llm_cfg is not None:
        # 命令行显式指定 LLM——直接用，不读 .env
        cfg = AppConfig(db_path=Path(db_path or "./memory/companion.db"), llm=llm_cfg)
    else:
        # 从 .env 读（含 DB path 和 LLM 配置）
        cfg = AppConfig.from_env()
        # 命令行 --db 优先于 .env（没给 --db 时保留 .env 里的路径）
        if db_path:
            cfg.db_path = Path(db_path)

    # 自动创建 db 所在目录（如果不存在）
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return build_app(cfg)


# ============ 子命令 ============

def cmd_setup(args) -> int:
    """setup：创建用户 + 角色。"""
    ctx = _build_app(args.db)
    try:
        # 1. 创建用户
        try:
            ctx.repos.user.create(args.user)
            print(f"[OK] Created user '{args.user}'")
        except ValueError as e:
            print(f"[--] User '{args.user}': {e}")

        # 2. 准备 definition
        if args.template:
            # 用模板时，把模板里的 {user} 占位符替换成实际 user_id
            definition = build_definition(args.template, args.user)
            print(f"[--] Using template '{args.template}' (with user='{args.user}')")
        elif args.definition:
            definition = args.definition
        else:
            # 默认用 template hot_girlfriend
            definition = build_definition("hot_girlfriend", args.user)
            print(f"[--] No template specified, using 'hot_girlfriend'")

        # 3. 创建 persona
        try:
            ctx.services.persona.create_persona(
                user_id=args.user,
                persona_id=args.persona,
                definition=definition,
            )
            print(f"[OK] Created persona '{args.persona}' for user '{args.user}'")
            return 0
        except ValueError as e:
            print(f"[FAIL] {e}")
            return 1
    finally:
        ctx.close()


def cmd_chat(args) -> int:
    """开发调试用对话循环，输入不包含终端控制命令。"""
    ctx = _build_app(args.db)
    try:
        # 验证 user / persona 存在
        if not ctx.repos.user.exists(args.user):
            print(f"[FAIL] User '{args.user}' 不存在。先跑 setup。")
            return 1
        if not ctx.services.persona.exists(args.user, args.persona):
            print(f"[FAIL] User '{args.user}' 不拥有 persona '{args.persona}'。先跑 setup。")
            return 1

        print(f"=== Companion Chat ===")
        print(f"  user:    {args.user}")
        print(f"  persona: {args.persona}")
        print("  关闭终端或按 Ctrl+C 退出。")
        print()

        ctx.lifecycle.start()
        while True:
            try:
                # 提示符显示当前 user（这是你说话的身份），不是 persona
                user_input = input(f"[{args.user}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                print("[--] 输入流关闭")
                return 0

            if not user_input:
                continue

            raw = ctx.services.chat.chat(args.user, args.persona, user_input)
            response, _ = strip_format_prefix(raw)
            if response.strip():
                print(f"\n{response.strip()}\n")
    finally:
        ctx.close()


def cmd_show(args) -> int:
    """show：查看状态。"""
    ctx = _build_app(args.db)
    try:
        if not ctx.repos.user.exists(args.user):
            print(f"[FAIL] User '{args.user}' 不存在。")
            return 1

        personas = ctx.services.persona.list_user_personas(args.user)
        print(f"User '{args.user}' owns {len(personas)} persona(s): {personas}")

        if args.persona:
            if args.persona not in personas:
                print(f"[FAIL] User '{args.user}' 不拥有 persona '{args.persona}'。")
                return 1

            definition = ctx.services.persona.get_definition(args.user,args.persona)
            if definition:
                print('\n--- Persona definition ---')
                print(definition['content'])

            # 1. 显示 notes
            notes = ctx.services.note.find_notes(
                user_id=args.user, persona_id=args.persona, limit=20,
            )
            print(f"\n--- Notes ({len(notes)}) ---")
            for n in notes:
                content = (
                    n.content
                    if isinstance(n.content, str)
                    else json.dumps(n.content, ensure_ascii=False)
                )
                # 截断过长内容
                display = content[:120] + "..." if len(content) > 120 else content
                print(f"  [{n.kind.value}] importance={n.importance:.2f}")
                print(f"    {display}")

            # 2. 显示 blocks
            blocks = ctx.services.block.get_blocks(args.user, args.persona)
            print(f"\n--- Blocks ({len(blocks)}) ---")
            for b in blocks:
                print(f"  [{b.block_type.value}]")
                print(f"    {json.dumps(b.fields, ensure_ascii=False, indent=2)}")

            # 3. 显示 conversation 统计
            count = ctx.services.conversation.count(args.user, args.persona)
            markers = ctx.services.conversation.get_last_n_session_closes(
                user_id=args.user, persona_id=args.persona, n=100,
            )
            print(f"\n--- Conversation ---")
            print(f"  total messages: {count}")
            print(f"  session_close markers: {len(markers)}")

        return 0
    finally:
        ctx.close()




def build_parser() -> argparse.ArgumentParser:
    """构造 argparse。"""
    parser = argparse.ArgumentParser(
        prog="companion-cli",
        description="Companion Ever Memory CLI",
    )
    parser.add_argument(
        "--db", default=None,
        help="SQLite database path（不指定则用 COMPANION_DB_PATH，默认 ./memory/companion.db）",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="创建用户 + 角色")
    p_setup.add_argument("--user", required=True, help="用户名")
    p_setup.add_argument("--persona", required=True, help="persona ID")
    p_setup.add_argument(
        "--template", choices=list_templates(),
        help="使用内置 persona 模板",
    )
    p_setup.add_argument(
        "--definition",
        help="自定义 persona_basics JSON（与 --template 互斥）",
    )

    # chat
    p_chat = sub.add_parser("chat", help="启动对话循环")
    p_chat.add_argument("--user", required=True)
    p_chat.add_argument("--persona", required=True)

    # show
    p_show = sub.add_parser("show", help="查看状态")
    p_show.add_argument("--user", required=True)
    p_show.add_argument("--persona", help="指定 persona 看详情")

    # maintain（数据维护）
    p_maint = sub.add_parser("maintain", help="数据库备份")
    maint_sub = p_maint.add_subparsers(dest="maint_cmd", required=True)

    m_bak = maint_sub.add_parser(
        "backup",
        help="做一份完整的数据库备份（含 WAL 内容）",
    )

    # serve（Web / PWA）
    p_serve = sub.add_parser(
        "serve",
        help="启动 Web 服务（手机同 WiFi 就能聊）",
    )
    p_serve.add_argument("--host", default=None, help="监听地址，默认 0.0.0.0")
    p_serve.add_argument("--port", type=int, default=None, help="端口，默认 8000")

    # trace（诊断：看模型究竟看到了什么）
    p_trace = sub.add_parser("trace", help="trace 诊断（看每轮链路 + 模型实际收到的内容）")
    p_trace.add_argument('--dir',dest='trace_dir',help='直接指定 trace 目录；默认使用数据库同级 traces/')
    trace_sub = p_trace.add_subparsers(dest="trace_cmd", required=True)

    t_list = trace_sub.add_parser("list", help="列出最近的 trace")
    t_list.add_argument("--user", default=None, help="按用户过滤")
    t_list.add_argument("--persona", default=None, help="按角色过滤")
    t_list.add_argument("--kind", default=None, choices=["chat", "extraction", "memory_attempt", "summary", "memory_evaluation"], help="按类型过滤")
    t_list.add_argument("--last", type=int, default=20, help="显示最近 N 条（默认 20）")

    t_show = trace_sub.add_parser("show", help="看某一轮的链路")
    t_show.add_argument("trace_id", help="trace_id（可只给前缀）")
    t_show.add_argument("--request", action="store_true", help="直接打印模型实际收到的请求")
    t_show.add_argument("--response", action="store_true", help="直接打印模型实际返回的内容")
    t_show.add_argument('--artifact',help='查看指定产物，如 llm-01.payload、llm-02.response、validation-01.error')
    t_show.add_argument("--max-chars", type=int, default=0, help="最多打印多少字符（0 = 不限）")

    t_prune = trace_sub.add_parser("prune", help="清理旧 trace")
    t_prune.add_argument("--days", type=int, default=7, help="保留最近 N 天（默认 7）")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "setup":
        return cmd_setup(args)
    elif args.cmd == "chat":
        return cmd_chat(args)
    elif args.cmd == "show":
        return cmd_show(args)
    elif args.cmd == "maintain":
        return cmd_backup(args)
    elif args.cmd == "serve":
        try:
            from companion_ever.entrypoints.web.server import run_server
        except ImportError as e:
            print("[FAIL] 缺少依赖：", e)
            print("       安装：pip install fastapi uvicorn")
            return 1
        run_server(host=args.host, port=args.port, db_path=args.db)
        return 0
    elif args.cmd == "trace":
        if args.trace_cmd == "list":
            return cmd_trace_list(args)
        elif args.trace_cmd == "show":
            if args.request or args.response or args.artifact:
                return cmd_trace_show_raw(args)
            return cmd_trace_show(args)
        elif args.trace_cmd == "prune":
            return cmd_trace_prune(args)
        else:
            print("[FAIL] 未知 trace 子命令")
            return 1
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
