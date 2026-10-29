"""Read a bounded neighbourhood; both endpoints must belong to the same scope."""
class NoteLinkRepo:
    def __init__(self, conn):
        self.conn = conn

    def neighbours(self, ids, user_id, persona_id, limit=100):
        if not ids:
            return []
        marks = ','.join('?' for _ in ids)
        rows = self.conn.execute(
            'SELECT l.* FROM note_link l JOIN note a ON a.id=l.from_id JOIN note b ON b.id=l.to_id '
            'WHERE a.user_id=? AND a.persona_id=? AND b.user_id=? AND b.persona_id=? '
            "AND a.type!='persona_basics' AND b.type!='persona_basics' "
            "AND l.link_type IN ('supports','continues','supersedes') "
            f'AND (l.from_id IN ({marks}) OR l.to_id IN ({marks})) '
            "ORDER BY CASE l.link_type WHEN 'supersedes' THEN 0 WHEN 'continues' THEN 1 ELSE 2 END,"
            'l.strength DESC,l.created_at DESC,l.id LIMIT ?',
            (user_id,persona_id,user_id,persona_id,*ids,*ids,limit)).fetchall()
        return [dict(row) for row in rows]
