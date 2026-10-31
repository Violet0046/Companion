"""Persona definitions are configuration, independent of memory notes."""
import uuid
from companion_ever.infrastructure.database.connection import transaction

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
                self.conn.execute('UPDATE persona_definition SET content=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                                  (content.strip(),row['id']))
                return row['id']
            pid = 'persona_' + uuid.uuid4().hex
            self.conn.execute('INSERT INTO persona_definition(id,user_id,persona_id,content) VALUES (?,?,?,?)',
                              (pid,user_id,persona_id,content.strip()))
            return pid

    def invalidate_edits(self, user_id, persona_id):
        """停用跨越一次配置版本，拒绝恢复后迟到的模型编辑。"""
        self.conn.execute('UPDATE persona_definition SET revision=revision+1 WHERE user_id=? AND persona_id=? '
                          'AND is_archived=0', (user_id, persona_id))

    def list_by_user(self, user_id):
        """该用户留存的全部角色设定（含已停用的）。

        不按 is_archived 过滤——那个列目前从未被置 1，停用只把角色移出
        有效列表，设定行仍在。列出全部留存设定才能支撑"已删除的角色"清单。
        """
        rows = self.conn.execute(
            'SELECT * FROM persona_definition WHERE user_id=? ORDER BY updated_at DESC,id',
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
