"""conversation_repo.py — 对话原文的 CRUD。

设计要点：
- AUTOINCREMENT id（不是 UUID，方便按时间排序）
- turn_index 是用户/助手同轮共享的索引（user+assistant 同值）
- role 是 'user' / 'assistant' / 'session_close'
  - 'session_close' 是 sentinel marker，由 ConversationService.log_session_close 写入，
    表示"一个 session 在此关闭"。抽取服务用它来识别 session 边界。
- UNIQUE(user_id, persona_id, turn_index, role) 防止同角色重复写
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from companion_ever.domain.entities import ConversationMessage
from .mappers import row_to_conversation_message


class ConversationRepo:
    """conversation_log 表 CRUD。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def log_turn(
        self,
        *,
        user_id: str,
        persona_id: str,
        turn_index: int,
        role: str,
        content: str,
    ) -> int:
        """记录一条对话消息（user / assistant）。

        Args:
            user_id: 用户 id
            persona_id: persona id
            turn_index: 同一轮 user + assistant 共享
            role: 'user' 或 'assistant'
            content: 消息原文

        Returns:
            新记录的 id（自增整数）

        Raises:
            ValueError: content 空 / role 不合法
            sqlite3.IntegrityError: UNIQUE 冲突（同 (user, persona, turn, role) 已存在）
        """
        if not content or not content.strip():
            raise ValueError("content 不能为空")
        if role not in ("user", "assistant"):
            raise ValueError(f"role 必须是 'user' 或 'assistant'，实际 {role!r}")

        cur = self.conn.execute(
            "INSERT INTO conversation_log "
            "(user_id, persona_id, turn_index, role, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, persona_id, turn_index, role, content.strip()),
        )
        return cur.lastrowid

    def log_session_close(
        self,
        *,
        user_id: str,
        persona_id: str,
        turn_index: int,
    ) -> int:
        """在 conversation_log 写一行 role='session_close' 的 marker。

        这是 session 边界标记——抽取服务用它来识别"哪个 session 刚结束"。

        turn_index 的选择：
        - 一般由调用方传入当前 max(turn_index) + 1，避免与既有 UNIQUE 冲突
        - 调用方应保证唯一性（schema 的 UNIQUE 约束会兜底）

        Args:
            user_id: 用户 id
            persona_id: persona id
            turn_index: marker 行的 turn_index 值

        Returns:
            新 marker 的 id

        Raises:
            sqlite3.IntegrityError: UNIQUE 冲突（同 (user, persona, turn, session_close) 已存在）
        """
        cur = self.conn.execute(
            "INSERT INTO conversation_log "
            "(user_id, persona_id, turn_index, role, content) "
            "VALUES (?, ?, ?, 'session_close', '[session_close]')",
            (user_id, persona_id, turn_index),
        )
        return cur.lastrowid

    def get_last_n_session_closes(
        self,
        *,
        user_id: str,
        persona_id: str,
        n: int,
    ) -> list[ConversationMessage]:
        """返回最近 n 个 session_close marker（时间升序，最早在前）。

        用于找到"上一个 marker"和"最新 marker"，从而确定要处理的 session 范围。

        Args:
            n: 最多返回几个 marker（默认 2 即可定位最近一个 session）

        Returns:
            按 created_at 升序的 marker 列表（可能少于 n 个）
        """
        rows = self.conn.execute(
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at "
            "FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? AND role = 'session_close' "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, persona_id, n),
        ).fetchall()
        # 反转成时间正序（最早在前）
        return [row_to_conversation_message(r) for r in reversed(rows)]

    def get_messages_between(
        self,
        *,
        user_id: str,
        persona_id: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        since_id: Optional[int] = None,
        until_id: Optional[int] = None,
        limit: int = 1000,
    ) -> list[ConversationMessage]:
        """获取 user/assistant 消息（自动过滤 session_close）。

        支持两种边界：
        - 时间：(since, until] —— 包含边界
        - id：(since_id, until_id] —— since_id 严格大于，until_id 包含

        两种边界可以同时使用（AND 关系）。
        都为 None 表示取所有。

        Args:
            user_id, persona_id: 限定范围
            since: 起始时间（包含）
            until: 截止时间（包含）
            since_id: 起始 id（不包含）
            until_id: 截止 id（包含）
            limit: 最多返回多少条

        Returns:
            按 id 升序的列表（**取最近 limit 条**，不是最早的）
        """
        sql = (
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at "
            "FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? "
            "  AND role IN ('user', 'assistant')"
        )
        params: list = [user_id, persona_id]

        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        if until is not None:
            sql += " AND created_at <= ?"
            params.append(until.strftime("%Y-%m-%d %H:%M:%S"))
        if since_id is not None:
            sql += " AND id > ?"
            params.append(since_id)
        if until_id is not None:
            sql += " AND id <= ?"
            params.append(until_id)

        # 取**最近** limit 条（再反转成时间正序）。
        # 曾经的 bug：用 ORDER BY id ASC LIMIT ?，没有边界时取到的是最早的 N 条——
        # 导致 prompt 里装的是一个月前的对话，模型完全看不到刚才聊了什么。
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [row_to_conversation_message(r) for r in reversed(rows)]

    # === thinking_log ===

    def log_thinking(
        self,
        *,
        message_id: int,
        user_id: str,
        persona_id: str,
        turn_index: int,
        content: str,
    ) -> int:
        """记录一条思考过程（与对应的 assistant 消息关联）。"""
        cur = self.conn.execute(
            "INSERT INTO thinking_log "
            "(message_id, user_id, persona_id, turn_index, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, user_id, persona_id, turn_index, content),
        )
        return cur.lastrowid

    def get_thinking_map(
        self,
        *,
        user_id: str,
        persona_id: str,
        message_ids: list[int],
    ) -> dict[int, str]:
        """批量取思考记录，返回 {message_id: think_content}。"""
        if not message_ids:
            return {}
        out: dict[int, str] = {}
        CHUNK = 400
        for i in range(0, len(message_ids), CHUNK):
            chunk = message_ids[i : i + CHUNK]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT message_id, content FROM thinking_log "
                f"WHERE user_id = ? AND persona_id = ? AND message_id IN ({marks})",
                (user_id, persona_id, *chunk),
            ).fetchall()
            for r in rows:
                out[int(r["message_id"])] = r["content"]
        return out

    def delete_all_thinking(self, *, user_id: str, persona_id: str) -> int:
        """删除该 persona 的全部思考记录（隐私用途）。"""
        cur = self.conn.execute(
            "DELETE FROM thinking_log WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        )
        return cur.rowcount

    def delete_session_close(self, marker_id: int) -> bool:
        """删除一个 session_close marker（抽取失败时回滚用）。

        为什么需要：
        抽取失败时 marker 已经写入了，而抽取的范围是
        [上一个 marker, 本次 marker]。如果保留 marker，下次抽取只会
        覆盖 [本次 marker, 下次 marker]，这段内容就永远不会被抽取了。
        回滚掉 marker，下次抽取就会自动补上。
        """
        cur = self.conn.execute(
            "DELETE FROM conversation_log WHERE id = ? AND role = 'session_close'",
            (marker_id,),
        )
        return cur.rowcount > 0

    def get_max_turn_index(self, *, user_id: str, persona_id: str) -> int:
        """返回该 (user, persona) 下最大的 turn_index（没有则 0）。

        比"拉 10000 条消息再取 max"快几个数量级 —— 每轮对话都要用。
        """
        row = self.conn.execute(
            "SELECT MAX(turn_index) FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        ).fetchone()
        return int(row[0] or 0)

    def get_recent_messages(
        self,
        *,
        user_id: str,
        persona_id: str,
        limit: int = 20,
    ) -> list[ConversationMessage]:
        """获取最近的 N 条消息（按 id DESC，取完后反转成时间正序）。

        自动过滤掉 session_close marker 行（只返回 user/assistant 消息）。

        Returns:
            按时间正序的列表（ConversationMessage 实体）
        """
        rows = self.conn.execute(
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at "
            "FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? "
            "  AND role IN ('user', 'assistant') "
            "ORDER BY id DESC LIMIT ?",
            (user_id, persona_id, limit),
        ).fetchall()
        return [row_to_conversation_message(r) for r in reversed(rows)]

    def get_since(
        self,
        *,
        user_id: str,
        persona_id: str,
        since: datetime,
        limit: int = 100,
    ) -> list[ConversationMessage]:
        """获取 since 之后的所有对话消息（按时间正序，自动过滤 session_close）。

        Args:
            user_id, persona_id: 限定范围
            since: 起始时间（包含）
            limit: 最多返回多少条

        Returns:
            按时间正序的列表
        """
        since_str = since.strftime("%Y-%m-%d %H:%M:%S")
        rows = self.conn.execute(
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at "
            "FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? "
            "  AND role IN ('user', 'assistant') "
            "  AND created_at >= ? "
            "ORDER BY id ASC LIMIT ?",
            (user_id, persona_id, since_str, limit),
        ).fetchall()
        return [row_to_conversation_message(r) for r in rows]

    def get_last_conversation_at(
        self,
        *,
        user_id: str,
        persona_id: str,
    ) -> Optional[str]:
        """获取最近一次 user/assistant 对话的时间（用于 system prompt 的时间感知）。

        自动过滤掉 session_close marker——只看真实对话。

        Returns:
            ISO 时间字符串，或 None（从未对话过）
        """
        row = self.conn.execute(
            "SELECT MAX(created_at) AS last_at "
            "FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? "
            "  AND role IN ('user', 'assistant')",
            (user_id, persona_id),
        ).fetchone()
        if row is None or row["last_at"] is None:
            return None
        return row["last_at"]

    def count(self, *, user_id: str, persona_id: str) -> int:
        """统计 user/assistant 对话总条数（不含 session_close marker）。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? "
            "  AND role IN ('user', 'assistant')",
            (user_id, persona_id),
        ).fetchone()
        return row["c"]

    def delete_all(self, *, user_id: str, persona_id: str) -> int:
        """删除 (user, persona) 的所有对话（含 session_close marker）。

        Returns:
            删除的条数
        """
        cur = self.conn.execute(
            "DELETE FROM conversation_log WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        )
        return cur.rowcount


__all__ = ["ConversationRepo"]