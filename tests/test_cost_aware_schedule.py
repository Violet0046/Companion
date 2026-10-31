"""Cost regressions: meaningful changes, text batches and bounded drain horizons."""
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from test_durable_flow import context
from test_background_service import configure,wait_for
from test_summary_evolution import generate,drain
from companion_ever.config import AppConfig,BackgroundConfig
from companion_ever.domain.enums import MemoryKind
from companion_ever.memory.notes.links import NoteLinkRepo


def log(ctx,role='user',text='new message'):
    return ctx.services.conversation.log_turn(user_id='alice',persona_id='one',role=role,content=text,turn_index=1)


def fact(ctx,text):
    return ctx.services.note.create_note('alice','one',MemoryKind.FACT,text)


def settled(ctx,predicate):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        if predicate() and not ctx.services.session_close.is_closing('alice','one') and not ctx.services.summary._active:
            return
        time.sleep(.01)
    raise AssertionError('worker did not settle')


def defaults(path):
    cfg=AppConfig.from_env({},dotenv_path=None).background
    assert (cfg.memory_message_threshold,cfg.memory_idle_seconds,cfg.memory_idle_min_messages,cfg.memory_max_wait_seconds)==(12,600,3,86400)
    assert (cfg.summary_fact_threshold,cfg.summary_interval_seconds)==(8,21600)
    cfg=AppConfig.from_env({'COMPANION_MEMORY_IDLE_MIN_MESSAGES':'2','COMPANION_MEMORY_MAX_WAIT_SECONDS':'123'},dotenv_path=None).background
    assert cfg.memory_idle_min_messages==2 and cfg.memory_max_wait_seconds==123
    for key in ('memory_idle_min_messages','memory_max_wait_seconds'):
        try:BackgroundConfig(**{key:0})
        except ValueError:pass
        else:raise AssertionError('invalid cadence accepted')


def idle_thresholds(path):
    ctx=context(path)
    try:
        bg,_,_=configure(ctx)
        log(ctx);log(ctx,'assistant');log(ctx,'assistant')
        ctx.conn.execute("UPDATE conversation_log SET created_at=datetime('now','-11 minutes')")
        row=ctx.conn.execute('SELECT * FROM conversation').fetchone()
        with patch.object(ctx.services.session_close,'close_session_background') as launch:
            bg.consider_memory(row);assert not launch.called,'one short exchange must not trigger on idle'
            log(ctx);log(ctx)
            bg.consider_memory(row);assert not launch.called,'recent messages reset the idle timer'
            ctx.conn.execute("UPDATE conversation_log SET created_at=datetime('now','-11 minutes')")
            bg.consider_memory(row);assert launch.call_count==1
            launch.reset_mock()
            ctx.conn.execute("UPDATE conversation_log SET created_at=datetime('now')")
            for _ in range(9):log(ctx)
            bg.consider_memory(row);assert launch.call_count==1,'12 user messages trigger even without idle'
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def maximum_wait(path):
    ctx=context(path)
    try:
        bg,_,_=configure(ctx)
        mid=log(ctx)
        ctx.conn.execute("UPDATE conversation_log SET created_at=datetime('now','-25 hours') WHERE id=?",(mid,))
        log(ctx,'assistant')
        row=ctx.conn.execute('SELECT * FROM conversation').fetchone()
        with patch.object(ctx.services.session_close,'close_session_background') as launch:
            bg.consider_memory(row);assert launch.call_count==1,'oldest pending input supplies a starvation bound'
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def no_meaning_no_model(path):
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx)
        nid=fact(ctx,'A stable preference');generate(ctx);drain(svc)
        before=ctx.llm.chat.call_count
        ctx.repos.note.update(nid,importance=.8)
        ctx.repos.note.update(nid,content='Temporary edit')
        ctx.repos.note.update(nid,content='A stable preference')
        ctx.repos.note.archive(nid);ctx.repos.note.unarchive(nid)
        transient=fact(ctx,'never became a lasting fact')
        NoteLinkRepo(ctx.conn).create(transient,nid,'supersedes','alice','one',reason='temporary revision',job_id=None)
        ctx.conn.execute("DELETE FROM note_link WHERE from_id=? AND to_id=? AND link_type='supersedes'",(transient,nid))
        ctx.conn.execute('DELETE FROM note WHERE id=?',(transient,))
        mid=log(ctx,text=' '.join('proof'+str(i) for i in range(260)))
        for i in range(260):
            ctx.conn.execute('INSERT INTO note_evidence(note_id,message_id,quote) VALUES (?,?,?)',(nid,mid,'proof'+str(i)))
        records=ctx.conn.execute('SELECT count(*) FROM fact_change').fetchone()[0]
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.llm.chat.call_count==before,'metadata, net reverts and evidence should use no model'
        assert ctx.conn.execute('SELECT count(*) FROM fact_change').fetchone()[0]==records,'audit events must remain intact'
        assert 'A stable preference' in svc.prompt_text('alice','one')
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def semantic_count_and_age(path):
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx)
        fact(ctx,'baseline');generate(ctx);drain(svc);before=ctx.llm.chat.call_count
        for i in range(7):fact(ctx,'new meaningful fact '+str(i))
        bg.tick();time.sleep(.05);assert ctx.llm.chat.call_count==before
        fact(ctx,'eighth meaningful fact')
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.llm.chat.call_count==before+1
        before=ctx.llm.chat.call_count
        nid=fact(ctx,'one fresh fact')
        for i in range(10):ctx.repos.note.update(nid,content='one changed fact '+str(i))
        bg.tick();time.sleep(.05);assert ctx.llm.chat.call_count==before,'ten edits to one fact are one change'
        ctx.conn.execute("UPDATE fact_change SET created_at=datetime('now','-7 hours') WHERE note_id=?",(nid,))
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.llm.chat.call_count==before+1
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def referenced_priority(path):
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx)
        selected=fact(ctx,'included in the summary');other=fact(ctx,'not included in the summary')
        generate(ctx,lambda fs:[f for f in fs if f['id']==selected]);drain(svc)
        before=ctx.llm.chat.call_count
        ctx.repos.note.update(other,content='unreferenced fact changed')
        ctx.repos.note.archive(other)
        bg.tick();time.sleep(.05);assert ctx.llm.chat.call_count==before
        ctx.repos.note.update(selected,content='referenced fact changed')
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert ctx.llm.chat.call_count==before+1
        assert 'referenced fact changed' in svc.prompt_text('alice','one')
        ctx.repos.note.archive(selected)
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert not svc.prompt_text('alice','one') and ctx.llm.chat.call_count==before+1
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def memory_horizon(path):
    from fake_memory import no_memory
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx)
        first=log(ctx);arrived=[]
        def reply(messages,**kwargs):
            arrived.append(log(ctx,text='arrived during extraction'))
            return no_memory(messages)
        ctx.llm.chat.side_effect=reply
        ctx.services.session_close.close_session_background('alice','one',boundary=False)
        settled(ctx,lambda:ctx.conn.execute("SELECT count(*) FROM memory_job WHERE status='succeeded'").fetchone()[0]==1)
        bg.tick();time.sleep(.05)
        assert ctx.llm.chat.call_count==1
        job=ctx.conn.execute('SELECT * FROM memory_job').fetchone()
        assert job['through_message_id']==first and arrived[0]>first
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def summary_horizon(path):
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx);fact(ctx,'first fact')
        model=ctx.llm.chat.side_effect
        def reply(messages,**kwargs):
            fact(ctx,'arrived during summary')
            return model(messages,**kwargs)
        ctx.llm.chat.side_effect=reply
        svc.request('alice','one')
        settled(ctx,lambda:svc.repo.checkpoint('alice','one') is not None)
        bg.tick();time.sleep(.05)
        assert ctx.llm.chat.call_count==1 and svc.status('alice','one')['pending_changes']==1
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def completion_handoff(path):
    ctx=context(path)
    try:
        _,svc,_=configure(ctx);log(ctx,text='a lasting preference')
        ctx.services.session_close.close_session_background('alice','one',boundary=False)
        # Deliberately do not scan: the completion hook must schedule baseline.
        settled(ctx,lambda:svc.status('alice','one')['current'])
        assert ctx.llm.chat.call_count==2
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


def bounded_window_progress(path):
    ctx=context(path)
    try:
        bg,svc,_=configure(ctx);fact(ctx,'baseline');generate(ctx);drain(svc)
        nid=fact(ctx,'pending meaningful change')
        mid=log(ctx,text=' '.join('proof'+str(i) for i in range(210)))
        for i in range(210):
            ctx.conn.execute('INSERT INTO note_evidence(note_id,message_id,quote) VALUES (?,?,?)',(nid,mid,'proof'+str(i)))
        for i in range(7):fact(ctx,'meaningful change '+str(i))
        cursor=svc.repo.checkpoint('alice','one')['last_change_id']
        window=svc.repo.change_window('alice','one',cursor)
        assert len(window['changes'])==1 and window['has_more']
        wait_for(ctx,bg,lambda:svc.status('alice','one')['current'])
        assert svc.status('alice','one')['last_change_id']==svc.repo.latest_change('alice','one')
    finally:
        ctx.services.extraction.runtime.shutdown();ctx.conn.close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        for test in (defaults,idle_thresholds,maximum_wait,no_meaning_no_model,semantic_count_and_age,
                     referenced_priority,memory_horizon,summary_horizon,completion_handoff,bounded_window_progress):
            test(Path(tmp)/(test.__name__+'.db'))
    print('[OK] cost-aware triggers, compacted changes, no-model acknowledgements, fixed horizons and handoff')
