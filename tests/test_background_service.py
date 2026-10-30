"""No buttons: note batching, independent summary cadence and automatic baseline."""
import json
import tempfile
import threading
import time
from pathlib import Path
from fastapi.testclient import TestClient
from test_durable_flow import context
from test_summary_evolution import service,empty,item
from test_kind_storage import draft,proposal
from companion_ever.config import BackgroundConfig,WebConfig
from companion_ever.services.background_service import BackgroundService
from companion_ever.api.server import create_app
from companion_ever.domain.enums import MemoryKind


def configure(ctx,**options):
    ctx.cfg.background=BackgroundConfig(**options)
    svc=service(ctx);bg=BackgroundService(ctx);ctx.services.background=bg
    ctx.services.session_close.on_memory_settled=bg.memory_settled
    active=0;maximum=0;lock=threading.Lock()
    def model(messages,**kwargs):
        nonlocal active,maximum
        with lock:
            active+=1;maximum=max(maximum,active)
        try:
            time.sleep(.02)
            payload=json.loads(messages[1]['content'])
            if 'new_message_ids' in payload:
                new=set(payload['new_message_ids'])
                sources=[m for m in payload['messages'] if m['role']=='user' and m['id'] in new]
                return json.dumps(draft([proposal(m['id'],m['content'],id='p'+str(i)) for i,m in enumerate(sources)]))
            document=empty()
            document['user_profile']['identity']=[item(f) for f in payload['current_facts'][:8]]
            return json.dumps(document)
        finally:
            with lock:
                active-=1
    ctx.llm.chat.side_effect=model
    return bg,svc,lambda:maximum


def wait_for(ctx,bg,predicate):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        bg.tick()
        if predicate() and not ctx.services.session_close.is_closing('alice','one') and not ctx.services.summary._active:
            return
        time.sleep(.02)
    raise AssertionError('background tasks did not settle')


def automatic(path):
    ctx=context(path)
    try:
        bg,svc,maximum=configure(ctx,memory_message_threshold=3,summary_fact_threshold=2)
        for i in range(2):
            ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=i+1,role='user',content='preference '+str(i))
        bg.tick();assert ctx.llm.chat.call_count==0
        ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=3,role='user',content='preference 2')
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.conn.execute('SELECT count(*) FROM note').fetchone()[0]==3
        assert ctx.conn.execute("SELECT count(*) FROM conversation_log WHERE role='session_close'").fetchone()[0]==0
        assert maximum()==1 and ctx.llm.chat.call_count==2

        before=ctx.llm.chat.call_count
        ctx.services.note.create_note('alice','one',MemoryKind.FACT,'another preference')
        bg.tick();time.sleep(.05);assert ctx.llm.chat.call_count==before
        ctx.services.note.create_note('alice','one',MemoryKind.FACT,'second new preference')
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.llm.chat.call_count==before+1

        before=ctx.llm.chat.call_count
        nid=ctx.conn.execute('SELECT id FROM note LIMIT 1').fetchone()[0]
        ctx.repos.note.archive(nid)
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.llm.chat.call_count==before+1
        assert nid not in json.dumps(svc.status('alice','one')['document'])
        before=ctx.llm.chat.call_count
        for _ in range(4):
            bg.tick()
        assert ctx.llm.chat.call_count==before
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def idle_and_age(path):
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx,memory_message_threshold=6,memory_idle_seconds=60,memory_idle_min_messages=1,
                           summary_fact_threshold=5,summary_interval_seconds=900)
        g,_=ctx.services.chat.receive('alice','one','a durable preference','request')
        ctx.conn.execute("UPDATE conversation_log SET created_at=datetime('now','-2 minutes')")
        bg.tick();assert ctx.llm.chat.call_count==0,'do not extract while the reply is generating'
        ctx.services.chat.generations.finish(g['id'],'completed')
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        before=ctx.llm.chat.call_count
        nid=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'small update')
        bg.tick();time.sleep(.05);assert ctx.llm.chat.call_count==before
        ctx.conn.execute("UPDATE fact_change SET created_at=datetime('now','-20 minutes') WHERE note_id=?",(nid,))
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.llm.chat.call_count==before+1
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def no_first_button(path):
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx)
        ctx.services.note.create_note('alice','one',MemoryKind.FACT,'existing fact awaiting its first summary')
        with TestClient(create_app(ctx,WebConfig())) as client:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                status=client.get('/api/persona/alice/one/summary-status').json()
                if status['current'] and not svc._active:
                    break
                time.sleep(.02)
            assert status['current'],status
            assert ctx.llm.chat.call_count==1
    finally:
        ctx.conn.close()


def network_cooldown(path):
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx)
        model=ctx.llm.chat.side_effect
        ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=1,role='user',content='a persistent preference')
        ctx.llm.chat.side_effect=ConnectionError('temporary upstream outage')
        repo=ctx.services.extraction.jobs
        for _ in range(3):
            result=ctx.services.extraction.run({'user_id':'alice','persona_id':'one'})
            assert result['job_status']=='failed'
            ctx.conn.execute('UPDATE memory_job SET retry_at=0 WHERE id=? AND retry_at IS NOT NULL',(result['job_id'],))
        assert repo.get(result['job_id'])['retry_at'] is None
        before=ctx.llm.chat.call_count;bg.tick();time.sleep(.03)
        assert ctx.llm.chat.call_count==before,'exhausted network attempts must cool down'
        ctx.conn.execute("UPDATE task_attempt SET finished_at=? WHERE status='failed'",(time.time()-901,))
        ctx.llm.chat.side_effect=model
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.conn.execute('SELECT count(*) FROM note').fetchone()[0]==1
        assert ctx.conn.execute("SELECT count(*) FROM task_event WHERE stage='retry_after_cooldown'").fetchone()[0]==1
        assert ctx.conn.execute("SELECT count(*) FROM task_attempt WHERE status='failed' AND failure_kind='transport'").fetchone()[0]==3
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        for name,test in [('automatic',automatic),('idle',idle_and_age),('baseline',no_first_button),('cooldown',network_cooldown)]:
            test(Path(tmp)/(name+'.db'))
    print('[OK] automatic message/idle triggers, low-frequency summaries, deletion priority and first-use baseline')
