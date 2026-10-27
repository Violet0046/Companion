"""cli — 命令行工具。

子命令：
- setup: 创建用户 + 角色
- chat: 启动对话循环（/quit 触发 session close + extraction）
- show: 查看用户 / 角色的状态

使用：
    python -m companion_ever.cli setup --user alice --persona 柳如烟 --template hot_girlfriend
    python -m companion_ever.cli chat --user alice --persona 柳如烟
    python -m companion_ever.cli show --user alice --persona 柳如烟
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from companion_ever.application.chat.session_close_service import SessionCloseReport
from companion_ever.config import AppConfig, LLMConfig
from companion_ever.app import build_app
from companion_ever.domain.enums import NoteType
from companion_ever.domain.persona_templates import PERSONA_TEMPLATES, template_to_json


def _substitute_user_in_template(fields: dict, user_id: str) -> dict:
    """把模板里写死的"周艺科"等用户名占位符替换成实际 user_id。

    递归扫描所有 string 字段，替换常见的用户身份代词：
    - "周艺科(用户)" / "周艺科" / "周艺科(用户)" → "{user_id}"
    - "我" / "自己" 保留（属于角色视角，不需要换）

    返回新 dict（不修改原 template）。
    """
    # 写死的角色身份占位符 → 实际 user_id
    substitutions = {
        "周艺科(用户)": f"{user_id}(用户)",
        "周艺科": user_id,
    }

    def _walk(obj):
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        if isinstance(obj, str):
            for old, new in substitutions.items():
                obj = obj.replace(old, new)
            return obj
        return obj

    return _walk(fields)


def _build_app(db_path: str, llm_cfg: Optional[LLMConfig] = None) -> "AppContext":
    """构造 AppContext（CLI 用）。

    优先级（从高到低）：
    1. 命令行 --db 参数指定的路径
    2. .env 中 COMPANION_DB_PATH
    3. 默认 ./memory/companion.db

    LLM 配置同理：命令行 > .env > 内置默认值。
    AppConfig.from_env() 自动加载 .env 文件。
    """
    # 自动创建 db 所在目录（如果不存在）
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    if llm_cfg is not None:
        # 命令行显式指定 LLM——直接用，不读 .env
        cfg = AppConfig(db_path=Path(db_path), llm=llm_cfg)
    else:
        # 从 .env 读（含 DB path 和 LLM 配置）
        cfg = AppConfig.from_env()
        # 命令行 --db 优先于 .env
        cfg.db_path = Path(db_path)
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
            # 用模板时，把模板里写死的"周艺科"替换成实际 user_id
            template_fields = PERSONA_TEMPLATES[args.template].copy()
            template_fields = _substitute_user_in_template(
                template_fields, args.user,
            )
            definition = json.dumps(template_fields, ensure_ascii=False, indent=2)
            print(f"[--] Using template '{args.template}' (with user='{args.user}')")
        elif args.definition:
            definition = args.definition
        else:
            # 默认用 template hot_girlfriend
            template_fields = PERSONA_TEMPLATES["hot_girlfriend"].copy()
            template_fields = _substitute_user_in_template(
                template_fields, args.user,
            )
            definition = json.dumps(template_fields, ensure_ascii=False, indent=2)
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
        ctx.conn.close()


def cmd_chat(args) -> int:
    """chat：交互式对话循环。/quit 触发 session close。"""
    ctx = _build_app(args.db)
    try:
        # 验证 user / persona 存在
        if not ctx.repos.user.exists(args.user):
            print(f"[FAIL] User '{args.user}' 不存在。先跑 setup。")
            return 1
        if not ctx.services.persona.exists(args.user, args.persona):
            print(f"[FAIL] User '{args.user}' 不拥有 persona '{args.persona}'。先跑 setup。")
            return 1

        graph = ctx.workflows.chat
        print(f"=== Companion Chat ===")
        print(f"  user:    {args.user}")
        print(f"  persona: {args.persona}")
        print(f"  输入 '/quit' 退出并触发 session 抽取。")
        print()

        # 从 DB 读取当前最大 turn_index（保证新 session 不会与历史 UNIQUE 冲突）
        # 注意：get_recent_messages 已自动过滤 session_close marker
        recent_msgs = ctx.services.conversation.get_recent_messages(
            args.user, args.persona, limit=10000,
        )
        if recent_msgs:
            next_turn = max(m.turn_index for m in recent_msgs) + 1
        else:
            next_turn = 1
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

            # 处理 /quit /exit 内部命令（langgraph 用 /quit 触发 close 节点）
            if user_input in ("/exit",):
                user_input = "/quit"

            result = graph.invoke({
                "user_id": args.user,
                "persona_id": args.persona,
                "user_input": user_input,
                "next_turn_index": next_turn,
            })

            # 输出 response（不再加 [persona] 前缀，避免和 LLM 的 <think> 混淆）
            if "response" in result and user_input != "/quit":
                response = result["response"].strip()
                if response:
                    print(f"\n{response}\n")
                next_turn = result.get("next_turn_index", next_turn + 1)

            # 处理 close
            if result.get("close_report"):
                _print_close_report(result["close_report"])
                return 0
    finally:
        ctx.conn.close()


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
                print(f"  [{n.type.value}] importance={n.importance:.2f}")
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
        ctx.conn.close()


def _print_close_report(close_report: SessionCloseReport) -> None:
    """打印 session close 的合并报告。"""
    ext = close_report.extraction
    print()
    print(f"=== Session Closed ===")
    print(f"  marker_id: {close_report.marker_id}")
    print(f"  conversations_processed: {ext.conversations_processed}")
    print(f"  facts_created: {ext.facts_created}")
    print(f"  blocks_updated: {ext.blocks_updated}")
    if ext.errors:
        print(f"  errors:")
        for err in ext.errors:
            print(f"    - {err}")
    print()


# ============ main ============

def build_parser() -> argparse.ArgumentParser:
    """构造 argparse。"""
    parser = argparse.ArgumentParser(
        prog="companion-cli",
        description="Companion Ever Memory CLI",
    )
    parser.add_argument(
        "--db", default="./memory/companion.db",
        help="SQLite database path（默认 ./memory/companion.db）",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="创建用户 + 角色")
    p_setup.add_argument("--user", required=True, help="用户名")
    p_setup.add_argument("--persona", required=True, help="persona ID")
    p_setup.add_argument(
        "--template", choices=list(PERSONA_TEMPLATES.keys()),
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
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())