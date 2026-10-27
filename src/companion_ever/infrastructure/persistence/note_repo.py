"""note_repo.py — note 表的 CRUD + FTS5 全文检索。

设计要点：
- FTS5 全文检索通过 trigger 自动同步（无需手动维护）
- 14 种 type 受 CHECK 约束保护
- source_quote 是防幻觉关键字段
- 返回 Note 实体（替代原 dict）；PERSONA_BASICS 自动解析为 dict
- 时间排序默认 updated_at DESC

FTS5 兼容性：
- 查询自动用双引号包裹，避开 FTS5 的 query syntax（:, *, ^, NEAR 等）
- 触发 OperationalError 时回退到 LIKE，不阻断调用方
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Optional

from companion_ever.domain.entities import Note
from companion_ever.utils.text import extract_keywords
from companion_ever.domain.enums import NoteType
from .mappers import row_to_note


def _new_note_id() -> str:
    """生成新的 note id（UUID 格式）。"""
    return f"note_{uuid.uuid4().hex[:12]}"


def _escape_fts5_query(query: str) -> str:
    """把查询包成 FTS5 字符串字面量（双引号包裹 + 转义内部双引号）。

    这样无论 query 含什么特殊字符（:, *, ^, 中文标点等），FTS5 都按字面字符串处理。
    trigram tokenizer 下这种"字面"匹配仍能正确分词。
    """
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


class NoteRepo:
    """note 表 CRUD + FTS5 检索。

    注意事项：
    - FTS5 通过 schema.sql 里的 trigger 自动同步，repo 无需管理
    - FK 要求 user_id 存在（自动抛 IntegrityError）
    - persona_id 应用层校验归属（由 service 层负责）
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # === 查询 ===

    def get(self, note_id: str) -> Optional[Note]:
        """获取单条笔记。

        Returns:
            None if 不存在
            否则 Note 实体
        """
        row = self.conn.execute(
            "SELECT * FROM note WHERE id = ?",
            (note_id,),
        ).fetchone()
        return row_to_note(row) if row else None

    def find(
        self,
        *,
        user_id: str,
        persona_id: str,
        type: Optional[NoteType] = None,
        types: Optional[list[NoteType]] = None,
        is_archived: bool = False,
        limit: int = 100,
    ) -> list[Note]:
        """按条件查询笔记（限定 user_id + persona_id）。"""
        sql = "SELECT * FROM note WHERE user_id = ? AND persona_id = ?"
        params: list = [user_id, persona_id]

        if not is_archived:
            sql += " AND is_archived = 0"
        elif is_archived:
            sql += " AND is_archived = 1"
        if type is not None and types is not None:
            raise ValueError("type 和 types 是互斥的")
        if type is not None:
            sql += " AND type = ?"
            params.append(type.value)
        elif types is not None:
            placeholders = ",".join("?" * len(types))
            sql += f" AND type IN ({placeholders})"
            params.extend(t.value for t in types)

        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [row_to_note(r) for r in rows]

    def search(
        self,
        query: str,
        *,
        user_id: str,
        persona_id: str,
        limit: int = 20,
    ) -> list[Note]:
        """关键词检索（FTS5 trigram + LIKE 兜底）。

        **曾经的 bug**：把整个 query 当成一个字面量去匹配
        （FTS5 的 MATCH 和 LIKE 都是），于是用户输入"我最近面试怎么样了"
        这种自然语言时，两条路径都要求整句原样出现 → 必然 0 命中。
        结果是模型看到的"相关记忆"和当前这句话其实毫无关系。

        **现在的做法**：
        1. 把 query 拆成 2 字 / 3 字关键词（见 utils/text.py）
        2. FTS5 用 3 字关键词 OR 组合（索引快，字面子串精确）
        3. LIKE 用 2 字 + 3 字关键词 OR 组合（中文的主力：面试 / 秋招 / 研三）
        4. 两条来源轮转合并，保证都有机会进入结果
        5. 精度交给上层"命中关键词数"打分（见 RetrievalService._score）

        Args:
            query: 用户的原始输入（可以是整句话）
            user_id, persona_id: 限定范围
            limit: 最多返回多少条

        Returns:
            候选笔记列表（顺序为两路轮转，精确度由上层重新排序）
        """
        if not query or not query.strip():
            return []

        short_kw, long_kw = extract_keywords(query)
        fts_notes: list[Note] = []
        like_notes: list[Note] = []

        # 1. FTS5 检索（3 字关键词 OR）
        if long_kw:
            fts_query = " OR ".join(_escape_fts5_query(k) for k in long_kw)
            try:
                rows = self.conn.execute(
                    "SELECT n.*, f.rank AS fts_rank "
                    "FROM note_fts f "
                    "JOIN note n ON n.rowid = f.rowid "
                    "WHERE note_fts MATCH ? "
                    "  AND n.user_id = ? AND n.persona_id = ? "
                    "  AND n.is_archived = 0 "
                    "ORDER BY f.rank LIMIT ?",
                    (fts_query, user_id, persona_id, max(limit * 2, limit)),
                ).fetchall()
                fts_notes = [row_to_note(r) for r in rows]
            except sqlite3.OperationalError:
                # FTS5 query syntax 异常 → 只走 LIKE
                fts_notes = []

        # 2. LIKE 兜底（2 字 + 3 字关键词 OR，再加整句精确匹配）
        patterns: list[str] = list(short_kw) + list(long_kw)
        exact = query.strip()
        if exact:
            patterns.append(exact)
        if patterns:
            where = " OR ".join(["content LIKE ?"] * len(patterns))
            rows = self.conn.execute(
                f"SELECT * FROM note "
                f"WHERE user_id = ? AND persona_id = ? "
                f"  AND is_archived = 0 "
                f"  AND ({where}) "
                f"ORDER BY updated_at DESC LIMIT ?",
                (
                    user_id,
                    persona_id,
                    *[f"%{pat}%" for pat in patterns],
                    max(limit * 3, limit),
                ),
            ).fetchall()
            like_notes = [row_to_note(r) for r in rows]

        # 3. 轮转合并（保证 FTS 和 LIKE 两条来源都有机会进入结果）
        merged: list[Note] = []
        seen_ids: set[str] = set()
        for i in range(max(len(fts_notes), len(like_notes))):
            for bucket in (fts_notes, like_notes):
                if i < len(bucket):
                    note = bucket[i]
                    if note.id not in seen_ids:
                        seen_ids.add(note.id)
                        merged.append(note)
                        if len(merged) >= limit:
                            return merged

        return merged

    def create(
        self,
        *,
        user_id: str,
        persona_id: str,
        type: NoteType,
        content: str,
        source_quote: Optional[str] = None,
        importance: float = 0.5,
    ) -> str:
        """创建一条笔记。

        FTS5 索引由 trigger 自动同步，无需手动管理。

        Args:
            user_id: 用户 id（必须已存在，FK 自动校验）
            persona_id: persona id
            type: 14 种 NoteType 之一
            content: 笔记正文（字符串；PERSONA_BASICS 由调用方负责 JSON 序列化）
            source_quote: 原话引用（防幻觉用，可选）
            importance: 重要性 [0, 1]

        Returns:
            新笔记的 id

        Raises:
            sqlite3.IntegrityError: user_id 不存在
            ValueError: content 空 / type 无效（CHECK 约束）
        """
        if not content or not content.strip():
            raise ValueError("content 不能为空")

        note_id = _new_note_id()
        self.conn.execute(
            "INSERT INTO note "
            "(id, user_id, persona_id, type, content, source_quote, importance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                note_id,
                user_id,
                persona_id,
                type.value,
                content.strip(),
                source_quote,
                importance,
            ),
        )
        return note_id

    def update(
        self,
        note_id: str,
        *,
        content: Optional[str] = None,
        importance: Optional[float] = None,
        decay_score: Optional[float] = None,
        is_archived: Optional[int] = None,
        source_quote: Optional[str] = None,
    ) -> None:
        """更新笔记字段（只更新提供的字段）。

        updated_at 自动刷新。
        FTS5 索引由 trigger 自动同步（如果 content 改变）。
        """
        updates = []
        params: list = []
        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if importance is not None:
            updates.append("importance = ?")
            params.append(importance)
        if decay_score is not None:
            updates.append("decay_score = ?")
            params.append(decay_score)
        if is_archived is not None:
            updates.append("is_archived = ?")
            params.append(is_archived)
        if source_quote is not None:
            updates.append("source_quote = ?")
            params.append(source_quote)

        if not updates:
            return

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(note_id)
        sql = f"UPDATE note SET {', '.join(updates)} WHERE id = ?"
        self.conn.execute(sql, params)

    def archive(self, note_id: str) -> bool:
        """软删除（is_archived=1）。

        Returns:
            True if 成功，False if 笔记不存在
        """
        cur = self.conn.execute(
            "UPDATE note SET is_archived = 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND is_archived = 0",
            (note_id,),
        )
        return cur.rowcount > 0

    def unarchive(self, note_id: str) -> bool:
        """恢复软删除的笔记。

        Returns:
            True if 成功，False if 笔记不存在或未归档
        """
        cur = self.conn.execute(
            "UPDATE note SET is_archived = 0, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND is_archived = 1",
            (note_id,),
        )
        return cur.rowcount > 0

    def bump_access(self, note_id: str) -> bool:
        """更新 last_accessed_at（Ebbinghaus 公式依赖）。

        Returns:
            True if 成功，False if 笔记不存在
        """
        cur = self.conn.execute(
            "UPDATE note SET last_accessed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (note_id,),
        )
        return cur.rowcount > 0


__all__ = ["NoteRepo"]