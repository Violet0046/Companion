"""Persona definitions are configuration, independent of memory notes."""
import uuid
from .db import transaction

class PersonaRepo:
    def __init__(self, conn):
        self.conn = conn

    def get(self, user_id, persona_id):
        row = self.conn.execute('SELECT * FROM persona_definition WHERE user_id=? AND persona_id=? '
                                'AND is_archived=0 ORDER BY updated_at DESC,id LIMIT 1', (user_id,persona_id)).fetchone()
        return dict(row) if row else None

    def put(self, user_id, persona_id, content):
        if not isinstance(content,str) or not content.strip():
            raise ValueError('角色设定不能为空')
        with transaction(self.conn):
            row = self.get(user_id,persona_id)
            if row:
                self.conn.execute('UPDATE persona_definition SET content=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                                  (content.strip(),row['id']))
                return row['id']
            pid = 'persona_' + uuid.uuid4().hex
            self.conn.execute('INSERT INTO persona_definition(id,user_id,persona_id,content) VALUES (?,?,?,?)',
                              (pid,user_id,persona_id,content.strip()))
            return pid
