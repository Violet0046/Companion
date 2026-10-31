"""Real task workers: failed heartbeats, interruption fencing, budget and migration."""
import json
import sqlite3
import tempfile
import threading
import time
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_durable_flow import context
from test_chat_service import _make_ctx
from test_summary_evolution import service,generate
from fake_memory import no_memory
from companion_ever.entrypoints.web.server import create_app
from companion_ever.config import WebConfig
from companion_ever.infrastructure.database.connection import init_db,connect,apply_schema
from companion_ever.runtime.repository import TaskRepo
from companion_ever.domain.enums import MemoryKind


def add_message(ctx):
    return ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=1,
                                               role='user',content='private source text')


def heartbeat_failure(path):
    ctx=context(path)
    try:
        add_message(ctx);svc=ctx.services.extraction
        svc.runtime.lease_seconds=.3;svc.runtime.heartbeat_interval=.04
        calls=[];renew=svc.jobs.renew
        def flaky(*args,**kwargs):
            calls.append(1)
            if len(calls)==1:
                raise sqlite3.OperationalError('database is locked')
            return renew(*args,**kwargs)
        def model(messages,**kwargs):
            time.sleep(.65)
            return no_memory(messages)
        ctx.llm.chat.side_effect=model
        with patch.object(svc.jobs,'renew',side_effect=flaky):
            result=svc.run({'user_id':'alice','persona_id':'one'})
        assert result['job_status']=='succeeded',result
        assert len(calls)>5
        attempt=TaskRepo(ctx.conn).recent('alice','one')[0]
        assert attempt['status']=='succeeded' and attempt['heartbeat_at']>attempt['started_at']+.2
        assert 'heartbeat_error' in [e['stage'] for e in attempt['events']]
        assert 'private source text' not in json.dumps(attempt)
    finally:
        ctx.conn.close()


def takeover_budget(path):
    ctx=context(path)
    try:
        add_message(ctx);repo=ctx.services.extraction.jobs
        job=repo.enqueue('alice','one');old=repo.claim(job['id'])
        for _ in range(4):
            ctx.conn.execute('UPDATE memory_job SET lease_expires_at=0 WHERE id=?',(job['id'],))
            new=repo.claim(job['id'])
            assert new['attempt_count']==1 and new['lease_token']!=old['lease_token']
            try:
                repo.succeed(job['id'],old['lease_token'])
                raise AssertionError('stale executor committed')
            except RuntimeError:
                pass
            old=new
        repo.fail(job['id'],old['lease_token'],'real network failure')
        for expected in (2,3):
            ctx.conn.execute('UPDATE memory_job SET retry_at=0 WHERE id=?',(job['id'],))
            new=repo.claim(job['id']);assert new['attempt_count']==expected
            repo.fail(job['id'],new['lease_token'],'real network failure')
        final=repo.get(job['id']);assert final['status']=='failed' and final['retry_at'] is None
        attempts=TaskRepo(ctx.conn).recent('alice','one')
        assert sum(a['status']=='interrupted' for a in attempts)==4
        assert sum(a['status']=='failed' for a in attempts)==3
    finally:
        ctx.conn.close()


def shutdown_fence(path):
    ctx=context(path);entered=threading.Event();release=threading.Event();outputs=[]
    try:
        add_message(ctx);svc=ctx.services.extraction
        def model(messages,**kwargs):
            entered.set();assert release.wait(5)
            return no_memory(messages)
        ctx.llm.chat.side_effect=model
        def worker():
            try:
                outputs.append(svc.run({'user_id':'alice','persona_id':'one'}))
            finally:
                ctx.conn.close_current()
        thread=threading.Thread(target=worker);thread.start();assert entered.wait(5)
        svc.runtime.shutdown()
        pending=svc.jobs.enqueue('alice','one');assert pending['status']=='queued' and pending['attempt_count']==0
        release.set();thread.join(5);assert not thread.is_alive()
        assert outputs[0]['job_status']=='queued'
        svc.runtime.start();ctx.llm.chat.side_effect=no_memory
        assert svc.run({'user_id':'alice','persona_id':'one'})['job_status']=='succeeded'
        history=TaskRepo(ctx.conn).recent('alice','one')
        assert {a['status'] for a in history}=={'interrupted','succeeded'}
    finally:
        release.set();ctx.conn.close()


def lost_lease_before_write(path):
    ctx=context(path)
    try:
        add_message(ctx);svc=ctx.services.extraction
        def model(messages,**kwargs):
            ctx.conn.execute("UPDATE memory_job SET lease_expires_at=0 WHERE status='running'")
            return no_memory(messages)
        ctx.llm.chat.side_effect=model
        result=svc.run({'user_id':'alice','persona_id':'one'})
        assert result['job_status']=='queued' and not result['errors']
        job=svc.jobs.enqueue('alice','one');assert job['attempt_count']==0
        history=TaskRepo(ctx.conn).recent('alice','one')
        assert history[0]['failure_kind']=='lease_lost' and history[0]['status']=='interrupted'
        ctx.llm.chat.side_effect=no_memory
        assert svc.run({'user_id':'alice','persona_id':'one'})['job_status']=='succeeded'
    finally:
        ctx.conn.close()


def summary_lease_and_api(path):
    ctx=context(path)
    try:
        svc=service(ctx);ctx.services.note.create_note('alice','one',MemoryKind.FACT,'private fact')
        generate(ctx);renew=svc.repo.renew;calls=[]
        original_model=ctx.llm.chat.side_effect
        def model(*args,**kwargs):
            time.sleep(.5);return original_model(*args,**kwargs)
        def flaky(*args,**kwargs):
            calls.append(1)
            if len(calls)==1:
                raise sqlite3.OperationalError('database is busy')
            return renew(*args,**kwargs)
        svc.runtime.lease_seconds=.25;svc.runtime.heartbeat_interval=.03
        ctx.llm.chat.side_effect=model
        with patch.object(svc.repo,'renew',side_effect=flaky):
            result=svc.run(svc.request('alice','one',background=False)['job_id'])
        assert result['status']=='succeeded',result
        with TestClient(create_app(ctx,WebConfig(pin='1234'))) as client:
            url='/api/persona/alice/one/task-attempts'
            assert client.get(url).status_code==401
            response=client.get(url,headers={'X-Pin':'1234'});assert response.status_code==200
            data=response.json();assert data[0]['status']=='succeeded'
            assert 'private fact' not in response.text
            assert client.get('/api/persona/foreign/one/task-attempts',headers={'X-Pin':'1234'}).status_code==403
    finally:
        ctx.conn.close()


def migration(path):
    ctx=context(path);add_message(ctx)
    job=ctx.services.extraction.jobs.enqueue('alice','one')
    ctx.conn.execute("UPDATE memory_job SET status='failed',attempt_count=3,last_error='执行租约到期，重试次数已用尽' WHERE id=?",(job['id'],))
    ctx.conn.execute('DROP TABLE task_event');ctx.conn.execute('DROP TABLE task_attempt');ctx.conn.execute('PRAGMA user_version=4');ctx.conn.close()
    conn=init_db(path,check_same_thread=False)
    try:
        row=conn.execute('SELECT * FROM memory_job WHERE id=?',(job['id'],)).fetchone()
        assert row['status']=='queued' and row['attempt_count']==0
        assert row['after_message_id']==job['after_message_id'] and row['through_message_id']==job['through_message_id']
        history=TaskRepo(conn).recent('alice','one')
        assert history[0]['stage']=='legacy_snapshot' and history[0]['status']=='interrupted'
        assert '重试次数已用尽' in history[0]['error']
        assert conn.execute('PRAGMA user_version').fetchone()[0]==6
        assert len(list(path.parent.glob(path.name+'.bak-before-task-runtime-*')))==1
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        conn.close()


def process_restart(path):
    child=r'''
import sys,time,json
from companion_ever.bootstrap.app import build_app
import companion_ever.bootstrap.app as app
from companion_ever.config import AppConfig
class Model:
    def chat(self,messages,**kwargs):
        time.sleep(60)
        return '{}'
app.create_llm_client=lambda cfg:Model()
ctx=build_app(AppConfig(sys.argv[1],background={'enabled':False},idle_session_minutes=0))
ctx.repos.user.create('alice');ctx.services.persona.create_persona('alice','one','{}')
ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=1,role='user',content='worker input')
runtime=ctx.services.extraction.runtime;runtime.lease_seconds=.3;runtime.heartbeat_interval=.03
ctx.services.extraction.run({'user_id':'alice','persona_id':'one'})
'''
    process=subprocess.Popen([sys.executable,'-u','-c',child,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
                try:
                    row=conn.execute("SELECT heartbeat_at-started_at FROM task_attempt WHERE status='running'").fetchone()
                    if row and row[0]>.07:
                        break
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
            assert process.poll() is None,process.communicate()[1].decode(errors='replace')
            time.sleep(.02)
        else:
            raise AssertionError('child did not start a renewing execution')
        process.terminate();process.wait(5)
        time.sleep(.35)
        ctx=_make_ctx(str(path),check_same_thread=False)
        try:
            ctx.llm.chat.side_effect=no_memory
            result=ctx.services.extraction.run({'user_id':'alice','persona_id':'one'})
            assert result['job_status']=='succeeded',result
            history=TaskRepo(ctx.conn).recent('alice','one')
            assert len(history)==2 and {a['status'] for a in history}=={'succeeded','interrupted'}
            assert len({a['owner_id'] for a in history})==2
            job=ctx.services.extraction.jobs.get(result['job_id']);assert job['attempt_count']==1
        finally:
            ctx.conn.close()
    finally:
        if process.poll() is None:
            process.kill();process.wait(5)
        process.communicate()


def migration_rollback(path):
    ctx=context(path);add_message(ctx)
    job=ctx.services.extraction.jobs.enqueue('alice','one')
    ctx.conn.execute("UPDATE memory_job SET status='failed',attempt_count=3,last_error='执行租约到期，重试次数已用尽' WHERE id=?",(job['id'],))
    ctx.conn.execute('DROP TABLE task_event');ctx.conn.execute('DROP TABLE task_attempt');ctx.conn.execute('PRAGMA user_version=4');ctx.conn.close()
    conn=connect(path)
    def deny_event(action,name,*args):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and name=='task_event' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_event)
    try:
        try:
            apply_schema(conn)
            raise AssertionError('injected migration error was ignored')
        except sqlite3.DatabaseError:
            pass
        conn.set_authorizer(None)
        assert conn.execute('PRAGMA user_version').fetchone()[0]==4
        row=conn.execute('SELECT status,attempt_count FROM memory_job WHERE id=?',(job['id'],)).fetchone()
        assert tuple(row)==('failed',3)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_attempt'").fetchone()
    finally:
        conn.close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        for name,test in [('heartbeat',heartbeat_failure),('budget',takeover_budget),('shutdown',shutdown_fence),
                          ('summary',summary_lease_and_api),('migration',migration),('process',process_restart),('lost',lost_lease_before_write),
                          ('rollback',migration_rollback)]:
            test(Path(tmp)/(name+'.db'))
    print('[OK] heartbeat recovery, interruption fencing, independent failure budget, attempt history and v5 migration')
