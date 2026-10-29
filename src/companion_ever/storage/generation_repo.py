"""Durable inputs, idempotent receipt and fenced reply persistence."""
import time
import uuid
from .db import transaction
from .conversation_repo import ConversationRepo


class GenerationRepo:
    def __init__(self, conn):
        self.conn = conn
        self.messages = ConversationRepo(conn)

    def get(self, generation_id):
        row = self.conn.execute(
            'SELECT g.*,c.user_id,c.persona_id FROM generation g '
            'JOIN conversation c ON c.id=g.conversation_id WHERE g.id=?', (generation_id,)).fetchone()
        return dict(row) if row else None

    def receive(self, user_id, persona_id, content, client_message_id=None):
        if not content.strip():
            raise ValueError('消息不能为空')
        with transaction(self.conn):
            cid = self.messages.ensure_conversation(user_id, persona_id)
            if client_message_id:
                row = self.conn.execute(
                    'SELECT id,content FROM conversation_log WHERE conversation_id=? AND client_message_id=?',
                    (cid, client_message_id)).fetchone()
                if row:
                    if row['content'] != content.strip():
                        raise ValueError('同一 client_message_id 不能用于不同内容')
                    gid = self.conn.execute('SELECT id FROM generation WHERE trigger_message_id=?', (row['id'],)).fetchone()[0]
                    return self.get(gid), False
            self.conn.execute("UPDATE generation SET status='cancelled',completed_at=CURRENT_TIMESTAMP "
                              "WHERE conversation_id=? AND status IN ('queued','running')", (cid,))
            turn = self.messages.get_max_turn_index(user_id=user_id, persona_id=persona_id) + 1
            mid = self.messages.log_turn(user_id=user_id, persona_id=persona_id, turn_index=turn,
                                         role='user', content=content)
            self.conn.execute('UPDATE conversation_log SET client_message_id=? WHERE id=?', (client_message_id, mid))
            gid = uuid.uuid4().hex
            self.conn.execute("INSERT INTO generation(id,conversation_id,trigger_message_id,status) VALUES (?,?,?,'queued')",
                              (gid, cid, mid))
            return self.get(gid), True

    def claim(self, gid):
        with transaction(self.conn):
            cur = self.conn.execute("UPDATE generation SET status='running',started_at=CURRENT_TIMESTAMP,lease_expires_at=? "
                                    "WHERE id=? AND status='queued'", (time.time()+300, gid))
            return cur.rowcount == 1

    def active(self, gid):
        cur = self.conn.execute("UPDATE generation SET lease_expires_at=? WHERE id=? AND status='running' "
                                "AND lease_expires_at>?", (time.time()+300, gid, time.time()))
        return cur.rowcount == 1

    def save_message(self, gid, index, content, thinking=None):
        if not content.strip():
            raise ValueError('模型未生成可保存的正文')
        with transaction(self.conn):
            g = self.get(gid)
            if g['status'] != 'running' or g['lease_expires_at'] <= time.time():
                return None
            prior = self.conn.execute('SELECT * FROM conversation_log WHERE generation_id=? AND output_index=?',
                                      (gid,index)).fetchone()
            if prior:
                return dict(prior)
            trigger = self.conn.execute('SELECT turn_index FROM conversation_log WHERE id=?', (g['trigger_message_id'],)).fetchone()
            mid = self.messages.log_turn(user_id=g['user_id'],persona_id=g['persona_id'],
                                         turn_index=trigger[0],role='assistant',content=content)
            self.conn.execute('UPDATE conversation_log SET generation_id=?,output_index=?,reply_to_message_id=? WHERE id=?',
                              (gid,index,g['trigger_message_id'],mid))
            if thinking:
                self.conn.execute('INSERT INTO thinking_log(message_id,user_id,persona_id,turn_index,content) VALUES (?,?,?,?,?)',
                                  (mid,g['user_id'],g['persona_id'],trigger[0],thinking))
            row = dict(self.conn.execute('SELECT * FROM conversation_log WHERE id=?',(mid,)).fetchone())
            row['thinking'] = thinking
            return row

    def finish(self, gid, status='completed', error=None):
        self.conn.execute("UPDATE generation SET status=?,last_error=?,completed_at=CURRENT_TIMESTAMP "
                          "WHERE id=? AND status IN ('queued','running')", (status,error,gid))

    def outputs(self, gid):
        return [dict(r) for r in self.conn.execute(
            'SELECT m.*,t.content AS thinking FROM conversation_log m LEFT JOIN thinking_log t ON t.message_id=m.id '
            'WHERE m.generation_id=? ORDER BY m.output_index', (gid,))]

    def recover(self):
        # Never silently regenerate possibly delivered text. Retain the user's message and expose failure.
        self.conn.execute("UPDATE generation SET status='failed',last_error='生成中断，可重新发送',completed_at=CURRENT_TIMESTAMP "
                          "WHERE (status='running' AND lease_expires_at<=?) OR "
                          "(status='queued' AND created_at<datetime('now','-5 minutes'))", (time.time(),))
