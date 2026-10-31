"""Incremental, grounded summary evolution driven by durable fact operations."""
import json
import sqlite3
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_durable_flow import context
from companion_ever.domain.enums import MemoryKind
from companion_ever.memory.summaries.service import SummaryService,SummaryDocument,Profile,Relationship
from companion_ever.infrastructure.database.connection import init_db


def empty():
    return SummaryDocument(user_profile=Profile(),relationship_state=Relationship()).model_dump()


def item(fact):
    return {'text':fact['content'],'reality':fact['reality'],
            'citations':[{'note_id':fact['id'],'quote':fact['content']}]}


def service(ctx):
    from companion_ever.memory.scheduling import MemoryScheduler
    from companion_ever.bootstrap.lifecycle import build_lifecycle
    svc=SummaryService(ctx.services.note.authorizer,ctx.repos.note,ctx.repos.block,ctx.llm)
    ctx.services.summary=svc;ctx.services.prompt.summary_service=svc
    ctx.workflows.summary=svc.graph
    bg=MemoryScheduler(ctx.cfg.background,ctx.services.note.authorizer,ctx.services.conversation,ctx.services.session_close,svc)
    ctx.services.background=bg
    ctx.services.session_close.on_memory_settled=bg.memory_settled
    ctx.lifecycle=build_lifecycle(ctx.cfg,ctx.conn,ctx.services)
    return svc


def generate(ctx,selector=lambda facts:facts[:8]):
    seen=[]
    def reply(messages,**kwargs):
        payload=json.loads(messages[-1]['content']);seen.append(payload)
        doc=empty();doc['user_profile']['identity']=[item(f) for f in selector(payload['current_facts'])]
        return json.dumps(doc)
    ctx.llm.chat.side_effect=reply
    return seen


def drain(svc,user='alice',persona='one'):
    result=svc.request(user,persona,background=False)
    job=svc.repo.get(result['job_id']) if result['job_id'] else None
    while job:
        out=svc.run(job['id'])
        assert out['status']=='succeeded',out
        job=svc.repo.enqueue(user,persona)
    return svc.status(user,persona)


def evolution(path):
    ctx=context(path)
    try:
        svc=service(ctx)
        a=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'用户住在杭州')
        b=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'无关且不在摘要中的事实')
        ep=ctx.services.note.create_note('alice','one',MemoryKind.EPISODE,'一次不该送入摘要的经历')
        seen=generate(ctx,lambda fs:[f for f in fs if f['id']==a])
        svc.resume_pending()
        assert ctx.llm.chat.call_count==0  # First initialization is explicit.
        status=drain(svc)
        assert status['current'] and len(seen)==1
        assert ep not in {f['id'] for f in seen[0]['current_facts']}
        original_cursor=status['last_change_id']
        calls=ctx.llm.chat.call_count
        assert svc.request('alice','one',background=False)['status']=='unchanged'
        assert ctx.llm.chat.call_count==calls
        ctx.services.note.set_pinned('alice','one',a,True)
        ctx.repos.note.bump_access(a)
        assert svc.repo.latest_change('alice','one')==original_cursor
        ctx.repos.note.update(a,content='用户现在住在苏州')
        assert not svc.status('alice','one')['current'] and '用户住在杭州' in svc.prompt_text('alice','one')
        status=drain(svc)
        assert status['current']
        assert {f['id'] for f in seen[-1]['current_facts']}=={a}  # No full-fact resubmission.
        assert b not in json.dumps(seen[-1])
        assert any(e['operation']=='update' and e['before']['content']=='用户住在杭州' for e in seen[-1]['changes'])
        prompt=ctx.services.prompt.build('alice','one',query='早上好')['system_prompt']
        assert '用户画像与关系概况' in prompt and '用户现在住在苏州' in prompt
        mid=ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=1,role='user',content='用户现在住在苏州')
        ctx.conn.execute('INSERT INTO note_evidence(note_id,message_id,quote) VALUES (?,?,?)',(a,mid,'用户现在住在苏州'))
        assert '用户现在住在苏州' in svc.prompt_text('alice','one')
        evidence_calls=ctx.llm.chat.call_count
        drain(svc)
        assert ctx.llm.chat.call_count==evidence_calls  # Confirmation evidence alone does not require a new summary.
        ctx.repos.note.archive(a)
        calls=ctx.llm.chat.call_count
        assert '用户现在住在苏州' in svc.prompt_text('alice','one')
        status=drain(svc)
        assert ctx.llm.chat.call_count==calls and not status['document']['user_profile']['identity']
        assert not svc.prompt_text('alice','one')
        ctx.repos.note.unarchive(a);drain(svc)
        assert svc.status('alice','one')['document']['user_profile']['identity']
        ctx.conn.execute('DELETE FROM note WHERE id=?',(a,))
        assert '用户现在住在苏州' in svc.prompt_text('alice','one')
        status=drain(svc)
        assert not status['document']['user_profile']['identity']
        assert not svc.prompt_text('alice','one')
        removed=ctx.conn.execute("SELECT * FROM fact_change WHERE note_id=? AND operation='delete'",(a,)).fetchone()
        assert json.loads(removed['before_json'])['content']=='用户现在住在苏州'
        assert svc.repo.latest_change('alice','one')==status['last_change_id']
    finally:
        ctx.conn.close()


def atomic_and_retry(path):
    ctx=context(path)
    try:
        svc=service(ctx)
        nid=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'用户喜欢茶')
        generate(ctx)
        before=svc.repo.blocks('alice','one')
        result=svc.request('alice','one',background=False)
        original=ctx.repos.block.upsert
        writes=[]
        def fail(user,persona,kind,fields):
            original(user,persona,kind,fields);writes.append(kind)
            if len(writes)==2:
                raise RuntimeError('failure after second block write')
        with patch.object(ctx.repos.block,'upsert',side_effect=fail):
            out=svc.run(result['job_id'])
        assert out['status']=='failed' and svc.repo.checkpoint('alice','one') is None
        assert svc.repo.blocks('alice','one')==before
        status=drain(svc)
        assert status['current']
        previous=status['last_change_id'];saved_blocks=svc.repo.blocks('alice','one')
        ctx.repos.note.update(nid,content='用户现在不喜欢茶')
        def stale_response(messages,**kwargs):
            return json.dumps(status['document'])  # Old quote cannot justify the new summary.
        ctx.llm.chat.side_effect=stale_response
        job=svc.request('alice','one',background=False)
        out=svc.run(job['job_id'])
        assert out['status']=='failed' and svc.repo.checkpoint('alice','one')['last_change_id']==previous
        assert svc.repo.blocks('alice','one')==saved_blocks
        assert not svc.status('alice','one')['current']
        assert '用户喜欢茶' in svc.prompt_text('alice','one')
        generate(ctx);drain(svc)
        assert '不喜欢' in svc.prompt_text('alice','one')
    finally:
        ctx.conn.close()


def continuous_prompt_context(path):
    ctx=context(path)
    try:
        svc=service(ctx)
        preference=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'用户长期喜欢茶')
        agreement=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'用户与角色约定周末一起读书')
        def document(messages,**kwargs):
            payload=json.loads(messages[1]['content'])
            facts={f['id']:f for f in payload['current_facts']}
            result=empty()
            result['user_profile']['preferences']=[item(facts[preference])]
            result['relationship_state']['agreements']=[item(facts[agreement])]
            return json.dumps(result)
        ctx.llm.chat.side_effect=document
        drain(svc)
        previous=svc.prompt_text('alice','one')
        assert '用户长期喜欢茶' in previous and '周末一起读书' in previous

        def chat_context():
            prompt=ctx.services.prompt.build('alice','one',query='你好')
            system=prompt['messages'][0]['content']
            assert '用户画像' in system and '关系概况' in system
            assert '周末一起读书' in system
            assert '以最新陈述和当前有效事实为准' in system
            return prompt

        ctx.services.note.create_note('alice','one',MemoryKind.FACT,'用户家里养了一只猫')
        assert not svc.status('alice','one')['current']
        assert svc.prompt_text('alice','one')==previous
        chat_context()  # Pending operations alone do not remove either summary.
        job=svc.request('alice','one',background=False)
        assert svc.repo.get(job['job_id'])['status']=='queued'
        assert svc.prompt_text('alice','one')==previous
        def timeout(messages,**kwargs):
            assert svc.repo.get(job['job_id'])['status']=='running'
            assert svc.prompt_text('alice','one')==previous
            chat_context()
            raise TimeoutError('temporary summary outage')
        ctx.llm.chat.side_effect=timeout
        assert svc.run(job['job_id'])['status']=='failed'
        assert svc.prompt_text('alice','one')==previous
        chat_context()  # Failed updates keep the committed summaries too.

        correction='更正，我现在长期喜欢咖啡，不喜欢茶了'
        ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=1,role='user',content=correction)
        ctx.repos.note.update(preference,content='用户现在长期喜欢咖啡，不喜欢茶')
        prompt=chat_context()
        assert '用户长期喜欢茶' in prompt['messages'][0]['content']
        assert any(m['role']=='user' and correction in m['content'] for m in prompt['messages'])
        seen=[]
        def updated(messages,**kwargs):
            seen.append(svc.prompt_text('alice','one'))
            chat_context()
            return document(messages,**kwargs)
        ctx.llm.chat.side_effect=updated
        drain(svc)
        # The original job ends before the later correction event. After its
        # successful commit, the next batch sees the new summary despite backlog.
        assert len(seen)==2 and seen[0]==previous
        assert '用户现在长期喜欢咖啡' in seen[1] and '周末一起读书' in seen[1]
        final=chat_context()['messages'][0]['content']
        assert '用户现在长期喜欢咖啡' in final and '用户长期喜欢茶' not in final
        assert svc.status('alice','one')['current']
    finally:
        ctx.conn.close()


def batches_and_reactivation(path):
    ctx=context(path)
    try:
        svc=service(ctx)
        expected={ctx.services.note.create_note('alice','one',MemoryKind.FACT,f'事实{i}') for i in range(65)}
        seen=generate(ctx)
        status=drain(svc)
        assert status['current'] and len(seen)==3
        assert max(len(p['changes']) for p in seen)<=24
        assert max(len(p['current_facts']) for p in seen)<=32
        assert {change['note_id'] for payload in seen for change in payload['changes']}==expected
        old=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'旧地址杭州')
        new=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'新地址苏州')
        from companion_ever.memory.notes.links import NoteLinkRepo
        NoteLinkRepo(ctx.conn).create(new,old,'supersedes','alice','one',reason='明确迁居',job_id=None)
        drain(svc)
        assert old not in {f['id'] for f in seen[-1]['current_facts']}
        ctx.repos.note.archive(new);drain(svc)
        assert old in {f['id'] for f in seen[-1]['current_facts']}
        assert ctx.conn.execute("SELECT 1 FROM fact_change WHERE note_id=? AND operation='supersedes_added'",(old,)).fetchone()
    finally:
        ctx.conn.close()


def migration(path):
    from importlib import resources
    conn=sqlite3.connect(path)
    conn.executescript(resources.files('companion_ever.infrastructure.database').joinpath('schema.sql').read_text(encoding='utf-8'))
    conn.execute("INSERT INTO user(id) VALUES ('alice')")
    for nid,kind in [('fact','fact'),('episode','episode')]:
        conn.execute("INSERT INTO note(id,user_id,persona_id,kind,content) VALUES (?,'alice','one',?,'kept')",(nid,kind))
    conn.execute("INSERT INTO memory_block(id,user_id,persona_id,block_type,fields_json) VALUES ('old','alice','one','user_profile','{\"old\":true}')")
    conn.execute('PRAGMA user_version=3');conn.commit();conn.close()
    conn=init_db(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0]==6
    assert [tuple(r) for r in conn.execute('SELECT note_id,operation FROM fact_change')]==[('fact','bootstrap')]
    assert conn.execute('SELECT fields_json FROM memory_block').fetchone()[0]=='{"old":true}'
    conn.close();conn=init_db(path)
    assert conn.execute('SELECT count(*) FROM fact_change').fetchone()[0]==1
    conn.close()
    assert len(list(path.parent.glob(path.name+'.bak-before-summary-journal-*')))==1


def stale_and_fenced(path):
    ctx=context(path)
    try:
        svc=service(ctx)
        nid=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'原始事实')
        def stale(messages,**kwargs):
            payload=json.loads(messages[-1]['content'])
            result=empty();result['user_profile']['identity']=[item(payload['current_facts'][0])]
            ctx.repos.note.update(nid,content='生成期间已变更的事实')
            return json.dumps(result)
        ctx.llm.chat.side_effect=stale
        job=svc.request('alice','one',background=False)
        assert svc.run(job['job_id'])['status']=='failed'
        assert svc.repo.checkpoint('alice','one') is None
        generate(ctx);drain(svc)
        assert '生成期间已变更' in svc.prompt_text('alice','one')
        ctx.repos.note.update(nid,content='另一条新事实')
        job=svc.request('alice','one',background=False)
        def takeover(messages,**kwargs):
            payload=json.loads(messages[-1]['content'])
            doc=empty();doc['user_profile']['identity']=[item(payload['current_facts'][0])]
            ctx.conn.execute("UPDATE summary_job SET lease_token='new-owner' WHERE id=?",(job['job_id'],))
            return json.dumps(doc)
        ctx.llm.chat.side_effect=takeover
        old_cursor=svc.repo.checkpoint('alice','one')['last_change_id']
        result=svc.run(job['job_id'])
        assert result['status']=='running' and result['lease_token']=='new-owner'
        assert svc.repo.checkpoint('alice','one')['last_change_id']==old_cursor
    finally:
        ctx.conn.close()


def api_scope(path):
    import time
    from fastapi.testclient import TestClient
    from companion_ever.entrypoints.web.server import create_app
    from companion_ever.config import WebConfig
    ctx=context(path)
    try:
        svc=service(ctx)
        ctx.services.note.create_note('alice','one',MemoryKind.FACT,'可核对的用户事实')
        ctx.services.persona.create_persona('alice','two','{}')
        generate(ctx)
        with TestClient(create_app(ctx,WebConfig(pin='secret'))) as client:
            base='/api/persona/alice/one'
            headers={'x-pin':'secret'}
            assert client.get(base+'/fact-changes').status_code==401
            assert client.post(base+'/summaries/refresh').status_code==401
            response=client.post(base+'/summaries/refresh',headers=headers)
            assert response.status_code==200
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                result=client.get(base+'/summary-status',headers=headers).json()
                if result['current'] and not svc._active:
                    break
                time.sleep(.01)
            assert result['current'],result
            assert client.get(base+'/summary-jobs',headers=headers).json()[0]['status']=='succeeded'
            events=client.get(base+'/fact-changes',headers=headers).json()
            assert len(events)==1 and events[0]['after']['content']=='可核对的用户事实'
            assert client.get('/api/persona/alice/two/fact-changes',headers=headers).json()==[]
            assert not client.get('/api/persona/alice/two/summary-status',headers=headers).json()['current']
            assert client.get('/api/persona/missing/one/summary-status',headers=headers).status_code==403
            count=ctx.llm.chat.call_count
            assert client.post(base+'/summaries/refresh',headers=headers).json()['status']=='unchanged'
            assert ctx.llm.chat.call_count==count
            # The background scanner, rather than the note writer, triggers subsequent evolution.
            ctx.repos.note.update(events[0]['note_id'],content='后来更新的用户事实')
            assert ctx.llm.chat.call_count==count
            assert not client.get(base+'/summary-status',headers=headers).json()['current']
            svc.resume_pending()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                result=client.get(base+'/summary-status',headers=headers).json()
                if result['current'] and not svc._active:
                    break
                time.sleep(.01)
            assert result['current'] and ctx.llm.chat.call_count==count+1,result
    finally:
        ctx.conn.close()


def note_job_evidence(path):
    from test_kind_storage import extract,draft,proposal
    ctx=context(path)
    try:
        svc=service(ctx)
        result=extract(ctx,'我长期喜欢喝茶',lambda mid,_:draft([proposal(mid,'我长期喜欢喝茶',content='用户长期喜欢茶')]))
        assert result['job_status']=='succeeded',result
        events=svc.conn.execute('SELECT * FROM fact_change ORDER BY id').fetchall()
        assert [e['operation'] for e in events]==['create','evidence_added']
        assert json.loads(events[0]['after_json'])['reality']=='user_report'
        assert all(e['source_job_id']==result['job_id'] for e in events)
        assert ctx.llm.chat.call_count==1 and svc.repo.checkpoint('alice','one') is None
    finally:
        ctx.conn.close()


def graph_branches(path):
    from companion_ever.infrastructure.tracing import TraceStore,set_default_store
    ctx=context(path);store=TraceStore(path.parent/'summary-graph-traces');set_default_store(store)
    try:
        svc=service(ctx)
        assert ctx.workflows.summary is svc.graph
        from companion_ever.infrastructure.llm.traced_client import TracedLLMClient
        class Model:
            model='fake';last_reasoning=None;last_usage=None
            def chat(self,messages,**kwargs):return ctx.llm.chat(messages,**kwargs)
        svc.llm=TracedLLMClient(Model())
        assert {'load_input','decide','reuse','clear','model','validate','repair','commit'}<=svc.graph.get_graph().nodes.keys()
        nid=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'A grounded preference')
        generate(ctx);drain(svc)
        record=store.list_traces(kind='summary')[0][1]
        assert record['status']=='ok'
        nodes=lambda data:[step['detail']['node'] for step in data['steps'] if step['name']=='graph.node']
        assert nodes(record)==['load_input','decide','model','validate','commit']
        ctx.repos.note.update(nid,importance=.7);before=ctx.llm.chat.call_count;drain(svc)
        record=store.list_traces(kind='summary')[0][1]
        assert nodes(record)==['load_input','decide','reuse','validate','commit'] and not record['llm_calls']
        ctx.repos.note.archive(nid);drain(svc)
        record=store.list_traces(kind='summary')[0][1]
        assert nodes(record)==['load_input','decide','clear','validate','commit'] and ctx.llm.chat.call_count==before
        ctx.repos.note.unarchive(nid)
        model=ctx.llm.chat.side_effect;seen=[]
        def correction(messages,**kwargs):
            seen.append(1)
            return '{bad json' if len(seen)==1 else model(messages[:2],**kwargs)
        ctx.llm.chat.side_effect=correction;drain(svc)
        record=store.list_traces(kind='summary')[0][1]
        assert nodes(record)==['load_input','decide','model','validate','repair','model','validate','commit']
        assert len(record['llm_calls'])==2 and record['status']=='ok'
    finally:ctx.conn.close();set_default_store(None)


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        for name,test in [('evolution',evolution),('atomic',atomic_and_retry),('batches',batches_and_reactivation),('migration',migration),
                          ('stale',stale_and_fenced),('api',api_scope),('note-job',note_job_evidence),('continuous',continuous_prompt_context),('graph',graph_branches)]:
            test(Path(tmp)/(name+'.db'))
    print('[OK] incremental evidence, continuous chat context, deletion retraction, atomic cursor, retries and migration')
