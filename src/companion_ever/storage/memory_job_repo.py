"""Per-conversation serial jobs with immutable ranges and renewable leases."""
import time
import uuid
from .db import transaction
from .conversation_repo import ConversationRepo


class MemoryJobRepo:
    def __init__(self, conn):
        self.conn = conn

    def get(self, jid):
        row = self.conn.execute('SELECT j.*,c.user_id,c.persona_id FROM memory_job j '
                                'JOIN conversation c ON c.id=j.conversation_id WHERE j.id=?', (jid,)).fetchone()
        return dict(row) if row else None

    def enqueue(self, user_id, persona_id, batch_size=200):
        with transaction(self.conn):
            cid = ConversationRepo(self.conn).ensure_conversation(user_id,persona_id)
            row = self.conn.execute("SELECT id FROM memory_job WHERE conversation_id=? AND status!='succeeded'",(cid,)).fetchone()
            if row:
                return self.get(row[0])
            start = self.conn.execute('SELECT max(memory_start_message_id,coalesce('
                '(SELECT max(through_message_id) FROM memory_job WHERE conversation_id=? AND status=\'succeeded\'),0)) '
                'FROM conversation WHERE id=?',(cid,cid)).fetchone()[0]
            rows = self.conn.execute("SELECT id FROM conversation_log WHERE conversation_id=? AND id>? "
                                     "AND role IN ('user','assistant') ORDER BY id LIMIT ?",(cid,start,batch_size)).fetchall()
            if not rows:
                return None
            jid = uuid.uuid4().hex
            self.conn.execute("INSERT INTO memory_job(id,conversation_id,after_message_id,through_message_id,status) "
                              "VALUES (?,?,?,?,'queued')",(jid,cid,start,rows[-1][0]))
            return self.get(jid)

    def claim(self, jid):
        with transaction(self.conn):
            now = time.time()
            job = self.get(jid)
            eligible = job['status']=='queued' or (job['status']=='running' and job['lease_expires_at']<=now)
            eligible |= job['status']=='failed' and job['retry_at'] is not None and job['retry_at']<=now
            if not eligible or job['attempt_count']>=3:
                if job['status']=='running' and job['lease_expires_at']<=now:
                    self.conn.execute("UPDATE memory_job SET status='failed',retry_at=NULL,last_error='执行租约到期，重试次数已用尽' WHERE id=?",(jid,))
                return None
            token = uuid.uuid4().hex
            self.conn.execute("UPDATE memory_job SET status='running',attempt_count=attempt_count+1,lease_token=?,"
                              "lease_expires_at=?,started_at=CURRENT_TIMESTAMP,retry_at=NULL WHERE id=?",
                              (token,now+300,jid))
            return self.get(jid)

    def renew(self, jid, token):
        return self.conn.execute("UPDATE memory_job SET lease_expires_at=? WHERE id=? AND lease_token=? "
                                 "AND status='running' AND lease_expires_at>?",
                                 (time.time()+300,jid,token,time.time())).rowcount == 1

    def require_owner(self, jid, token):
        row = self.get(jid)
        if row['status']!='running' or row['lease_token']!=token or row['lease_expires_at']<=time.time():
            raise RuntimeError('记忆任务的执行租约已失效')

    def fail(self, jid, token, error):
        with transaction(self.conn):
            job=self.get(jid)
            retry=time.time()+30*(2**(job['attempt_count']-1)) if job['attempt_count']<3 else None
            self.conn.execute("UPDATE memory_job SET status='failed',last_error=?,retry_at=?,lease_expires_at=NULL "
                              "WHERE id=? AND lease_token=? AND status='running'",(error[:1000],retry,jid,token))

    def succeed(self, jid, token):
        self.require_owner(jid,token)
        self.conn.execute("UPDATE memory_job SET status='succeeded',completed_at=CURRENT_TIMESTAMP,last_error=NULL,"
                          "lease_expires_at=NULL WHERE id=? AND lease_token=?",(jid,token))

    def retry(self, jid):
        self.conn.execute("UPDATE memory_job SET status='queued',attempt_count=0,retry_at=NULL,last_error=NULL "
                          "WHERE id=? AND status='failed'", (jid,))
