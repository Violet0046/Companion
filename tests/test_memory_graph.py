"""Run real graph branches: scoped tools, reconciliation, budgets and stale writes."""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from test_durable_flow import context
from test_kind_storage import draft, proposal
from companion_ever.domain.enums import MemoryKind
from companion_ever.services.memory_search import MemorySearchResult
from companion_ever.services.memory_tools import MemoryTools, ToolRequest
from companion_ever.llm.traced_client import TracedLLMClient
from companion_ever.storage.note_link_repo import NoteLinkRepo
from companion_ever.trace import TraceStore, set_default_store


def request(name, **arguments):
    return {'tool_calls':[{'name':name,'arguments':arguments}]}


def message(ctx,text='I still like that tea.'):
    return ctx.services.conversation.log_turn(user_id='alice',persona_id='one',role='user',content=text,turn_index=1)


def miss_initial(svc):
    search=svc.retrieval.search
    return patch.object(svc.retrieval,'search',side_effect=lambda user,persona,query:
                        MemorySearchResult([],[],[],False) if query.recent_limit else search(user,persona,query))


def readonly_and_scope(path):
    ctx=context(path)
    try:
        svc=ctx.services.extraction
        ctx.services.persona.create_persona('alice','two','{}')
        own=ctx.services.note.create_note('alice','one',MemoryKind.FACT,'User likes oolong tea.')
        foreign=ctx.services.note.create_note('alice','two',MemoryKind.FACT,'private elsewhere needle')
        tools=MemoryTools(ctx.services.note,svc.retrieval,'alice','one')
        tables=('note','note_evidence','note_revision','note_link','fact_change')
        snapshot=lambda:{table:[tuple(row) for row in ctx.conn.execute('SELECT * FROM '+table)] for table in tables}
        before=snapshot()
        result,notes=tools.search_notes('oolong')
        assert own in notes and foreign not in notes
        result,notes=tools.get_note_context(own)
        assert result['note']['id']==own and own in notes
        for nid in (foreign,'missing'):
            result,notes=tools.get_note_context(nid)
            assert result['error']=='not_found' and not notes
            assert 'private elsewhere' not in json.dumps(result)
        result,notes=tools.search_notes('elsewhere needle')
        assert foreign not in notes and 'private elsewhere needle' not in json.dumps(result)
        for bad in (request('delete_note',note_id=own),request('search_notes',query='tea',user_id='other'),
                    request('search_notes',query='tea',limit=True),request('search_notes',query='tea',kinds=[])):
            try:
                ToolRequest.model_validate(bad)
                raise AssertionError('unrecognized or unscoped tool arguments accepted')
            except ValueError:
                pass
        assert snapshot()==before,'read tools must not change notes, access metadata or the fact journal'
    finally:
        ctx.conn.close()


def active_reconciliation(path):
    ctx=context(path);store=TraceStore(path.parent/'traces');set_default_store(store)
    try:
        svc=ctx.services.extraction
        assert ctx.services.session_close.extraction_service is svc and ctx.workflows.extraction is svc.graph
        assert {'load_input','prefetch','model','validate','tools','repair','commit'} <= svc.graph.get_graph().nodes.keys()
        old_mid=message(ctx,'I have always liked oolong tea.')
        old=ctx.repos.note.create(user_id='alice',persona_id='one',kind=MemoryKind.FACT,
                                  content='User likes oolong tea.',reality='user_report')
        episode=ctx.repos.note.create(user_id='alice',persona_id='one',kind=MemoryKind.EPISODE,
                                      content='Previous tea tasting.',reality='user_report')
        ctx.conn.execute('INSERT INTO note_evidence(note_id,message_id,quote) VALUES (?,?,?)',(old,old_mid,'oolong tea'))
        NoteLinkRepo(ctx.conn).create(episode,old,'supports','alice','one',reason='earlier confirmation',job_id=None)
        ctx.conn.execute('UPDATE conversation SET memory_start_message_id=?',(old_mid,))
        text="I still like that tea; last week's tasting confirmed it."
        mid=message(ctx,text);seen=[]
        class Model:
            model='fake';last_reasoning=None
            def chat(self,messages,**kwargs):
                payload=json.loads(messages[-1]['content']);seen.append(payload)
                if len(seen)==1:
                    assert not payload['existing_notes']
                    return json.dumps(request('search_notes',query='oolong',kinds=['fact'],limit=2))
                if len(seen)==2:
                    result=payload['tool_results'][0]['result']
                    assert result['notes'][0]['id']==old
                    return json.dumps(request('get_note_context',note_id=old))
                result=payload['tool_results'][0]['result']
                assert result['evidence'][0]['message_id']==old_mid and result['relations']
                return json.dumps(draft([
                    proposal(mid,text,action='confirm',target_id=old,content='User likes oolong tea.'),
                    proposal(mid,text,id='p2',kind='episode',content='Last week, user confirmed the preference at a tea tasting.')],
                    [{'from_id':'p2','to_id':'p1','relation':'supports','reason':'explicit user confirmation'}]))
        svc.llm_client=TracedLLMClient(Model())
        with miss_initial(svc):
            out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='succeeded',out['errors']
        assert out['model_calls']==3 and out['tool_rounds']==2 and out['tool_calls']==2
        assert out['facts_created']==1 and out['note_ids']['p1']==old
        assert ctx.conn.execute("SELECT count(*) FROM note WHERE kind='fact'").fetchone()[0]==1
        assert ctx.conn.execute('SELECT count(*) FROM note_evidence WHERE note_id=?',(old,)).fetchone()[0]==2
        assert ctx.conn.execute("SELECT count(*) FROM note_link WHERE to_id=? AND link_type='supports'",(old,)).fetchone()[0]==2
        meta,data=store.list_traces(kind='memory_attempt')[0]
        assert len(data['llm_calls'])==3
        names=[step['detail']['node'] for step in data['steps'] if step['name']=='graph.node']
        assert names==['load_input','prefetch','model','validate','tools','model','validate','tools','model','validate','commit'],names
        assert all(store.artifact_path(meta,name) for name in ('tool-01.request','tool-01.result','tool-02.result','input_case.final'))
        final=json.loads(store.artifact_path(meta,'input_case.final').read_text(encoding='utf-8'))
        assert old in {note['id'] for note in final['existing_notes']}
    finally:
        ctx.conn.close();set_default_store(None)


def loop_limits(path):
    ctx=context(path)
    try:
        mid=message(ctx);svc=ctx.services.extraction
        ctx.llm.chat.return_value=json.dumps(request('search_notes',query='unknown'))
        with patch.object(MemoryTools,'execute',wraps=MemoryTools(ctx.services.note,svc.retrieval,'alice','one').execute) as calls:
            out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='failed' and '预算' in out['errors'][0]
        assert ctx.llm.chat.call_count==3 and calls.call_count==2
        assert ctx.conn.execute('SELECT count(*) FROM note').fetchone()[0]==0
        job=svc.jobs.get(out['job_id'])
        assert job['through_message_id']==mid and job['retry_at'] is None
        svc.jobs.retry(job['id']);ctx.llm.chat.reset_mock()
        ctx.llm.chat.return_value=json.dumps({'tool_calls':[request('get_note_context',note_id='missing')['tool_calls'][0]]*4})
        with patch.object(MemoryTools,'execute',wraps=MemoryTools(ctx.services.note,svc.retrieval,'alice','one').execute) as calls:
            out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='failed' and calls.call_count==4 and ctx.llm.chat.call_count==2
    finally:
        ctx.conn.close()


def repair_and_stale(path):
    ctx=context(path)
    try:
        mid=message(ctx);svc=ctx.services.extraction
        skip={'proposals':[],'links':[],'uncertainties':[],'skipped':[{'message_id':mid,'reason':'no lasting information'}]}
        ctx.llm.chat.side_effect=[json.dumps(request('delete_note',note_id='missing')),json.dumps(skip)]
        out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='succeeded' and out['model_calls']==2
        assert ctx.llm.chat.call_args.kwargs['background_thinking'] is True
        old=ctx.repos.note.create(user_id='alice',persona_id='one',kind=MemoryKind.FACT,
                                  content='User likes oolong.',reality='user_report')
        mid=message(ctx,'I still like oolong.')
        calls=[]
        def model(messages,**kwargs):
            calls.append(1)
            if len(calls)==1:return json.dumps(request('search_notes',query='oolong',kinds=['fact']))
            ctx.repos.note.update(old,content='Updated by another writer.')
            return json.dumps(draft([proposal(mid,'I still like oolong.',action='confirm',target_id=old,content='User likes oolong.')]))
        ctx.llm.chat.side_effect=model
        with miss_initial(svc):out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='failed' and '候选记忆已改变' in out['errors'][0],out
        assert svc.jobs.get(out['job_id'])['retry_at'] is not None
        assert ctx.conn.execute('SELECT count(*) FROM note_evidence').fetchone()[0]==0
    finally:
        ctx.conn.close()


def total_deadline(path):
    ctx=context(path)
    try:
        message(ctx);svc=ctx.services.extraction;svc.timeout=.01
        def delayed(*args,**kwargs):
            time.sleep(.05)
            return json.dumps(request('search_notes',query='tea'))
        ctx.llm.chat.side_effect=delayed
        with patch.object(MemoryTools,'execute') as tool:
            out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='failed' and svc.jobs.get(out['job_id'])['retry_at'] is not None
        assert not tool.called and not out['facts_created']
    finally:
        ctx.conn.close()


def missing_candidate_guard(path):
    ctx=context(path)
    try:
        svc=ctx.services.extraction
        old=ctx.repos.note.create(user_id='alice',persona_id='one',kind=MemoryKind.FACT,
                                  content='User likes oolong.',reality='user_report')
        mid=message(ctx,'I still like oolong.')
        duplicate=draft([proposal(mid,'I still like oolong.')])
        ctx.llm.chat.return_value=json.dumps(duplicate)
        with miss_initial(svc):out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='failed' and 'search_notes' in out['errors'][0]
        assert out['model_calls']==2 and ctx.conn.execute('SELECT count(*) FROM note').fetchone()[0]==1
        assert not ctx.conn.execute('SELECT * FROM note_evidence').fetchall()
        svc.jobs.retry(out['job_id']);ctx.llm.chat.reset_mock()
        ctx.llm.chat.side_effect=[json.dumps(duplicate),json.dumps(request('search_notes',query='oolong')),
            json.dumps(draft([proposal(mid,'I still like oolong.',action='confirm',target_id=old,content='User likes oolong.')]))]
        with miss_initial(svc):out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='succeeded' and out['model_calls']==3 and out['tool_calls']==1,out
        assert out['facts_created']==0 and out['note_ids']['p1']==old
    finally:
        ctx.conn.close()


def oversized_context(path):
    ctx=context(path)
    try:
        svc=ctx.services.extraction;svc.MAX_TOOL_RESULT_CHARS=100
        old=ctx.repos.note.create(user_id='alice',persona_id='one',kind=MemoryKind.FACT,
                                  content='User likes oolong.'*100,reality='user_report')
        mid=message(ctx);seen=[]
        def model(messages,**kwargs):
            payload=json.loads(messages[-1]['content']);seen.append(payload)
            if len(seen)==1:return json.dumps(request('get_note_context',note_id=old))
            assert payload['tool_results'][0]['result']['error']=='result_budget_exceeded'
            return json.dumps({'proposals':[],'links':[],'uncertainties':[],
                               'skipped':[{'message_id':mid,'reason':'cannot resolve the reference'}]})
        ctx.llm.chat.side_effect=model
        with miss_initial(svc):out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='succeeded' and out['tool_calls']==1 and not out['facts_created']
        assert not ctx.conn.execute('SELECT * FROM note_evidence').fetchall()
    finally:
        ctx.conn.close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        for name,test in [('readonly',readonly_and_scope),('reconcile',active_reconciliation),('limits',loop_limits),
                          ('repair',repair_and_stale),('deadline',total_deadline),
                          ('required-search',missing_candidate_guard),('oversized',oversized_context)]:
            test(Path(tmp)/(name+'.db'))
    print('[OK] actual graph nodes, scoped read tools, active dedup/links, traces, bounded loops and stale-write fencing')
