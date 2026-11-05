"""Per-conversation serial jobs with immutable ranges and renewable leases."""
import time
import uuid
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.conversations.repository import ConversationRepo
from companion_ever.runtime.repository import TaskRepo
from companion_ever.runtime.errors import LeaseLostError


class MemoryJobRepo:
    def __init__(self, conn):
        self.conn = conn

    def get(self, jid):
        row = self.conn.execute('SELECT j.*,c.user_id,c.kind AS conversation_kind,c.persona_id AS private_persona_id FROM memory_job j '
                                'JOIN conversation c ON c.id=j.conversation_id WHERE j.id=?', (jid,)).fetchone()
        if row is None:return None
        result=dict(row);result['persona_id']=result['persona_id'] or result['private_persona_id']
        return result

    def source_visible(self,job,row):
        if row is None or row['user_id']!=job['user_id']:return False
        if row['role'] not in ('user','assistant'):return False
        if job['conversation_kind']!='group':return row['persona_id']==job['persona_id']
        scope=ConversationRepo(self.conn).memory_scope(job['user_id'],job['persona_id'],job['conversation_id'])
        return row['conversation_id']==job['conversation_id'] and row['id']>scope['joined_after_message_id']

    def latest_message_id(self,user_id,persona_id,*,conversation_id=None):
        if conversation_id is not None:
            ConversationRepo(self.conn).memory_scope(user_id,persona_id,conversation_id)
            return self.conn.execute("SELECT coalesce(max(id),0) FROM conversation_log WHERE conversation_id=? AND role IN ('user','assistant')",(conversation_id,)).fetchone()[0]
        return self.conn.execute("SELECT coalesce(max(id),0) FROM conversation_log WHERE user_id=? AND persona_id=? "
                                 "AND role IN ('user','assistant')",(user_id,persona_id)).fetchone()[0]

    def pending_for_conversation(self, conversation_id,persona_id=None):
        return bool(self.conn.execute("SELECT 1 FROM memory_job WHERE conversation_id=? AND (? IS NULL OR persona_id=?) AND status!='succeeded' "
            "AND (member_revision IS NULL OR EXISTS(SELECT 1 FROM conversation_member m WHERE m.conversation_id=memory_job.conversation_id "
            "AND m.persona_id=memory_job.persona_id AND m.revision=memory_job.member_revision AND m.left_after_message_id IS NULL))",
                                     (conversation_id,persona_id,persona_id)).fetchone())

    def progress(self, conversation):
        conversation = dict(conversation)
        lower = max(conversation['memory_start_message_id'], conversation.get('joined_after_message_id') or 0)
        return self.conn.execute('SELECT max(?,coalesce(max(through_message_id),0)) FROM memory_job '
            "WHERE conversation_id=? AND persona_id=? AND status='succeeded'",
            (lower, conversation['id'],conversation['persona_id'])).fetchone()[0]

    def busy(self, user, persona):
        return bool(self.conn.execute('SELECT 1 FROM memory_job j JOIN conversation c ON c.id=j.conversation_id '
            "WHERE c.user_id=? AND j.persona_id=? AND j.status IN ('queued','running') LIMIT 1", (user, persona)).fetchone())

    def pending_scopes(self):
        return [dict(row) for row in self.conn.execute('SELECT DISTINCT c.user_id,j.persona_id,CASE WHEN c.persona_id IS NULL THEN c.id END AS conversation_id FROM memory_job j '
            "JOIN conversation c ON c.id=j.conversation_id WHERE j.status='queued' OR "
            "(j.status='running' AND j.lease_expires_at<=?) OR (j.status='failed' AND j.retry_at<=?)",
            (time.time(), time.time()))]

    def recent(self, user, persona):
        return [dict(row) for row in self.conn.execute('SELECT j.* FROM memory_job j '
            'JOIN conversation c ON c.id=j.conversation_id WHERE c.user_id=? AND j.persona_id=? '
            'ORDER BY j.created_at DESC,j.rowid DESC LIMIT 50', (user, persona))]

    def overview(self, user, persona):
        completed = self.conn.execute('SELECT max(j.completed_at) FROM memory_job j '
            "JOIN conversation c ON c.id=j.conversation_id WHERE c.user_id=? AND j.persona_id=? AND j.status='succeeded'",
            (user, persona)).fetchone()[0]
        latest = self.conn.execute('SELECT j.id,j.status,j.attempt_count,j.last_error,j.retry_at FROM memory_job j '
            'JOIN conversation c ON c.id=j.conversation_id WHERE c.user_id=? AND j.persona_id=? '
            'ORDER BY j.rowid DESC LIMIT 1', (user, persona)).fetchone()
        return {'last_record_at': completed, 'memory_meta': dict(latest) if latest else None}

    def enqueue(self, user_id, persona_id, batch_size=200, max_chars=8000, *, until_id=None,conversation_id=None):
        if not 1 <= batch_size <= 200 or max_chars < 1:
            raise ValueError('记忆批次必须为 1～200 条且文本预算为正数')
        with transaction(self.conn):
            cid = conversation_id if conversation_id is not None else ConversationRepo(self.conn).ensure_conversation(user_id,persona_id)
            scope=ConversationRepo(self.conn).memory_scope(user_id,persona_id,cid)
            row = self.conn.execute("SELECT id FROM memory_job WHERE conversation_id=? AND persona_id=? AND member_revision IS ? AND status!='succeeded'",(cid,persona_id,scope['member_revision'])).fetchone()
            if row:
                return self.get(row[0])
            start=self.progress({'id':cid,'persona_id':persona_id,'memory_start_message_id':max(scope['memory_start_message_id'],scope['joined_after_message_id'] or 0)})
            rows = self.conn.execute("SELECT id,length(content) AS chars FROM conversation_log WHERE conversation_id=? AND id>? "
                                     "AND (? IS NULL OR id<=?) AND role IN ('user','assistant') ORDER BY id LIMIT ?",
                                     (cid,start,until_id,until_id,batch_size)).fetchall()
            if not rows:
                return None
            through, chars = rows[0]['id'], 0
            for row in rows:
                # A single large message stays whole; never skip or truncate source evidence.
                if chars and chars + row['chars'] > max_chars:
                    break
                through = row['id']
                chars += row['chars']
            jid = uuid.uuid4().hex
            self.conn.execute("INSERT INTO memory_job(id,conversation_id,after_message_id,through_message_id,status,persona_id,member_revision) "
                              "VALUES (?,?,?,?,'queued',?,?)",(jid,cid,start,through,persona_id,scope['member_revision']))
            return self.get(jid)

    def claim(self, jid, *, owner_id=None, lease_seconds=300):
        with transaction(self.conn):
            now = time.time()
            job = self.get(jid)
            if not self.valid_scope(job):
                self.conn.execute("UPDATE memory_job SET status='failed',last_error='群成员资格已变化',retry_at=NULL,lease_expires_at=NULL WHERE id=?",(jid,))
                TaskRepo(self.conn).finish(job['lease_token'],'failed','permission','群成员资格已变化')
                return None
            eligible = job['status']=='queued' or (job['status']=='running' and job['lease_expires_at']<=now)
            eligible |= job['status']=='failed' and job['retry_at'] is not None and job['retry_at']<=now
            interrupted=job['status']=='running' and job['lease_expires_at']<=now
            if not eligible or (job['attempt_count']>=3 and not interrupted):
                return None
            if interrupted:
                TaskRepo(self.conn).finish(job['lease_token'],'interrupted','lease_expired','执行中断后接管，保留原范围')
                self.conn.execute('UPDATE memory_job SET attempt_count=max(attempt_count-1,0) WHERE id=?',(jid,))
            token = uuid.uuid4().hex
            self.conn.execute("UPDATE memory_job SET status='running',attempt_count=attempt_count+1,lease_token=?,"
                              "lease_expires_at=?,started_at=CURRENT_TIMESTAMP,retry_at=NULL,last_error=NULL WHERE id=?",
                              (token,now+lease_seconds,jid))
            claimed=self.get(jid)
            TaskRepo(self.conn).start('memory',claimed,owner_id)
            return claimed

    def valid_scope(self,job):
        if not job:return False
        if job['conversation_kind']!='group':return True
        try:scope=ConversationRepo(self.conn).memory_scope(job['user_id'],job['persona_id'],job['conversation_id'])
        except PermissionError:return False
        return scope['member_revision']==job['member_revision'] and job['after_message_id']>=scope['joined_after_message_id']

    def renew(self, jid, token, *, lease_seconds=300):
        with transaction(self.conn):
            if not self.valid_scope(self.get(jid)):return False
            changed=self.conn.execute("UPDATE memory_job SET lease_expires_at=? WHERE id=? AND lease_token=? "
                                 "AND status='running' AND lease_expires_at>?",
                                 (time.time()+lease_seconds,jid,token,time.time())).rowcount == 1
            if changed:
                TaskRepo(self.conn).beat(token)
            return changed

    def require_owner(self, jid, token):
        row = self.get(jid)
        if not self.valid_scope(row) or row['status']!='running' or row['lease_token']!=token or row['lease_expires_at']<=time.time():
            raise LeaseLostError('记忆任务的执行租约已失效')

    def fail(self, jid, token, error, *, retryable=True, failure_kind='model_error'):
        with transaction(self.conn):
            job=self.get(jid)
            retry=time.time()+30*(2**(job['attempt_count']-1)) if retryable and job['attempt_count']<3 else None
            changed=self.conn.execute("UPDATE memory_job SET status='failed',last_error=?,retry_at=?,lease_expires_at=NULL "
                              "WHERE id=? AND lease_token=? AND status='running'",(error[:1000],retry,jid,token))
            if changed.rowcount:
                TaskRepo(self.conn).finish(token,'failed',failure_kind,error)

    def interrupt(self,jid,token,error,*,failure_kind='execution_interrupted'):
        with transaction(self.conn):
            changed=self.conn.execute("UPDATE memory_job SET status='queued',attempt_count=max(attempt_count-1,0),"
                "lease_token=NULL,lease_expires_at=NULL,retry_at=NULL,last_error=? WHERE id=? AND status='running' AND lease_token=?",
                (error[:1000],jid,token))
            if changed.rowcount:
                TaskRepo(self.conn).finish(token,'interrupted',failure_kind,error)

    def succeed(self, jid, token):
        with transaction(self.conn):
            self.require_owner(jid,token)
            self.conn.execute("UPDATE memory_job SET status='succeeded',completed_at=CURRENT_TIMESTAMP,last_error=NULL,"
                          "lease_expires_at=NULL WHERE id=? AND lease_token=?",(jid,token))
            TaskRepo(self.conn).finish(token,'succeeded')

    def retry(self, jid):
        self.conn.execute("UPDATE memory_job SET status='queued',attempt_count=0,retry_at=NULL,last_error=NULL "
                          "WHERE id=? AND status='failed'", (jid,))

    def revoke_persona(self, user_id, persona_id):
        with transaction(self.conn):
            rows = self.conn.execute("SELECT j.id,j.lease_token FROM memory_job j JOIN conversation c "
                "ON c.id=j.conversation_id WHERE c.user_id=? AND j.persona_id=? AND "
                "(j.status IN ('queued','running') OR (j.status='failed' AND j.retry_at IS NOT NULL))",
                (user_id, persona_id)).fetchall()
            for row in rows:
                TaskRepo(self.conn).finish(row['lease_token'], 'failed', 'permission', '角色已停用')
                self.conn.execute("UPDATE memory_job SET status='failed',lease_token=NULL,lease_expires_at=NULL,"
                    "retry_at=NULL,last_error='角色已停用' WHERE id=?", (row['id'],))

    def revoke_group(self,user,persona,cid):
        with transaction(self.conn):
            rows=self.conn.execute("SELECT j.id,j.lease_token FROM memory_job j JOIN conversation c ON c.id=j.conversation_id "
                "WHERE c.user_id=? AND j.conversation_id=? AND j.persona_id=? AND j.status!='succeeded'",(user,cid,persona)).fetchall()
            for row in rows:
                TaskRepo(self.conn).finish(row['lease_token'],'failed','permission','角色已退出群聊')
                self.conn.execute("UPDATE memory_job SET status='failed',last_error='角色已退出群聊',lease_token=NULL,lease_expires_at=NULL,retry_at=NULL WHERE id=?",(row['id'],))

    def restore_persona(self, user_id, persona_id):
        self.conn.execute("UPDATE memory_job SET status='queued',attempt_count=0,last_error=NULL "
            "WHERE status='failed' AND last_error='角色已停用' AND conversation_id IN "
            "(SELECT id FROM conversation WHERE user_id=?) AND persona_id=?", (user_id, persona_id))
