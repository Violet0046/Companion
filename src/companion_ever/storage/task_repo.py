"""Fenced attempt history shared by memory and summary jobs."""
import json
import os
import time
import uuid
from .db import transaction


PROCESS_ID=uuid.uuid4().hex


class TaskRepo:
    def __init__(self,conn):
        self.conn=conn

    def start(self,kind,job,owner_id=None):
        sequence=self.conn.execute('SELECT coalesce(max(sequence),0)+1 FROM task_attempt WHERE kind=? AND job_id=?',
                                   (kind,job['id'])).fetchone()[0]
        start,end=('after_message_id','through_message_id') if kind=='memory' else ('after_change_id','through_change_id')
        now=time.time();token=job['lease_token']
        self.conn.execute("INSERT INTO task_attempt(id,kind,job_id,user_id,persona_id,sequence,owner_id,pid,status,stage,started_at,heartbeat_at,range_json) "
            "VALUES (?,?,?,?,?,?,?,?,'running','claimed',?,?,?)",(token,kind,job['id'],job['user_id'],job['persona_id'],sequence,
            owner_id or PROCESS_ID,os.getpid(),now,now,json.dumps({start:job[start],end:job[end]})))
        self.event(token,'claimed',{'attempt':sequence,'budget_attempt':job['attempt_count']})

    def event(self,token,stage,detail=None):
        current=self.conn.execute("UPDATE task_attempt SET stage=? WHERE id=? AND status='running'",(stage,token))
        if current.rowcount:
            self.conn.execute('INSERT INTO task_event(attempt_id,created_at,stage,detail_json) VALUES (?,?,?,?)',
                              (token,time.time(),stage,json.dumps(detail or {},ensure_ascii=False)))

    def beat(self,token):
        self.conn.execute("UPDATE task_attempt SET heartbeat_at=? WHERE id=? AND status='running'",(time.time(),token))

    def finish(self,token,status,kind=None,error=None):
        changed=self.conn.execute("UPDATE task_attempt SET status=?,stage=?,finished_at=?,failure_kind=?,error=? WHERE id=? AND status='running'",
            (status,status,time.time(),kind,error,token))
        if changed.rowcount:
            self.conn.execute('INSERT INTO task_event(attempt_id,created_at,stage,detail_json) VALUES (?,?,?,?)',
                (token,time.time(),status,json.dumps({'failure_kind':kind,'error':error},ensure_ascii=False)))

    def recent(self,user,persona,kind=None,job_id=None):
        sql='SELECT * FROM task_attempt WHERE user_id=? AND persona_id=?';params=[user,persona]
        if kind:
            sql+=' AND kind=?';params.append(kind)
        if job_id:
            sql+=' AND job_id=?';params.append(job_id)
        rows=self.conn.execute(sql+' ORDER BY started_at DESC,sequence DESC LIMIT 30',params).fetchall()
        return [{**dict(r),'events':[{**dict(e),'detail':json.loads(e['detail_json'])} for e in self.conn.execute(
            'SELECT created_at,stage,detail_json FROM task_event WHERE attempt_id=? ORDER BY id',(r['id'],))]} for r in rows]

    def latest_trace(self,kind,job_id):
        row=self.conn.execute("SELECT e.detail_json FROM task_event e JOIN task_attempt a ON a.id=e.attempt_id "
                              "WHERE a.kind=? AND a.job_id=? AND e.stage='trace' ORDER BY e.id DESC LIMIT 1",
                              (kind,job_id)).fetchone()
        return json.loads(row[0]).get('trace_id') if row else None

    def cooldown_retry(self,attempt):
        table={'memory':'memory_job','summary':'summary_job'}[attempt['kind']]
        with transaction(self.conn):
            changed=self.conn.execute(f"UPDATE {table} SET status='queued',attempt_count=0,retry_at=NULL,last_error=NULL "
                "WHERE id=? AND status='failed' AND attempt_count>=3 AND lease_token=?",(attempt['job_id'],attempt['id']))
            if changed.rowcount:
                self.conn.execute('INSERT INTO task_event(attempt_id,created_at,stage,detail_json) VALUES (?,?,?,?)',
                    (attempt['id'],time.time(),'retry_after_cooldown','{}'))
            return bool(changed.rowcount)
