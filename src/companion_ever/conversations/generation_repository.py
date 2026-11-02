"""Durable inputs, idempotent receipt and fenced reply persistence."""
import time
import uuid
import json
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.conversations.repository import ConversationRepo
from companion_ever.accounts.usage import AccountUsage


class GenerationRepo:
    def __init__(self, conn):
        self.conn = conn
        self.messages = ConversationRepo(conn)
        self.usage = AccountUsage(conn)

    def get(self, generation_id):
        row = self.conn.execute(
            'SELECT g.*,c.user_id,coalesce(g.actor_persona_id,c.persona_id) AS persona_id,c.kind AS conversation_kind FROM generation g '
            'JOIN conversation c ON c.id=g.conversation_id WHERE g.id=?', (generation_id,)).fetchone()
        return dict(row) if row else None

    def latest(self, user_id, persona_id):
        row = self.conn.execute(
            'SELECT g.id FROM generation g JOIN conversation c ON c.id=g.conversation_id '
            "WHERE c.user_id=? AND c.persona_id=? AND g.origin='reply' ORDER BY g.trigger_message_id DESC,g.rowid DESC LIMIT 1",
            (user_id, persona_id)).fetchone()
        return self.get(row['id']) if row else None

    def receive(self, user_id, persona_id, content, client_message_id=None, *, quoted_message_id=None):
        if not content.strip():
            raise ValueError('消息不能为空')
        with transaction(self.conn):
            cid = self.messages.ensure_conversation(user_id, persona_id)
            if quoted_message_id is not None:
                self.messages.quote(cid,quoted_message_id)
            if client_message_id:
                row = self.conn.execute(
                    'SELECT id,content,quoted_message_id FROM conversation_log WHERE conversation_id=? AND client_message_id=?',
                    (cid, client_message_id)).fetchone()
                if row:
                    if row['content'] != content.strip() or row['quoted_message_id'] != quoted_message_id:
                        raise ValueError('同一 client_message_id 不能用于不同内容')
                    gid = self.conn.execute("SELECT id FROM generation WHERE trigger_message_id=? AND origin='reply' ORDER BY rowid DESC LIMIT 1", (row['id'],)).fetchone()[0]
                    return self.get(gid), False
            self.usage.require_capacity(user_id, replacing_persona=persona_id, new_message=True)
            self.conn.execute("UPDATE generation SET status='cancelled',completed_at=CURRENT_TIMESTAMP,last_error='用户已发来新消息' "
                "WHERE origin='proactive' AND actor_persona_id=? AND status IN ('queued','running') "
                "AND conversation_id IN (SELECT id FROM conversation WHERE user_id=?)",(persona_id,user_id))
            self.conn.execute("UPDATE generation SET status='cancelled',completed_at=CURRENT_TIMESTAMP, "
                              "last_error=CASE WHEN origin='proactive' THEN '用户已发来新消息' ELSE last_error END "
                              "WHERE conversation_id=? AND status IN ('queued','running')", (cid,))
            turn = self.messages.get_max_turn_index(user_id=user_id, persona_id=persona_id) + 1
            mid = self.messages.log_turn(user_id=user_id, persona_id=persona_id, turn_index=turn,
                                         role='user', content=content)
            self.conn.execute('UPDATE conversation_log SET client_message_id=?,quoted_message_id=? WHERE id=?', (client_message_id,quoted_message_id,mid))
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
            if old is None or old['origin'] != 'reply':
                raise ValueError('只能重试用户发言触发的回复')
            latest=self.latest(old['user_id'],old['persona_id'])
            if old['status']!='failed' or latest['trigger_message_id']!=old['trigger_message_id']:
                raise ValueError('只能重试最后一条消息的失败回复')
            if latest['id']!=gid:
                if latest['status'] in ('queued','running','completed'):
                    return latest,False
                raise ValueError('请重试最新的失败任务')
            if self.outputs(gid):
                raise ValueError('已有部分回复保存，请继续发送消息')
            self.usage.require_capacity(old['user_id'])
            retry_id=uuid.uuid4().hex
            self.conn.execute("INSERT INTO generation(id,conversation_id,trigger_message_id,status) VALUES (?,?,?,'queued')",
                              (retry_id,old['conversation_id'],old['trigger_message_id']))
            return self.get(retry_id),True

    def claim(self, gid):
        with transaction(self.conn):
            generation = self.get(gid)
            if not generation or generation['status'] != 'queued' or not self.usage.can_start(generation['user_id']):
                return False
            cur = self.conn.execute("UPDATE generation SET status='running',started_at=CURRENT_TIMESTAMP,lease_expires_at=? "
                                    "WHERE id=? AND status='queued'", (time.time()+300, gid))
            return cur.rowcount == 1

    def active(self, gid):
        generation = self.get(gid)
        if generation and generation['origin'] == 'proactive' and not self.proactive_valid(generation):
            self.finish(gid, 'cancelled', '对话已变化、任务已过期或主动联系已关闭')
            return False
        cur = self.conn.execute("UPDATE generation SET lease_expires_at=? WHERE id=? AND status='running' "
                                "AND lease_expires_at>?", (time.time()+300, gid, time.time()))
        return cur.rowcount == 1

    def save_message(self, gid, index, content, thinking=None):
        if not content.strip():
            raise ValueError('模型未生成可保存的正文')
        with transaction(self.conn):
            g = self.get(gid)
            if g['status'] != 'running' or not self.active(gid):
                return None
            prior = self.conn.execute('SELECT * FROM conversation_log WHERE generation_id=? AND output_index=?',
                                      (gid,index)).fetchone()
            if prior:
                return dict(prior)
            if g['origin'] == 'proactive':
                previous = self.conn.execute('SELECT turn_index FROM conversation_log WHERE generation_id=? ORDER BY id LIMIT 1', (gid,)).fetchone()
                turn = previous[0] if previous else self.messages.get_max_turn_index(user_id=g['user_id'], persona_id=g['persona_id']) + 1
            else:
                turn = self.conn.execute('SELECT turn_index FROM conversation_log WHERE id=?', (g['trigger_message_id'],)).fetchone()[0]
            mid = self.messages.log_turn(user_id=g['user_id'],persona_id=g['persona_id'],
                                         turn_index=turn,role='assistant',content=content)
            self.conn.execute('UPDATE conversation_log SET generation_id=?,output_index=?,reply_to_message_id=? WHERE id=?',
                              (gid,index,g['trigger_message_id'] if g['origin'] == 'reply' else None,mid))
            if thinking:
                self.conn.execute('INSERT INTO thinking_log(message_id,user_id,persona_id,turn_index,content) VALUES (?,?,?,?,?)',
                                  (mid,g['user_id'],g['persona_id'],turn,thinking))
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
        return [self.get(row['id']) for row in self.conn.execute("SELECT id FROM generation WHERE status='queued' AND scheduled_at<=?", (time.time(),))]

    def proactive_snapshot(self, user, persona=None, *, conversation_id=None, excluding_generation=''):
        row = self.conn.execute('SELECT c.id,c.kind,c.name,c.proactive_enabled,c.last_read_message_id, '
            "(SELECT max(id) FROM conversation_log WHERE conversation_id=c.id AND role IN ('user','assistant')) AS last_id, "
            "(SELECT max(id) FROM conversation_log WHERE conversation_id=c.id AND role='user') AS user_id, "
            "(SELECT created_at FROM conversation_log WHERE conversation_id=c.id AND role='user' ORDER BY id DESC LIMIT 1) AS user_at "
            'FROM conversation c WHERE c.user_id=? AND '+('c.id=?' if conversation_id is not None else "c.kind='private' AND c.persona_id=?"),
            (user,conversation_id if conversation_id is not None else persona)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['busy'] = bool(self.conn.execute("SELECT 1 FROM generation WHERE conversation_id=? AND id!=? AND status IN ('queued','running') LIMIT 1", (row['id'],excluding_generation)).fetchone())
        result['unread_proactive'] = bool(self.conn.execute("SELECT 1 FROM conversation_log m JOIN generation g ON g.id=m.generation_id "
            "WHERE m.conversation_id=? AND g.origin='proactive' AND m.id>? LIMIT 1", (row['id'],row['last_read_message_id'])).fetchone())
        return result

    def proactive_valid(self, g):
        if g['expires_at'] is not None and g['expires_at'] <= time.time():
            return False
        opportunity=json.loads(g['opportunity_json']) if g.get('opportunity_json') else None
        source_id=opportunity['source']['id'] if opportunity else g['conversation_id']
        latest = self.conn.execute("SELECT max(id) FROM conversation_log WHERE conversation_id=? AND role IN ('user','assistant') "
                                   'AND (generation_id IS NULL OR generation_id!=?)', (source_id,g['id'])).fetchone()[0]
        enabled = self.conn.execute('SELECT proactive_enabled FROM user_settings WHERE user_id=? AND persona_id=?', (g['user_id'],g['persona_id'])).fetchone()
        if latest!=g['context_message_id'] or not (g['test_mode'] or enabled and enabled[0]):return False
        source=self.conn.execute('SELECT kind,proactive_enabled FROM conversation WHERE id=?',(source_id,)).fetchone()
        if source and source['kind']=='group':
            member=self.conn.execute('SELECT 1 FROM conversation_member WHERE conversation_id=? AND persona_id=? AND left_after_message_id IS NULL',(source_id,g['persona_id'])).fetchone()
            return bool(member and (g['test_mode'] or source['proactive_enabled']))
        return source is not None

    def automatic_attempted(self, user, since):
        return bool(self.conn.execute("SELECT 1 FROM generation g JOIN conversation c ON c.id=g.conversation_id "
            "WHERE c.user_id=? AND origin='proactive' AND test_mode=0 AND g.created_at>=? LIMIT 1", (user,since)).fetchone())

    def enqueue_proactive(self, user, persona, snapshot, gid, *, delay=0, test=False, forced_group_id=None):
        now = time.time()
        # trigger_message_id is a historical user anchor, never a synthetic input;
        # proactive execution never calls trigger_content or replies to this message.
        self.conn.execute("INSERT INTO generation(id,conversation_id,trigger_message_id,status,origin,scheduled_at,expires_at,context_message_id,test_mode,actor_persona_id,opportunity_json) "
            "VALUES (?,?,?,'queued','proactive',?,?,?,?,?,?)", (gid,snapshot['id'],snapshot['user_id'],now+delay,now+delay+900,snapshot['last_id'],int(test),persona,
                json.dumps({'source':snapshot,'forced_group_id':forced_group_id,'destinations':[]})))
        return self.get(gid)

    def enabled_targets(self):
        return [(r[0],r[1]) for r in self.conn.execute('SELECT s.user_id,s.persona_id FROM user_settings s '
            'WHERE s.proactive_enabled=1')]

    def cancel_automatic(self, user, persona):
        self.conn.execute("UPDATE generation SET status='cancelled',last_error='主动联系已关闭' "
            "WHERE conversation_id IN (SELECT id FROM conversation WHERE user_id=?) AND actor_persona_id=? "
            "AND origin='proactive' AND test_mode=0 AND status IN ('queued','running')", (user,persona))

    def save_proactive_messages(self,gid,destination,parts,thinking=None):
        """Caller fences actor/target and owns the same transaction as notification enqueue."""
        g=self.get(gid);cid=destination['id']
        self.conn.execute('UPDATE generation SET conversation_id=? WHERE id=?',(cid,gid))
        turn=self.conn.execute('SELECT coalesce(max(turn_index),0)+1 FROM conversation_log WHERE conversation_id=?',(cid,)).fetchone()[0]
        saved=[]
        for index,part in enumerate(parts):
            mid=self.conn.execute("INSERT INTO conversation_log(user_id,persona_id,turn_index,role,content,conversation_id,sender_kind,sender_id,generation_id,output_index) "
                "VALUES (?,?,?,'assistant',?,?,'persona',?,?,?)",(g['user_id'],g['persona_id'] if destination['kind']=='private' else None,turn,part,cid,g['persona_id'],gid,index)).lastrowid
            if thinking and index==0 and destination['kind']=='private':
                self.conn.execute('INSERT INTO thinking_log(message_id,user_id,persona_id,turn_index,content) VALUES (?,?,?,?,?)',(mid,g['user_id'],g['persona_id'],turn,thinking))
            row=dict(self.conn.execute('SELECT * FROM conversation_log WHERE id=?',(mid,)).fetchone())
            row['thinking']=thinking if index==0 and destination['kind']=='private' else None
            saved.append(row)
        return saved

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
                          "AND conversation_id IN (SELECT id FROM conversation WHERE user_id=?) AND "
                          "(conversation_id IN (SELECT id FROM conversation WHERE persona_id=?) OR "
                          "EXISTS(SELECT 1 FROM conversation_member m WHERE m.conversation_id=generation.conversation_id "
                          "AND m.persona_id=? AND m.left_after_message_id IS NULL))",
                          (user_id, persona_id,persona_id))
