"""Persistence owned by notifications; network requests never run in transactions."""
import time
import uuid
import json

from companion_ever.infrastructure.database.connection import transaction


class NotificationRepo:
    def __init__(self, conn):
        self.conn = conn

    def identity(self, factory):
        with transaction(self.conn):
            row = self.conn.execute('SELECT private_key,public_key FROM push_identity WHERE id=1').fetchone()
            if row is None:
                private, public = factory()
                self.conn.execute('INSERT INTO push_identity(id,private_key,public_key) VALUES (1,?,?)', (private, public))
                return private, public
            return row['private_key'], row['public_key']

    def get(self, user, endpoint):
        row = self.conn.execute('SELECT * FROM push_subscription WHERE user_id=? AND endpoint=?', (user, endpoint)).fetchone()
        return dict(row) if row else None

    def save(self, user, subscription):
        with transaction(self.conn):
            owner = self.conn.execute('SELECT user_id FROM push_subscription WHERE endpoint=?', (subscription['endpoint'],)).fetchone()
            if owner is not None and owner['user_id'] != user:
                raise PermissionError('此设备订阅属于其他用户，请先关闭原用户的通知')
            self.conn.execute('INSERT INTO push_subscription(endpoint,user_id,p256dh,auth,revision,updated_at) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET '
                'p256dh=excluded.p256dh,auth=excluded.auth,revision=excluded.revision,updated_at=excluded.updated_at',
                (subscription['endpoint'], user, subscription['keys']['p256dh'], subscription['keys']['auth'], uuid.uuid4().hex, time.time()))

    def delete(self, user, endpoint, revision=None):
        sql = 'DELETE FROM push_subscription WHERE user_id=? AND endpoint=?'
        args = [user, endpoint]
        if revision is not None:
            sql += ' AND revision=?'
            args.append(revision)
        self.conn.execute(sql, args)

    def enqueue(self, user, persona, gid, message_id, payload,*,conversation_id=None,update_queued=False):
        now=time.time()
        if conversation_id is None:
            conversation_id=self.conn.execute('SELECT conversation_id FROM generation WHERE id=?',(gid,)).fetchone()[0]
        for row in self.conn.execute('SELECT endpoint FROM push_subscription WHERE user_id=?', (user,)).fetchall():
            conflict=' ON CONFLICT(generation_id,endpoint) DO UPDATE SET message_id=excluded.message_id,persona_id=excluded.persona_id,payload_json=excluded.payload_json WHERE push_delivery.status=\'queued\'' if update_queued else ' ON CONFLICT(generation_id,endpoint) DO NOTHING'
            self.conn.execute('INSERT INTO push_delivery(generation_id,message_id,user_id,persona_id,endpoint,payload_json,retry_at,expires_at,conversation_id) '
                'VALUES (?,?,?,?,?,?,?,?,?)'+conflict, (gid,message_id,user,persona,row['endpoint'],json.dumps(payload,ensure_ascii=False),now+5,now+900,conversation_id))

    def claim_delivery(self):
        with transaction(self.conn):
            now=time.time()
            self.conn.execute("UPDATE push_delivery SET status='skipped',last_error='会话已变化' WHERE status IN ('queued','running') AND generation_id IN (SELECT id FROM generation WHERE status='cancelled')")
            self.conn.execute("UPDATE push_delivery SET status='skipped',last_error='通知已过期' WHERE status IN ('queued','running') AND expires_at<=?", (now,))
            self.conn.execute("UPDATE push_delivery SET status='failed',last_error=coalesce(last_error,'投递尝试次数已用尽') "
                "WHERE attempt>=3 AND (status='queued' OR (status='running' AND lease_until<=?))", (now,))
            self.conn.execute("UPDATE push_delivery SET status='queued',lease_token=NULL WHERE status='running' AND lease_until<=?", (now,))
            row=self.conn.execute("SELECT p.*,c.kind AS conversation_kind FROM push_delivery p JOIN generation g ON g.id=p.generation_id JOIN conversation c ON c.id=p.conversation_id "
                "WHERE p.status='queued' AND p.retry_at<=? AND g.status IN ('completed','failed') ORDER BY p.id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            token=uuid.uuid4().hex
            self.conn.execute("UPDATE push_delivery SET status='running',lease_token=?,lease_until=?,attempt=attempt+1 WHERE id=?", (token,now+60,row['id']))
            return {**dict(row),'lease_token':token,'attempt':row['attempt']+1}

    def has_delivery(self):
        now=time.time()
        return bool(self.conn.execute("SELECT 1 FROM push_delivery WHERE (status='queued' AND retry_at<=?) "
                                     "OR (status='running' AND lease_until<=?) LIMIT 1", (now,now)).fetchone())

    def finish_delivery(self, job, status, error=None):
        self.conn.execute("UPDATE push_delivery SET status=?,last_error=?,retry_at=?,lease_token=NULL,lease_until=NULL "
            "WHERE id=? AND status='running' AND lease_token=?", (status,error,time.time()+30*job['attempt'],job['id'],job['lease_token']))

    def defer_delivery(self,job,until):
        self.conn.execute("UPDATE push_delivery SET status='queued',attempt=max(0,attempt-1),retry_at=?,lease_token=NULL,lease_until=NULL WHERE id=? AND status='running' AND lease_token=?",
            (until,job['id'],job['lease_token']))

    def wake_group(self,cid):
        self.conn.execute("UPDATE push_delivery SET retry_at=min(retry_at,?) WHERE conversation_id=? AND status='queued'",(time.time()+2,cid))

    def deliveries(self, gid):
        return [dict(r) for r in self.conn.execute('SELECT status,attempt,last_error FROM push_delivery WHERE generation_id=? ORDER BY id', (gid,))]
