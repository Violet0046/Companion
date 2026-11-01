"""对话原文的 CRUD。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from companion_ever.domain.entities import ConversationMessage
from companion_ever.utils.timeutil import tz_modifier



def row_to_conversation_message(row: sqlite3.Row) -> ConversationMessage:
    """conversation_log 表行 → ConversationMessage 实体。"""
    return ConversationMessage(
        id=row["id"],
        user_id=row["user_id"],
        persona_id=row["persona_id"],
        turn_index=row["turn_index"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
        client_message_id=row['client_message_id'] if 'client_message_id' in row.keys() else None,
    )



class ConversationRepo:
    """conversation_log 表 CRUD。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def ensure_conversation(self, user_id: str, persona_id: str) -> int:
        self.conn.execute("INSERT OR IGNORE INTO conversation(user_id,persona_id) VALUES (?,?)",
                          (user_id, persona_id))
        return self.conn.execute("SELECT id FROM conversation WHERE user_id=? AND persona_id=?",
                                 (user_id, persona_id)).fetchone()[0]

    def unread_count(self, user_id: str, persona_id: str, after_id: int = 0) -> dict:
        row = self.conn.execute("SELECT count(*),coalesce(max(id),0) FROM conversation_log "
            "WHERE user_id=? AND persona_id=? AND role='assistant' AND id>?",
            (user_id, persona_id, after_id)).fetchone()
        return {'count': row[0], 'latest_id': row[1]}

    def mark_read(self, user, persona, message_id):
        self.conn.execute('UPDATE conversation SET last_read_message_id=max(last_read_message_id,?) '
            'WHERE user_id=? AND persona_id=? AND EXISTS (SELECT 1 FROM conversation_log '
            "WHERE id=? AND conversation_id=conversation.id AND role='assistant')", (message_id,user,persona,message_id))

    def should_notify(self, user, persona, message_id):
        return bool(self.conn.execute('SELECT 1 FROM conversation c JOIN conversation_log m ON m.conversation_id=c.id '
            "WHERE c.user_id=? AND c.persona_id=? AND m.id=? AND m.role='assistant' AND c.last_read_message_id<m.id "
            "AND NOT EXISTS (SELECT 1 FROM conversation_log newer WHERE newer.conversation_id=c.id AND newer.role='user' AND newer.id>m.id)",
            (user,persona,message_id)).fetchone())

    def list_conversations(self):
        return [dict(row) for row in self.conn.execute('SELECT * FROM conversation')]

    def pending_input(self, user_id, persona_id, after_id):
        row = self.conn.execute("SELECT sum(role='user') AS user_count,max(created_at) AS last_at,"
            "min(CASE WHEN role='user' THEN created_at END) AS oldest_user_at FROM conversation_log "
            "WHERE user_id=? AND persona_id=? AND id>? AND role IN ('user','assistant')",
            (user_id, persona_id, after_id)).fetchone()
        generating = bool(self.conn.execute("SELECT 1 FROM generation g JOIN conversation c ON c.id=g.conversation_id "
            "WHERE c.user_id=? AND c.persona_id=? AND g.status IN ('queued','running') LIMIT 1",
            (user_id, persona_id)).fetchone())
        return {**dict(row), 'generating': generating}

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
            "(user_id, persona_id, turn_index, role, content,conversation_id,sender_kind,sender_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, persona_id, turn_index, role, content.strip(),
             self.ensure_conversation(user_id,persona_id),
             "user" if role == "user" else "persona", user_id if role == "user" else persona_id),
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
            "(user_id, persona_id, turn_index, role, content,conversation_id,sender_kind,sender_id) "
            "VALUES (?, ?, ?, 'session_close', '[session_close]', ?, 'system', ?)",
            (user_id, persona_id, turn_index, self.ensure_conversation(user_id,persona_id), persona_id),
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
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at, client_message_id "
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
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at, client_message_id "
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

    def get_active_session(
        self, *, user_id: str, persona_id: str,
    ) -> Optional[dict]:
        """返回"未关闭 session"的信息。

        即：最后一个 session_close marker 之后还有 message，则返回
        {"last_message_id": int, "last_message_at": str}；没有则返回 None。

        用途：判定是否应该自动关闭 session（空闲超时）。
        """
        row = self.conn.execute(
            "SELECT MAX(id) AS mid, MAX(created_at) AS mat FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? "
            "  AND role IN ('user', 'assistant') "
            "  AND id > COALESCE("
            "      (SELECT MAX(id) FROM conversation_log "
            "       WHERE user_id = ? AND persona_id = ? AND role = 'session_close'), 0)",
            (user_id, persona_id, user_id, persona_id),
        ).fetchone()
        if row is None or row["mid"] is None:
            return None
        return {
            "last_message_id": int(row["mid"]),
            "last_message_at": row["mat"],
        }

    def list_active_sessions(self) -> list[dict]:
        """列出所有"未关闭的 session"（用于服务器定时扫描）。"""
        rows = self.conn.execute(
            "SELECT c.user_id AS user_id, c.persona_id AS persona_id, "
            "       MAX(c.id) AS mid, MAX(c.created_at) AS mat "
            "FROM conversation_log c "
            "WHERE c.role IN ('user', 'assistant') "
            "  AND c.id > COALESCE(("
            "      SELECT MAX(m.id) FROM conversation_log m "
            "       WHERE m.user_id = c.user_id AND m.persona_id = c.persona_id "
            "         AND m.role = 'session_close'), 0) "
            "GROUP BY c.user_id, c.persona_id"
        ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "persona_id": r["persona_id"],
                "last_message_id": int(r["mid"]),
                "last_message_at": r["mat"],
            }
            for r in rows
        ]

    # === 游标分页（微信式无限上翻）===

    def get_page(
        self,
        *,
        user_id: str,
        persona_id: str,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
        date: Optional[str] = None,
        limit: int = 50,
        tz_offset_hours: int = 0,
    ) -> list[ConversationMessage]:
        """按游标取一页消息（始终返回时间正序）。

        语义（与微信的上翻加载一致）：
        - before_id=X → 取 id < X 的最近 limit 条（即「紧跟着 X 上面」）
        - after_id=X  → 取 id > X 的最近 limit 条（即「从 X 往后」）
        - 都不给 → 取最新的 limit 条（首次加载）
        - date 可与其中一个组合（“看某一天”）

        Args:
            before_id: 游标，取这之前的
            after_id: 游标，取这之后的
            date: "YYYY-MM-DD" 形式的日期
            limit: 最多返回多少条
        """
        sql = (
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at, client_message_id "
            "FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? AND role IN ('user', 'assistant')"
        )
        params: list = [user_id, persona_id]

        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
            order = "DESC"          # 先取最近的，再反转成正序
        elif after_id is not None:
            sql += " AND id > ?"
            params.append(after_id)
            order = "ASC"
        else:
            order = "DESC"

        if date:
            # date 是**本地日期**，DB 里存的是 UTC，要先转换再比
            sql += " AND datetime(created_at, ?) LIKE ?"
            params.append(tz_modifier(tz_offset_hours))
            params.append(f"{date}%")

        sql += f" ORDER BY id {order} LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        msgs = [row_to_conversation_message(r) for r in rows]
        return list(reversed(msgs)) if order == "DESC" else msgs

    def list_dates(
        self, *, user_id: str, persona_id: str, tz_offset_hours: int = 0,
    ) -> list[dict]:
        """列出有对话的日期（降序）+ 每天的消息数 + 当天首/末消息 id。

        first_id / last_id 是给"日历跳转"用的：
        前端点某一天之后，要能定位到**那天第一条**消息，
        而不是像以前那样把视图限制在那一天里。
        """
        # 按**本地日期**分组：DB 存的是 UTC，
        # 直接 substr 会把凌晨 0-8 点的消息算到前一天去
        rows = self.conn.execute(
            "SELECT substr(datetime(created_at, ?), 1, 10) AS d, COUNT(*) AS n, "
            "       MIN(id) AS first_id, MAX(id) AS last_id "
            "FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? AND role IN ('user', 'assistant') "
            "GROUP BY d ORDER BY d DESC",
            (tz_modifier(tz_offset_hours), user_id, persona_id),
        ).fetchall()
        return [
            {
                "date": r["d"],
                "count": r["n"],
                "first_id": r["first_id"],
                "last_id": r["last_id"],
            }
            for r in rows
        ]

    def search(
        self,
        *,
        user_id: str,
        persona_id: str,
        patterns: list[str],
        limit: int = 50,
    ) -> list[ConversationMessage]:
        """按多个关键词 OR 搜索消息。

        为什么不用 FTS5：中文可能只有 2 字（“面试”“阳台”），
        trigram 分词器要求 ≥3 字，而每次扫描整表对个人应用可以接受。
        大量用户时再换 FTS5（见 START_HERE.md）。
        """
        if not patterns:
            return []
        where = " OR ".join(["content LIKE ?"] * len(patterns))
        sql = (
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at, client_message_id "
            "FROM conversation_log "
            f"WHERE user_id = ? AND persona_id = ? AND role IN ('user', 'assistant') "
            f"  AND ({where}) "
            "ORDER BY id DESC LIMIT ?"
        )
        params = [user_id, persona_id] + [f"%{p}%" for p in patterns] + [limit]
        rows = self.conn.execute(sql, params).fetchall()
        return [row_to_conversation_message(r) for r in rows]

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
            "SELECT id, user_id, persona_id, turn_index, role, content, created_at, client_message_id "
            "FROM conversation_log "
            "WHERE user_id = ? AND persona_id = ? "
            "  AND role IN ('user', 'assistant') "
            "ORDER BY id DESC LIMIT ?",
            (user_id, persona_id, limit),
        ).fetchall()
        return [row_to_conversation_message(r) for r in reversed(rows)]

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
