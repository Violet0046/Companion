"""cli/maintain_cmd.py — 数据维护命令。

子命令：
- maintain strip-think       剥掉 assistant 消息里的 <think> 块
- maintain clean-history     清理 conversation_log 的历史污染（/quit 轮次 + 格式模仿前缀）
- maintain clean-quit-notes  清理 note 表里由 /quit 派生出的幻觉记忆

三个命令都支持 --dry-run（只报告不改动）。

为什么要清理
------------
conversation_log 有两个职责：
1. 存档（给人看）—— 这里保留 <think> 是有意思的
2. **把最近 50 条送进 prompt**（给模型看）—— 这里 <think> 是纯污染

同一个字段同时服务两个用途，就会出现"对存档友好、对模型有害"的内容。
现在写入路径已经 strip 掉 think 块（存档会少一点，但模型受益更大），
这些命令负责清理历史存量。
"""
from __future__ import annotations

import re

from companion_ever.application.llm_json import THINK_PATTERN
from companion_ever.utils.text import strip_format_prefix, strip_invisible



# note 里描述"用 /quit 结束对话"的从句
_QUIT_CLAUSE_RE = re.compile(
    r"[，,；;]\s*(?:随后|然后|之后)?\s*"
    r"(?:使用|用|以)\s*/quit\s*"
    r"(?:命令)?\s*"
    r"(?:来)?\s*(?:结束|退出|结束掉)?\s*对话"
    r"|[，,；;]\s*(?:用户)?(?:习惯|喜欢)?(?:在[^，。]{0,20})?"
    r"(?:使用|用)\s*/quit\s*(?:命令)?\s*(?:结束|退出)?\s*对话"
)


def _make_backup(conn, db_path: str) -> str | None:
    """用 SQLite 的 VACUUM INTO 做一份**完整**的备份。

    为什么不能用 Copy-Item：
    数据库是 WAL 模式，最近写入的数据还躺在 companion.db-wal 里，
    没有 checkpoint 到主库文件。直接复制 .db 会漏掉这部分
    （实测漏了 4 行）。VACUUM INTO 会把 WAL 内容一起写进新文件。
    """
    import datetime
    import os

    src = os.path.abspath(db_path)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(
        os.path.dirname(src), f"{os.path.basename(src)}.bak-{stamp}"
    )
    if os.path.exists(out):
        return None
    try:
        conn.execute("VACUUM INTO ?", (out,))
        return out
    except Exception:
        return None


# 格式前缀的匹配实现放在 utils/text.py（写入路径也要用）
_strip_format_prefix = strip_format_prefix


def _fmt_preview(content: str, n: int = 46) -> str:
    flat = " ".join(content.split())
    return flat[:n] + ("..." if len(flat) > n else "")


# ============ maintain strip-think ============

def cmd_strip_think(args) -> int:
    """剥掉历史 assistant 消息里的 <think> 块。"""
    from companion_ever.cli import _build_app

    ctx = _build_app(args.db)
    try:
        conn = ctx.conn
        rows = conn.execute(
            "SELECT id, content FROM conversation_log "
            "WHERE role = 'assistant' AND content LIKE '%<think>%'"
        ).fetchall()

        if not rows:
            print("[OK] 没有需要清理的消息（历史里已经没有 <think> 块）")
            return 0

        before = sum(len(r["content"]) for r in rows)
        updated = 0
        emptied = 0
        for r in rows:
            new = THINK_PATTERN.sub("", r["content"]).strip()
            if new == r["content"]:
                continue
            if not new:
                emptied += 1
            conn.execute(
                "UPDATE conversation_log SET content = ? WHERE id = ?",
                (new, r["id"]),
            )
            updated += 1

        after = sum(
            len(r[0]) for r in conn.execute(
                "SELECT content FROM conversation_log WHERE role = 'assistant'"
            ).fetchall()
        )

        print(f"[OK] 清理 {updated} 条 assistant 消息（其中 {emptied} 条剥完后为空）")
        print(f"     assistant 消息总字符：{before:,} -> {after:,}")
        if emptied:
            print(f"     注意：{emptied} 条消息剥完后没有可见内容，仍保留在库里")
        print("     提示：conversation_log 不在 FTS 索引里，无需重建索引")
        return 0
    finally:
        ctx.conn.close()


# ============ maintain backup ============

def cmd_backup(args) -> int:
    """做一份完整的数据库备份（WAL 内容也会包含进去）。"""
    from companion_ever.cli import _build_app

    ctx = _build_app(args.db)
    try:
        out = _make_backup(ctx.conn, args.db)
        if out:
            import os
            size = os.path.getsize(out)
            print(f"[OK] 备份完成: {out}  ({size:,} 字节)")
            return 0
        print("[FAIL] 备份失败")
        return 1
    finally:
        ctx.conn.close()


# ============ maintain restore-thinking ============

def cmd_restore_thinking(args) -> int:
    """从备份里把历史思考过程恢复到 thinking_log。

    背景：以前的 assistant 消息里存着完整的 <think> 块，
    后来为了修 prompt 尺寸把它们剔掉了（那时尚未分表）。
    备份里还有它们，这个命令把它们找回来，存到 thinking_log。

    匹配方式：(user_id, persona_id, turn_index, role='assistant')
    """
    import glob
    import os
    import sqlite3

    from companion_ever.cli import _build_app
    from companion_ever.utils.text import extract_thinking

    ctx = _build_app(args.db)
    try:
        conn = ctx.conn

        # --- 选备份 ---
        if args.source:
            src_path = args.source
            if not os.path.exists(src_path):
                print(f"[FAIL] 备份不存在: {src_path}")
                return 1
        else:
            cands = sorted(glob.glob("memory/*.db.bak-*"))
            if not cands:
                print("[!] 没有找到备份文件（memory/*.db.bak-*）")
                return 1
            src_path = cands[-1]
        print()
        print(f"备份源: {src_path}")

        src = sqlite3.connect(src_path)
        src.row_factory = sqlite3.Row

        rows = src.execute(
            "SELECT user_id, persona_id, turn_index, content FROM conversation_log "
            "WHERE role = 'assistant' AND content LIKE '%<think>%'"
        ).fetchall()

        restored = 0
        skipped = 0
        matched = 0
        for r in rows:
            _, think = extract_thinking(r["content"])
            if not think:
                continue

            # 在当前库里找对应的 assistant 消息
            live = conn.execute(
                "SELECT id FROM conversation_log "
                "WHERE user_id = ? AND persona_id = ? AND turn_index = ? AND role = 'assistant' "
                "ORDER BY id LIMIT 1",
                (r["user_id"], r["persona_id"], r["turn_index"]),
            ).fetchone()
            if live is None:
                skipped += 1
                continue

            exists = conn.execute(
                "SELECT 1 FROM thinking_log WHERE message_id = ?",
                (live["id"],),
            ).fetchone()
            if exists:
                skipped += 1
                continue

            conn.execute(
                "INSERT INTO thinking_log "
                "(message_id, user_id, persona_id, turn_index, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (live["id"], r["user_id"], r["persona_id"], r["turn_index"], think),
            )
            restored += 1
            matched += 1

        total = conn.execute(
            "SELECT COUNT(*) FROM thinking_log WHERE user_id = ? AND persona_id = ?",
            (args.user, args.persona),
        ).fetchone()[0]
        chars = conn.execute(
            "SELECT COALESCE(SUM(length(content)), 0) FROM thinking_log "
            "WHERE user_id = ? AND persona_id = ?",
            (args.user, args.persona),
        ).fetchone()[0]

        print(f"[OK] 恢复 {restored} 条思考记录（跳过 {skipped} 条："
              "已存在或对应消息已被删除）")
        print(f"     thinking_log 当前：{total} 条 / {chars:,} 字符")
        print("     提示：thinking_log 不会进 prompt，只影响界面展示")
        return 0
    finally:
        ctx.conn.close()


# ============ maintain clean-history ============

def cmd_clean_history(args) -> int:
    """清理 conversation_log 的历史污染。

    做两件事：
    1. 删除 /quit 轮次（假 user 消息 + 模型对它生成的告别回复）
       —— 老实现里 /quit 会先跑一次完整 LLM 调用，这轮对话是纯噪声
    2. 剥掉 assistant 回复开头模仿旧格式的时间戳/role 前缀
       —— 模型学会旧版 prompt 的历史格式后自己吐了出来

    session_close marker（哨兵）保留不动。
    """
    from companion_ever.cli import _build_app

    ctx = _build_app(args.db)
    try:
        conn = ctx.conn
        dry = getattr(args, "dry_run", False)

        # --- 1. 找 /quit 轮次 ---
        quit_turns = conn.execute(
            "SELECT DISTINCT user_id, persona_id, turn_index FROM conversation_log "
            "WHERE role = 'user' AND trim(content) IN ('/quit', '/exit')"
        ).fetchall()

        quit_rows = []
        for t in quit_turns:
            rs = conn.execute(
                "SELECT id, role, content FROM conversation_log "
                "WHERE user_id = ? AND persona_id = ? AND turn_index = ? "
                "  AND role IN ('user', 'assistant')",
                (t["user_id"], t["persona_id"], t["turn_index"]),
            ).fetchall()
            quit_rows.extend(rs)

        # --- 2b. 找含零宽字符的行（BOM 等） ---
        invisible_rows = []
        for r in conn.execute(
            "SELECT id, role, content FROM conversation_log WHERE content LIKE ?",
            ("%﻿%",),
        ).fetchall():
            new = strip_invisible(r["content"])
            if new != r["content"]:
                invisible_rows.append((r["id"], r["content"], new))

        # --- 2. 找带格式模仿前缀的 assistant 行（排除将被删除的 /quit 行）---
        quitting_ids = {r["id"] for r in quit_rows}
        prefix_rows = []
        for r in conn.execute(
            "SELECT id, content FROM conversation_log "
            "WHERE role = 'assistant' AND content LIKE '[%'"
        ).fetchall():
            if r["id"] in quitting_ids:
                continue
            new, layers = _strip_format_prefix(r["content"])
            if layers and new != r["content"]:
                prefix_rows.append((r["id"], r["content"], new, layers))

        print()
        print("=" * 60)
        print("  清理 conversation_log 历史污染")
        print("=" * 60)

        # --- 报告 ---
        print(f"\n[1] /quit 轮次：{len(quit_turns)} 轮 / {len(quit_rows)} 行")
        for r in sorted(quit_rows, key=lambda x: x["id"])[:12]:
            print(f"    id={r['id']:>4} {r['role']:<9} {_fmt_preview(r['content'])}")
        if len(quit_rows) > 12:
            print(f"    ... 共 {len(quit_rows)} 行")

        print(f"\n[2] 格式模仿前缀：{len(prefix_rows)} 行")
        for rid, old, new, layers in prefix_rows[:8]:
            print(f"    id={rid:>4} ({layers}层)")
            print(f"        前: {_fmt_preview(old)}")
            print(f"        后: {_fmt_preview(new)}")
        if len(prefix_rows) > 8:
            print(f"    ... 共 {len(prefix_rows)} 行")

        print(f"\n[2b] 零宽字符（BOM 等）：{len(invisible_rows)} 行")
        for rid, old, new in invisible_rows[:6]:
            print(f"    id={rid:>4}")
            print(f"        前: {_fmt_preview(old)}")
            print(f"        后: {_fmt_preview(new)}")

        before_total = conn.execute(
            "SELECT COALESCE(SUM(length(content)), 0) FROM conversation_log"
        ).fetchone()[0]

        if dry:
            print("\n[dry-run] 没有改动任何东西。加 --dry-run=false 之外直接运行即可执行。")
            print("（本命令为 dry-run 模式）")
            return 0

        # --- 执行 ---
        if not getattr(args, "no_backup", False):
            bak = _make_backup(conn, args.db)
            if bak:
                print(f"     [backup] {bak}")

        if quit_rows:
            ids = [r["id"] for r in quit_rows]
            marks = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM conversation_log WHERE id IN ({marks})", ids
            )

        for rid, old, new, layers in prefix_rows:
            conn.execute(
                "UPDATE conversation_log SET content = ? WHERE id = ?",
                (new, rid),
            )

        for rid, old, new in invisible_rows:
            conn.execute(
                "UPDATE conversation_log SET content = ? WHERE id = ?",
                (new, rid),
            )
            conn.execute(
                "UPDATE conversation_log SET content = ? WHERE id = ?",
                (new, rid),
            )

        after_total = conn.execute(
            "SELECT COALESCE(SUM(length(content)), 0) FROM conversation_log"
        ).fetchone()[0]
        remain = conn.execute(
            "SELECT COUNT(*) FROM conversation_log "
            "WHERE role = 'assistant' AND content LIKE '[%'"
        ).fetchone()[0]

        print()
        print(f"[OK] 删除 {len(quit_rows)} 行 /quit 轮次，"
              f"剔掉 {len(prefix_rows)} 行格式前缀，"
              f"清理 {len(invisible_rows)} 行零宽字符")
        print(f"     conversation_log 总字符：{before_total:,} -> {after_total:,}")
        print(f"     仍带 '[' 开头的 assistant 行：{remain}（属于正常内容，不用管）")
        print("     session_close 哨兵已保留")
        return 0
    finally:
        ctx.conn.close()


# ============ maintain clean-quit-notes ============

def cmd_clean_quit_notes(args) -> int:
    """清理 note 表里由 /quit 派生出的幻觉记忆。

    老实现里 /quit 会触发一次真实 LLM 调用，抽取器把这段噪声当成了
    用户行为，于是产生了这类假记忆：
        "用户习惯在暧昧氛围升温时突然用 /quit 结束对话，属于一种调戏式的收尾方式"

    处理方式：
    - 整条都在描述 /quit 的 -> 直接删除
    - 除了 /quit 还含其他有效信息的 -> 只剪掉 /quit 那个从句
    """
    from companion_ever.cli import _build_app

    ctx = _build_app(args.db)
    try:
        conn = ctx.conn
        dry = getattr(args, "dry_run", False)

        rows = conn.execute(
            "SELECT id, type, content, importance FROM note "
            "WHERE content LIKE '%/quit%' OR content LIKE '%退出对话%' "
            "   OR content LIKE '%结束对话%'"
        ).fetchall()

        print()
        print("=" * 60)
        print("  清理 note 表里的 /quit 幻觉记忆")
        print("=" * 60)
        print()

        if not rows:
            print("[OK] 没有需要清理的 note")
            return 0

        to_delete = []
        to_trim = []
        for r in rows:
            content = r["content"]
            trimmed = _QUIT_CLAUSE_RE.sub("", content).strip("，,；;。 ")
            if trimmed == content or not trimmed:
                # 剪完就没了 -> 整条删
                to_delete.append(r)
            else:
                to_trim.append((r, trimmed))

        trim_map = {t["id"]: trimmed for t, trimmed in to_trim}
        for r in rows:
            print(f"  [{r['type']}] id={r['id'][:12]}")
            print(f"      原: {_fmt_preview(r['content'], 70)}")
            if r["id"] in trim_map:
                print(f"      改: {_fmt_preview(trim_map[r['id']], 70)}")
            else:
                print("      删: 整条删除")
        print()

        if dry:
            print("[dry-run] 没有改动任何东西。")
            return 0

        if not getattr(args, "no_backup", False):
            bak = _make_backup(conn, args.db)
            if bak:
                print(f"     [backup] {bak}")

        for r in to_delete:
            conn.execute("DELETE FROM note WHERE id = ?", (r["id"],))
        for r, trimmed in to_trim:
            conn.execute(
                "UPDATE note SET content = ? WHERE id = ?", (trimmed, r["id"])
            )

        print(f"[OK] 删除 {len(to_delete)} 条纯 /quit 幻觉 note，"
              f"修剪 {len(to_trim)} 条含 /quit 从句的 note")
        return 0
    finally:
        ctx.conn.close()


__all__ = [
    "cmd_strip_think",
    "cmd_restore_thinking",
    "cmd_clean_history",
    "cmd_clean_quit_notes",
    "cmd_backup",
]
