"""Durable inputs, idempotent receipt and fenced reply persistence."""
import time
import uuid
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.conversations.repository import ConversationRepo


class GenerationRepo:
    def __init__(self, conn):
        self.conn = conn
        self.messages = ConversationRepo(conn)

    def get(self, generation_id):
        row = self.conn.execute(
            'SELECT g.*,c.user_id,c.persona_id FROM generation g '
            'JOIN conversation c ON c.id=g.conversation_id WHERE g.id=?', (generation_id,)).fetchone()
        return dict(row) if row else None

    def latest(self, user_id, persona_id):
        row = self.conn.execute(
            'SELECT g.id FROM generation g JOIN conversation c ON c.id=g.conversation_id '
            'WHERE c.user_id=? AND c.persona_id=? ORDER BY g.trigger_message_id DESC,g.rowid DESC LIMIT 1',
            (user_id, persona_id)).fetchone()
        return self.get(row['id']) if row else None

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
                    gid = self.conn.execute('SELECT id FROM generation WHERE trigger_message_id=? ORDER BY rowid DESC LIMIT 1', (row['id'],)).fetchone()[0]
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

    def retry(self, gid):
        """Explicitly retry the latest failed reply, retaining the original input.

        A new execution ID fences off late callbacks from the failed worker.
        Partial replies are retained and cannot be silently regenerated.
        """
        with transaction(self.conn):
            old=self.get(gid)
            latest=self.latest(old['user_id'],old['persona_id'])
            if old['status']!='failed' or latest['trigger_message_id']!=old['trigger_message_id']:
                raise ValueError('只能重试最后一条消息的失败回复')
            if latest['id']!=gid:
                if latest['status'] in ('queued','running','completed'):
                    return latest,False
                raise ValueError('请重试最新的失败任务')
            if self.outputs(gid):
                raise ValueError('已有部分回复保存，请继续发送消息')
            retry_id=uuid.uuid4().hex
            self.conn.execute("INSERT INTO generation(id,conversation_id,trigger_message_id,status) VALUES (?,?,?,'queued')",
                              (retry_id,old['conversation_id'],old['trigger_message_id']))
            return self.get(retry_id),True

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
                          "WHERE status='running' AND lease_expires_at<=?", (time.time(),))

    def queued(self):
        return [self.get(row['id']) for row in self.conn.execute("SELECT id FROM generation WHERE status='queued'")]

    def trigger_content(self, generation):
        return self.conn.execute('SELECT content FROM conversation_log WHERE id=?',
                                 (generation['trigger_message_id'],)).fetchone()[0]

    def cancel(self, generation_id):
        self.finish(generation_id, 'cancelled')

    def interrupt_running(self, generation_ids):
        if not generation_ids:
            return
        with transaction(self.conn):
            for generation_id in generation_ids:
                self.conn.execute("UPDATE generation SET status='failed',last_error='生成中断，可重试',"
                    "completed_at=CURRENT_TIMESTAMP,lease_expires_at=NULL WHERE id=? AND status='running'",
                    (generation_id,))

    def revoke_persona(self, user_id, persona_id):
        """停止该角色的待执行/在途生成，完整气泡和用户输入继续保留。"""
        self.conn.execute("UPDATE generation SET status='cancelled',last_error='角色已停用',"
                          "completed_at=CURRENT_TIMESTAMP,lease_expires_at=NULL WHERE status IN ('queued','running') "
                          "AND conversation_id IN (SELECT id FROM conversation WHERE user_id=? AND persona_id=?)",
                          (user_id, persona_id))
