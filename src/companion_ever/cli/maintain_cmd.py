"""cli/maintain_cmd.py — 数据维护命令。

子命令：
- maintain strip-think   剥掉历史 assistant 消息里的 <think> 块

为什么需要：
推理模型（MiniMax-M3 等）的回复里带 <think>...</think> 思考块。
这些内容对后续对话没有任何价值，却会：
- 占掉 prompt 的 90%+ token（实测 60,921 -> 7,814 字符）
- 污染模型上下文（让模型看到自己上一轮的内心戏）

新写入的消息已经不存 think 块了（chat_service / chat_workflow 都会 strip），
这个命令用来清理**历史存量**。
"""
from __future__ import annotations

from companion_ever.application.llm_json import THINK_PATTERN


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


__all__ = ["cmd_strip_think"]
