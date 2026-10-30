"""Independent summary jobs, operation cursors and effective fact lookup."""
import hashlib
import json
import time
import uuid
from .db import transaction
from .note_repo import NoteRepo
from companion_ever.domain.enums import MemoryKind
from .task_repo import TaskRepo
from companion_ever.services.task_errors import LeaseLostError
from companion_ever.services.summary_changes import coalesce_changes


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


class SummaryRepo:
    def __init__(self,conn):
        self.conn=conn

    def checkpoint(self,user,persona):
        row=self.conn.execute('SELECT * FROM summary_checkpoint WHERE user_id=? AND persona_id=?',(user,persona)).fetchone()
        return dict(row) if row else None

    def latest_change(self,user,persona):
        return self.conn.execute('SELECT coalesce(max(id),0) FROM fact_change WHERE user_id=? AND persona_id=?',(user,persona)).fetchone()[0]

    def blocks(self,user,persona):
        return {r['block_type']:json.loads(r['fields_json']) for r in self.conn.execute(
            'SELECT block_type,fields_json FROM memory_block WHERE user_id=? AND persona_id=?',(user,persona))}

    def get(self,jid):
        row=self.conn.execute('SELECT * FROM summary_job WHERE id=?',(jid,)).fetchone()
        return dict(row) if row else None

    def pending(self,user,persona):
        row=self.conn.execute("SELECT * FROM summary_job WHERE user_id=? AND persona_id=? AND status!='succeeded'",(user,persona)).fetchone()
        return dict(row) if row else None

    def change_window(self,user,persona,start,*,until_id=None):
        rows=self.conn.execute('SELECT * FROM fact_change WHERE user_id=? AND persona_id=? AND id>? '
            'AND (? IS NULL OR id<=?) ORDER BY id LIMIT 200',(user,persona,start,until_id,until_id)).fetchall()
        events=[];changes=[];through=start
        for row in rows:
            event={**dict(row),'before':json.loads(row['before_json']) if row['before_json'] else None,
                   'after':json.loads(row['after_json']) if row['after_json'] else None}
            candidate=coalesce_changes(events+[event])
            if events and (len(candidate)>24 or len(json.dumps(candidate,ensure_ascii=False))>12000):
                break
            events.append(event);changes=candidate;through=row['id']
        has_more=bool(self.conn.execute('SELECT 1 FROM fact_change WHERE user_id=? AND persona_id=? AND id>? '
            'AND (? IS NULL OR id<=?) LIMIT 1',(user,persona,through,until_id,until_id)).fetchone())
        return {'through_change_id':through,'raw_count':len(events),'changes':changes,'has_more':has_more}

    def enqueue(self,user,persona,manual=False,*,until_id=None):
        with transaction(self.conn):
            pending=self.pending(user,persona)
            if pending:
                if manual and pending['status']=='failed':
                    self.conn.execute("UPDATE summary_job SET status='queued',attempt_count=0,retry_at=NULL,last_error=NULL WHERE id=?",(pending['id'],))
                return self.get(pending['id'])
            checkpoint=self.checkpoint(user,persona)
            start=checkpoint['last_change_id'] if checkpoint else 0
            latest=self.latest_change(user,persona)
            if checkpoint and latest==start and digest(self.blocks(user,persona))==checkpoint['blocks_hash']:
                return None
            # Budget the compacted changes, while retaining an ordered fixed
            # raw-event range for recovery and atomic cursor advancement.
            window=self.change_window(user,persona,start,until_id=until_id)
            through=window['through_change_id']
            if checkpoint and through==start and (until_id is not None or digest(self.blocks(user,persona))==checkpoint['blocks_hash']):
                return None
            jid=uuid.uuid4().hex
            self.conn.execute("INSERT INTO summary_job(id,user_id,persona_id,after_change_id,through_change_id,status) VALUES (?,?,?,?,?,'queued')",
                              (jid,user,persona,start,through))
            return self.get(jid)

    def claim(self,jid,*,owner_id=None,lease_seconds=300):
        with transaction(self.conn):
            job=self.get(jid);now=time.time()
            eligible=job['status']=='queued' or (job['status']=='running' and job['lease_expires_at']<=now) or (
                job['status']=='failed' and job['retry_at'] is not None and job['retry_at']<=now)
            interrupted=job['status']=='running' and job['lease_expires_at']<=now
            if not eligible or (job['attempt_count']>=3 and not interrupted):
                return None
            if interrupted:
                TaskRepo(self.conn).finish(job['lease_token'],'interrupted','lease_expired','执行中断后接管，保留原范围')
                self.conn.execute('UPDATE summary_job SET attempt_count=max(attempt_count-1,0) WHERE id=?',(jid,))
            token=uuid.uuid4().hex
            self.conn.execute("UPDATE summary_job SET status='running',attempt_count=attempt_count+1,lease_token=?,lease_expires_at=?,retry_at=NULL,last_error=NULL WHERE id=?",
                              (token,now+lease_seconds,jid))
            claimed=self.get(jid)
            TaskRepo(self.conn).start('summary',claimed,owner_id)
            return claimed

    def require_owner(self,job):
        current=self.get(job['id'])
        if current['status']!='running' or current['lease_token']!=job['lease_token'] or current['lease_expires_at']<=time.time():
            raise LeaseLostError('摘要任务租约已失效')

    def renew(self,job,*,lease_seconds=300):
        with transaction(self.conn):
            changed=self.conn.execute("UPDATE summary_job SET lease_expires_at=? WHERE id=? AND status='running' AND lease_token=? AND lease_expires_at>?",
                                 (time.time()+lease_seconds,job['id'],job['lease_token'],time.time())).rowcount==1
            if changed:
                TaskRepo(self.conn).beat(job['lease_token'])
            return changed

    def fail(self,job,error,*,retryable=True,failure_kind='model_error'):
        retry=time.time()+30*2**(job['attempt_count']-1) if retryable and job['attempt_count']<3 else None
        with transaction(self.conn):
            changed=self.conn.execute("UPDATE summary_job SET status='failed',last_error=?,retry_at=?,lease_expires_at=NULL WHERE id=? AND status='running' AND lease_token=?",
                          (error[:1000],retry,job['id'],job['lease_token']))
            if changed.rowcount:
                TaskRepo(self.conn).finish(job['lease_token'],'failed',failure_kind,error)

    def interrupt(self,job,error,*,failure_kind='execution_interrupted'):
        with transaction(self.conn):
            changed=self.conn.execute("UPDATE summary_job SET status='queued',attempt_count=max(attempt_count-1,0),"
                "lease_token=NULL,lease_expires_at=NULL,retry_at=NULL,last_error=? WHERE id=? AND status='running' AND lease_token=?",
                (error[:1000],job['id'],job['lease_token']))
            if changed.rowcount:
                TaskRepo(self.conn).finish(job['lease_token'],'interrupted',failure_kind,error)

    def changes(self,job):
        rows=self.conn.execute('SELECT * FROM fact_change WHERE user_id=? AND persona_id=? AND id>? AND id<=? ORDER BY id',
            (job['user_id'],job['persona_id'],job['after_change_id'],job['through_change_id'])).fetchall()
        return [{**dict(r),'before':json.loads(r['before_json']) if r['before_json'] else None,
                 'after':json.loads(r['after_json']) if r['after_json'] else None} for r in rows]

    def affected(self,ids,user,persona):
        result=set(ids)
        if not ids:
            return result
        marks=','.join('?' for _ in ids)
        rows=self.conn.execute(f"WITH RECURSIVE affected(id) AS (SELECT to_id FROM note_link WHERE from_id IN ({marks}) AND link_type='supersedes' "
            "UNION SELECT l.to_id FROM note_link l JOIN affected a ON l.from_id=a.id WHERE l.link_type='supersedes') "
            'SELECT n.id FROM affected a JOIN note n ON n.id=a.id WHERE n.user_id=? AND n.persona_id=?',(*ids,user,persona)).fetchall()
        result.update(r[0] for r in rows)
        return result

    def fact(self,nid,user,persona):
        note=NoteRepo(self.conn).get(nid)
        if note is None or note.kind!=MemoryKind.FACT or note.is_archived or (note.user_id,note.persona_id)!=(user,persona):
            return None
        # Traverse archived intermediate versions too; an active later version still supersedes the root.
        successor=self.conn.execute("WITH RECURSIVE successors(id) AS (SELECT ? UNION SELECT newer.id FROM successors s "
            "JOIN note old ON old.id=s.id JOIN note_link l ON l.to_id=old.id AND l.link_type='supersedes' "
            "JOIN note newer ON newer.id=l.from_id WHERE newer.kind='fact' AND old.kind='fact' "
            "AND newer.user_id=old.user_id AND newer.persona_id=old.persona_id "
            "AND (newer.reality=old.reality OR old.reality='uncertain')) "
            'SELECT 1 FROM successors s JOIN note n ON n.id=s.id WHERE n.id!=? AND n.is_archived=0 LIMIT 1',(nid,nid)).fetchone()
        if successor:
            return None
        evidence=[dict(r) for r in self.conn.execute('SELECT e.message_id,e.quote FROM note_evidence e JOIN conversation_log m ON m.id=e.message_id '
            'WHERE e.note_id=? AND m.user_id=? AND m.persona_id=? ORDER BY e.message_id DESC LIMIT 2',(nid,user,persona))]
        return {'id':note.id,'kind':'fact','content':note.content,'reality':note.reality,'event_time':note.event_time,
                'time_basis':note.time_basis,'importance':note.importance,'revision':note.revision,'evidence':evidence}
