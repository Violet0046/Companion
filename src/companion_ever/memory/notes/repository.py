"""note 表的 CRUD + FTS5 全文检索。"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Optional

from companion_ever.domain.entities import Note
from companion_ever.utils.text import extract_keywords
from companion_ever.domain.enums import MemoryKind


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



def row_to_note(row: sqlite3.Row) -> Note:
    """note 表行 → 经历/事实实体；角色配置不经过这里。"""
    return Note(
        id=row['id'], user_id=row['user_id'], persona_id=row['persona_id'],
        kind=MemoryKind(row['kind']), content=row['content'], source_quote=row['source_quote'],
        importance=row['importance'], decay_score=row['decay_score'], is_archived=bool(row['is_archived']),
        created_at=row['created_at'], updated_at=row['updated_at'], last_accessed_at=row['last_accessed_at'],
        reality=row['reality'], event_time=row['event_time'], time_basis=row['time_basis'],
        revision=row['revision'],
    )



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

    def visible_ids(self,user_id,persona_id,conversation_id,*,include_private=False):
        scope=self.conn.execute("SELECT m.joined_after_message_id FROM conversation c JOIN conversation_member m ON m.conversation_id=c.id "
            "WHERE c.id=? AND c.user_id=? AND c.kind='group' AND m.persona_id=? AND m.left_after_message_id IS NULL",(conversation_id,user_id,persona_id)).fetchone()
        if scope is None:raise PermissionError('当前角色不可访问这个群聊')
        if include_private:
            # Chat-only reading. Extraction and summaries retain the default group-only scope.
            return {r[0] for r in self.conn.execute(
                'SELECT n.id FROM note n WHERE n.user_id=? AND n.persona_id=? '
                'AND EXISTS(SELECT 1 FROM note_evidence e WHERE e.note_id=n.id) AND NOT EXISTS('
                'SELECT 1 FROM note_evidence e LEFT JOIN conversation_log l ON l.id=e.message_id '
                'LEFT JOIN conversation c ON c.id=l.conversation_id WHERE e.note_id=n.id AND '
                '(l.id IS NULL OR c.id IS NULL OR l.user_id!=? OR c.user_id!=? OR coalesce('
                "(c.kind='private' AND c.persona_id=? AND l.persona_id=?) OR "
                "(c.kind='group' AND c.id=? AND (l.id>? OR EXISTS(SELECT 1 FROM conversation_membership_history h "
                "WHERE h.conversation_id=c.id AND h.persona_id=n.persona_id AND l.id>h.joined_after_message_id AND l.id<=h.left_after_message_id))),0)=0))",
                (user_id,persona_id,user_id,user_id,persona_id,persona_id,conversation_id,scope[0]))}
        return {r[0] for r in self.conn.execute('SELECT n.id FROM note n WHERE n.user_id=? AND n.persona_id=? '
            'AND EXISTS(SELECT 1 FROM note_evidence e WHERE e.note_id=n.id) AND NOT EXISTS('
            'SELECT 1 FROM note_evidence e JOIN conversation_log l ON l.id=e.message_id WHERE e.note_id=n.id '
            'AND (l.user_id!=? OR l.conversation_id!=? OR NOT (l.id>? OR EXISTS(SELECT 1 FROM conversation_membership_history h '
            'WHERE h.conversation_id=l.conversation_id AND h.persona_id=n.persona_id AND l.id>h.joined_after_message_id AND l.id<=h.left_after_message_id))))',
            (user_id,persona_id,user_id,conversation_id,scope[0]))}

    def sources(self,user,persona,note_ids):
        import json
        result={nid:[] for nid in note_ids}
        for row in self.conn.execute('SELECT DISTINCT n.id AS note_id,c.id,c.kind,c.name FROM note n JOIN note_evidence e ON e.note_id=n.id '
            'JOIN conversation_log m ON m.id=e.message_id JOIN conversation c ON c.id=m.conversation_id '
            'WHERE n.user_id=? AND n.persona_id=? AND m.user_id=n.user_id AND c.user_id=n.user_id AND n.id IN (SELECT value FROM json_each(?)) '
            'ORDER BY c.id',(user,persona,json.dumps(list(note_ids)))):
            data=dict(row);nid=data.pop('note_id');result[nid].append(data)
        return result

    @staticmethod
    def _allowed_sql(allowed_ids,alias=''):
        if allowed_ids is None:return '',[]
        import json
        return ' AND '+alias+'id IN (SELECT value FROM json_each(?))',[json.dumps(sorted(allowed_ids))]

    def exists_active(self, user_id, persona_id,*,allowed_ids=None):
        clause,params=self._allowed_sql(allowed_ids)
        return bool(self.conn.execute('SELECT 1 FROM note WHERE user_id=? AND persona_id=? '
                                     'AND is_archived=0'+clause+' LIMIT 1', (user_id, persona_id,*params)).fetchone())

    def evidence(self, note_id, user_id, persona_id, *, limit=3, user_only=False):
        role = " AND m.role='user'" if user_only else ''
        return [dict(row) for row in self.conn.execute(
            'SELECT e.message_id,e.quote FROM note_evidence e JOIN conversation_log m ON m.id=e.message_id '
            'JOIN note n ON n.id=e.note_id JOIN conversation c ON c.id=m.conversation_id '
            'WHERE e.note_id=? AND n.user_id=? AND n.persona_id=? AND m.user_id=n.user_id AND '
            "((c.kind='private' AND c.persona_id=n.persona_id) OR (c.kind='group' AND EXISTS(SELECT 1 FROM conversation_member cm "
            'WHERE cm.conversation_id=c.id AND cm.persona_id=n.persona_id AND m.id>cm.joined_after_message_id '
            'AND (cm.left_after_message_id IS NULL OR m.id<=cm.left_after_message_id) OR cm.conversation_id=c.id AND cm.persona_id=n.persona_id AND EXISTS('
            'SELECT 1 FROM conversation_membership_history h WHERE h.conversation_id=c.id AND h.persona_id=n.persona_id '
            'AND m.id>h.joined_after_message_id AND m.id<=h.left_after_message_id))))' + role + ' ORDER BY e.message_id DESC LIMIT ?',
            (note_id, user_id, persona_id, limit))]

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
        kind: Optional[MemoryKind] = None,
        kinds: Optional[list[MemoryKind]] = None,
        is_archived: bool = False,
        limit: int = 100,
        allowed_ids=None,
    ) -> list[Note]:
        """按条件查询笔记（限定 user_id + persona_id）。"""
        sql = "SELECT * FROM note WHERE user_id = ? AND persona_id = ?"
        params: list = [user_id, persona_id]
        clause,extra=self._allowed_sql(allowed_ids)
        sql+=clause;params.extend(extra)

        if not is_archived:
            sql += " AND is_archived = 0"
        elif is_archived:
            sql += " AND is_archived = 1"
        if kind is not None and kinds is not None:
            raise ValueError("kind 和 kinds 是互斥的")
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind.value)
        elif kinds is not None:
            placeholders = ",".join("?" * len(kinds))
            sql += f" AND kind IN ({placeholders})"
            params.extend(t.value for t in kinds)

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
        include_archived: bool = False,
        allowed_ids=None,
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
        fts_scope,fts_args=self._allowed_sql(allowed_ids,'n.')
        like_scope,like_args=self._allowed_sql(allowed_ids)

        # 1. FTS5 检索（3 字关键词 OR）
        if long_kw:
            fts_query = " OR ".join(_escape_fts5_query(k) for k in long_kw)
            try:
                rows = self.conn.execute(
                    "SELECT n.*, f.rank AS fts_rank "
                    "FROM note_fts f "
                    "JOIN note n ON n.id = f.note_id "
                    "WHERE note_fts MATCH ? "
                    "  AND n.user_id = ? AND n.persona_id = ? "
                    f"  AND (n.is_archived = 0 OR {int(include_archived)}) "
                    +fts_scope+" ORDER BY f.rank LIMIT ?",
                    (fts_query, user_id, persona_id,*fts_args, max(limit * 2, limit)),
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
            where = " OR ".join(["content LIKE ? ESCAPE '!'"] * len(patterns))
            rows = self.conn.execute(
                f"SELECT * FROM note "
                f"WHERE user_id = ? AND persona_id = ? "
                f"  AND (is_archived = 0 OR {int(include_archived)}) "
                f"  AND ({where}) "
                +like_scope+
                f"ORDER BY updated_at DESC LIMIT ?",
                (
                    user_id,
                    persona_id,
                    *["%" + pat.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%" for pat in patterns],
                    *like_args,
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
        kind: MemoryKind,
        content: str,
        source_quote: Optional[str] = None,
        importance: float = 0.5,
        reality: str = 'uncertain',
        event_time: Optional[str] = None,
        time_basis: str = '',
    ) -> str:
        """创建一条笔记。

        FTS5 索引由 trigger 自动同步，无需手动管理。

        Args:
            user_id: 用户 id（必须已存在，FK 自动校验）
            persona_id: persona id
            kind: episode 或 fact
            content: 笔记正文字符串
            source_quote: 原话引用（防幻觉用，可选）
            importance: 重要性 [0, 1]

        Returns:
            新笔记的 id

        Raises:
            sqlite3.IntegrityError: user_id 不存在
            ValueError: content 空 / kind 无效（CHECK 约束）
        """
        if not content or not content.strip():
            raise ValueError("content 不能为空")

        note_id = _new_note_id()
        self.conn.execute(
            "INSERT INTO note "
            "(id, user_id, persona_id, kind, content, source_quote, importance, reality, event_time, time_basis) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                note_id,
                user_id,
                persona_id,
                kind.value,
                content.strip(),
                source_quote,
                importance,
                reality,
                event_time,
                time_basis,
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
        updates.append("revision = revision + 1")
        params.append(note_id)
        sql = f"UPDATE note SET {', '.join(updates)} WHERE id = ?"
        self.conn.execute(sql, params)

    def edit_content(self, note: Note, content: str) -> None:
        """调用方持有事务；保存旧版本，正文变更由触发器进入 fact 流水。"""
        self.conn.execute('INSERT INTO note_revision(note_id,revision,content,reality,event_time,time_basis,updated_at,job_id) '
            'VALUES (?,?,?,?,?,?,?,NULL)', (note.id,note.revision,note.content,note.reality,
                                           note.event_time,note.time_basis,note.updated_at))
        self.update(note.id,content=content)

    def archive(self, note_id: str) -> bool:
        """软删除（is_archived=1）。

        Returns:
            True if 成功，False if 笔记不存在
        """
        cur = self.conn.execute(
            "UPDATE note SET is_archived = 1, revision=revision+1, updated_at = CURRENT_TIMESTAMP "
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
            "UPDATE note SET is_archived = 0, revision=revision+1, updated_at = CURRENT_TIMESTAMP "
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
