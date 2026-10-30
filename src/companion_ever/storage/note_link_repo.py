"""Read a bounded neighbourhood; both endpoints must belong to the same scope."""
import math
import uuid
from companion_ever.domain.enums import LinkType
from companion_ever.domain.memory import valid_relation
from .note_repo import NoteRepo
from .db import transaction

class NoteLinkRepo:
    def __init__(self, conn):
        self.conn = conn

    def create(self, from_id, to_id, relation, user_id, persona_id, *, reason, job_id, strength=0.5):
        relation = LinkType(relation)
        if not isinstance(reason,str) or not reason.strip() or not math.isfinite(strength) or not 0 <= strength <= 1:
            raise ValueError('关系必须有依据，权重必须在 0～1')
        with transaction(self.conn):
            notes = NoteRepo(self.conn)
            source, target = notes.get(from_id), notes.get(to_id)
            if from_id == to_id or any(n is None or (n.user_id,n.persona_id)!=(user_id,persona_id) or n.is_archived
                                      for n in (source,target)):
                raise ValueError('关系端点不存在、已归档或超出当前角色范围')
            if not valid_relation(source,target,relation):
                raise ValueError('关系方向、记忆性质或现实情境不匹配')
            if relation in (LinkType.CONTINUES,LinkType.SUPERSEDES):
                cycle = self.conn.execute('WITH RECURSIVE path(id) AS (SELECT to_id FROM note_link WHERE from_id=? AND link_type=? '
                    'UNION SELECT l.to_id FROM note_link l JOIN path p ON l.from_id=p.id WHERE l.link_type=?) '
                    'SELECT 1 FROM path WHERE id=? LIMIT 1',(to_id,relation.value,relation.value,from_id)).fetchone()
                if cycle:
                    raise ValueError('时序或替代关系不能形成环路')
            prior = self.conn.execute('SELECT id FROM note_link WHERE from_id=? AND to_id=? AND link_type=?',
                                      (from_id,to_id,relation.value)).fetchone()
            if prior:
                return prior[0]
            lid = uuid.uuid4().hex
            self.conn.execute('INSERT INTO note_link(id,from_id,to_id,link_type,strength,reason,job_id) VALUES (?,?,?,?,?,?,?)',
                              (lid,from_id,to_id,relation.value,strength,reason.strip(),job_id))
            return lid

    def neighbours(self, ids, user_id, persona_id, limit=100):
        if not ids:
            return []
        marks = ','.join('?' for _ in ids)
        relation_marks = ','.join('?' for _ in LinkType)
        rows = self.conn.execute(
            'SELECT l.* FROM note_link l JOIN note a ON a.id=l.from_id JOIN note b ON b.id=l.to_id '
            'WHERE a.user_id=? AND a.persona_id=? AND b.user_id=? AND b.persona_id=? '
            f'AND l.link_type IN ({relation_marks}) '
            f'AND (l.from_id IN ({marks}) OR l.to_id IN ({marks})) '
            "ORDER BY CASE l.link_type WHEN 'supersedes' THEN 0 WHEN 'continues' THEN 1 ELSE 2 END,"
            'l.strength DESC,l.created_at DESC,l.id LIMIT ?',
            (user_id,persona_id,user_id,persona_id,*[r.value for r in LinkType],*ids,*ids,limit)).fetchall()
        return [dict(row) for row in rows]
