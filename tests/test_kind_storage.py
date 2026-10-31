"""Real schema migration and production draft writes, using only temporary databases."""
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from test_durable_flow import context
from companion_ever.infrastructure.database.connection import init_db
from companion_ever.domain.enums import MemoryKind
from companion_ever.memory.retrieval.query import MemorySearch


def proposal(mid, quote, **changes):
    return {'id':'p1','kind':'fact','action':'create','target_id':None,'content':quote,
            'event_time':None,'time_basis':'用户未说明起始时间','reality':'user_report',
            'evidence':[{'message_id':mid,'quote':quote}],'reason':'用户明确说明',**changes}


def draft(proposals, links=None):
    return {'proposals':proposals,'links':links or [],'skipped':[],'uncertainties':[]}


def extract(ctx, text, build):
    conv=ctx.services.conversation
    mid=conv.log_turn(user_id='alice',persona_id='one',role='user',content=text,
                      turn_index=conv.get_max_turn_index('alice','one')+1)
    def respond(messages, **kwargs):
        payload=json.loads(messages[-1]['content'])
        return json.dumps(build(mid,payload),ensure_ascii=False)
    ctx.llm.chat.side_effect=respond
    return ctx.services.extraction.run({'user_id':'alice','persona_id':'one'})


def production(path):
    ctx=context(path)
    try:
        assert 'type' not in {r['name'] for r in ctx.conn.execute('PRAGMA table_info(note)')}
        assert not ctx.repos.note.find(user_id='alice',persona_id='one')
        assert ctx.services.persona.get_definition('alice','one')['content']=='{}'
        out=extract(ctx,'我住在杭州',lambda mid,_:draft([proposal(mid,'我住在杭州')]))
        assert out['job_status']=='succeeded',out
        first=out['note_ids']['p1']
        original=ctx.repos.note.get(first)
        ctx.services.note.set_pinned('alice','one',first,True)
        out=extract(ctx,'我住在杭州',lambda mid,_:draft([proposal(mid,'我住在杭州',action='confirm',target_id=first,importance=.1)]))
        assert out['job_status']=='succeeded' and out['facts_created']==0,out
        assert ctx.repos.note.get(first).importance==original.importance
        assert ctx.conn.execute('SELECT count(*) FROM note_evidence WHERE note_id=?',(first,)).fetchone()[0]==2
        # Even a create proposal goes through the production writer's exact
        # fact deduplication, preserving identity/importance and adding evidence.
        out=extract(ctx,'我住在杭州',lambda mid,_:draft([proposal(mid,'我住在杭州',importance=.1)]))
        assert out['job_status']=='succeeded' and out['facts_created']==0 and out['note_ids']['p1']==first,out
        assert ctx.repos.note.get(first).importance==original.importance
        assert ctx.conn.execute('SELECT count(*) FROM note_evidence WHERE note_id=?',(first,)).fetchone()[0]==3
        out=extract(ctx,'更正，我现在住在苏州',lambda mid,_:draft([proposal(mid,'更正，我现在住在苏州',action='revise',target_id=first)]))
        assert out['job_status']=='succeeded',out
        latest=out['note_ids']['p1']
        assert latest!=first and ctx.repos.note.get(first).content=='我住在杭州'
        assert ctx.repos.note.get(latest).pinned
        fixed=ctx.services.prompt.build('alice','one',query='早上好')['system_prompt']
        assert '更正，我现在住在苏州' in fixed and '我住在杭州' not in fixed
        edge=ctx.conn.execute("SELECT * FROM note_link WHERE link_type='supersedes'").fetchone()
        assert (edge['from_id'],edge['to_id'],edge['job_id'])==(latest,first,out['job_id'])
        hits=ctx.services.retrieval.search('alice','one',MemorySearch(query='杭州',max_hops=2)).notes
        assert latest in {n.id for n in hits} and first not in {n.id for n in hits}

        out=extract(ctx,'明天上午十一点面试',lambda mid,_:draft([proposal(mid,'明天上午十一点面试',kind='episode')]))
        event=out['note_ids']['p1']
        out=extract(ctx,'同一场面试改到下午三点',lambda mid,_:draft([proposal(mid,'同一场面试改到下午三点',kind='episode',action='append',target_id=event,content='同一场面试从上午十一点改到下午三点')]))
        assert out['job_status']=='succeeded' and out['facts_created']==0,out
        assert ctx.repos.note.get(event).revision==2
        assert ctx.conn.execute('SELECT content FROM note_revision WHERE note_id=?',(event,)).fetchone()[0]=='明天上午十一点面试'

        # A database failure after link insertion must undo notes, evidence, edge AND success.
        before=tuple(ctx.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('note','note_evidence','note_link','fact_change'))
        original_succeed=ctx.services.extraction.jobs.succeed
        def fail_after_success(jid,token):
            original_succeed(jid,token)
            raise RuntimeError('injected failure after marking succeeded')
        with patch.object(ctx.services.extraction.jobs,'succeed',side_effect=fail_after_success):
            out=extract(ctx,'这次试饮让我确认喜欢茶',lambda mid,_:draft([
                proposal(mid,'这次试饮让我确认喜欢茶',kind='episode'),
                proposal(mid,'这次试饮让我确认喜欢茶',id='p2',content='用户明确表示喜欢茶')],
                [{'from_id':'p1','to_id':'p2','relation':'supports','reason':'用户明确确认'}]))
        assert out['job_status']=='failed' and ctx.services.extraction.jobs.get(out['job_id'])['status']=='failed',out
        after=tuple(ctx.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('note','note_evidence','note_link','fact_change'))
        assert before==after
        ctx.services.extraction.jobs.retry(out['job_id'])
        retried=ctx.services.extraction.run({'user_id':'alice','persona_id':'one'})
        assert retried['job_status']=='succeeded',retried
        assert ctx.conn.execute("SELECT count(*) FROM note_link WHERE link_type='supports'").fetchone()[0]==1
        calls=ctx.llm.chat.call_count
        assert ctx.services.extraction.run({'user_id':'alice','persona_id':'one'})['facts_created']==0
        assert ctx.llm.chat.call_count==calls

        # Mutating a candidate while the model runs invalidates its snapshot.
        def stale(mid,payload):
            ctx.repos.note.update(latest,content='用户另行手动修订的居住地')
            return draft([proposal(mid,'我再次确认住在苏州',action='confirm',target_id=latest,content='更正，我现在住在苏州')])
        out=extract(ctx,'我再次确认住在苏州',stale)
        assert out['job_status']=='failed' and '候选记忆已改变' in out['errors'][0],out

        from fastapi.testclient import TestClient
        from companion_ever.entrypoints.web.server import create_app
        from companion_ever.config import WebConfig
        with TestClient(create_app(ctx,WebConfig(pin='test-pin'))) as client:
            base='/api/persona/alice/one/notes'
            assert client.get(base).status_code==401
            headers={'x-pin':'test-pin'}
            items=client.get(base+'?kind=fact',headers=headers).json()
            assert all('type' not in n and n['kind']=='fact' for n in items)
            assert client.get(base+'?type=preference',headers=headers).status_code==400
            assert client.get(base+'?kind=preference',headers=headers).status_code==400
            assert client.patch(base+f'/{latest}/pin',headers=headers,json={'pinned':False}).json()['pinned']==False
            ctx.services.persona.create_persona('alice','two','{}')
            assert client.patch(f'/api/persona/alice/two/notes/{latest}/pin',headers=headers,json={'pinned':True}).status_code==404
    finally:
        ctx.conn.close()


def migration(path):
    conn=sqlite3.connect(path)
    conn.executescript((Path(__file__).parent/'fixtures/legacy_schema_v0.sql').read_text(encoding='utf-8'))
    conn.execute("INSERT INTO user(id,persona_ids) VALUES ('alice','one')")
    for nid,typ,content in [('config','persona_basics','{"name":"test"}'),('f','preference','用户喜欢茶'),('e','event','用户参加试饮')]:
        conn.execute('INSERT INTO note(id,user_id,persona_id,type,content) VALUES (?,\'alice\',\'one\',?,?)',(nid,typ,content))
    conn.execute("INSERT INTO note_link(id,from_id,to_id,link_type) VALUES ('edge','e','f','supports')")
    conn.execute("INSERT INTO conversation_log(user_id,persona_id,turn_index,role,content) VALUES ('alice','one',1,'user','我喜欢茶')")
    conn.commit(); conn.close()
    conn=init_db(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==6
        assert 'type' not in {r['name'] for r in conn.execute('PRAGMA table_info(note)')}
        assert [tuple(r) for r in conn.execute('SELECT id,kind FROM note ORDER BY id')]==[('e','episode'),('f','fact')]
        assert conn.execute('SELECT id FROM persona_definition').fetchone()[0]=='config'
        assert conn.execute('SELECT id FROM note_link').fetchone()[0]=='edge'
        assert conn.execute("SELECT note_id FROM note_fts WHERE note_fts MATCH '喜欢茶'").fetchone()[0]=='f'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:
        conn.close()
    conn=init_db(path); conn.close()
    assert len(list(path.parent.glob(path.name+'.bak-before-kind-*')))==1


def rejected_migration(path):
    # An invalid historical edge must cause a recoverable rollback, not silent deletion.
    conn=sqlite3.connect(path)
    conn.executescript((Path(__file__).parent/'fixtures/legacy_schema_v0.sql').read_text(encoding='utf-8'))
    conn.execute("INSERT INTO user(id) VALUES ('alice')")
    for nid in ('a','b'):
        conn.execute("INSERT INTO note(id,user_id,persona_id,type,content) VALUES (?,'alice','one','preference','text')",(nid,))
    conn.execute("INSERT INTO note_link(id,from_id,to_id,link_type) VALUES ('bad','a','b','supports')")
    conn.commit();conn.close()
    try:
        init_db(path)
    except ValueError:
        pass
    else:
        raise AssertionError('invalid legacy relation was silently migrated')
    conn=sqlite3.connect(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0]==1
    assert 'type' in {r[1] for r in conn.execute('PRAGMA table_info(note)')}
    assert conn.execute('SELECT id FROM note_link').fetchone()[0]=='bad'
    conn.close()


def relation_guards(path):
    from companion_ever.memory.notes.links import NoteLinkRepo
    ctx=context(path)
    try:
        ctx.services.persona.create_persona('alice','two','{}')
        notes=ctx.services.note
        episode=notes.create_note('alice','one',MemoryKind.EPISODE,'同一经历')
        later=notes.create_note('alice','one',MemoryKind.EPISODE,'新的进展')
        fact=notes.create_note('alice','one',MemoryKind.FACT,'现实事实')
        foreign=notes.create_note('alice','two',MemoryKind.FACT,'其他角色私有事实')
        fiction=notes.create_note('alice','one',MemoryKind.EPISODE,'共同想象')
        ctx.conn.execute("UPDATE note SET reality='shared_fiction' WHERE id=?",(fiction,))
        repo=NoteLinkRepo(ctx.conn)
        for a,b,kind in [(episode,foreign,'supports'),(fact,episode,'supports'),
                         (fiction,fact,'supports'),(episode,episode,'continues'),(episode,fact,'contradicts')]:
            try:
                repo.create(a,b,kind,'alice','one',reason='测试依据',job_id=None)
            except ValueError:
                pass
            else:
                raise AssertionError('invalid relation persisted')
        assert ctx.conn.execute('SELECT count(*) FROM note_link').fetchone()[0]==0
        repo.create(later,episode,'continues','alice','one',reason='明确后续',job_id=None)
        try:
            repo.create(episode,later,'continues','alice','one',reason='错误环路',job_id=None)
        except ValueError:
            pass
        else:
            raise AssertionError('cycle persisted')
        # Even a deliberately corrupt raw edge cannot cross reality scopes at retrieval time.
        ctx.conn.execute("INSERT INTO note_link(id,from_id,to_id,link_type) VALUES ('corrupt',?,?,'supports')",(fiction,fact))
        result=ctx.services.retrieval.search('alice','one',MemorySearch(seed_ids=[fact],max_hops=1))
        assert fiction not in {n.id for n in result.notes}
    finally:
        ctx.conn.close()


def fenced_write(path):
    ctx=context(path)
    try:
        def take_over(mid,payload):
            ctx.conn.execute("UPDATE memory_job SET lease_token='other-owner' WHERE id=?",(payload['id'],))
            return draft([proposal(mid,'明确的测试事实')])
        out=extract(ctx,'明确的测试事实',take_over)
        assert out['job_status']=='running'
        assert not ctx.repos.note.find(user_id='alice',persona_id='one')
        job=ctx.services.extraction.jobs.get(out['job_id'])
        assert job['status']=='running' and job['lease_token']=='other-owner'
    finally:
        ctx.conn.close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        migration(Path(tmp)/'legacy.db')
        production(Path(tmp)/'production.db')
        rejected_migration(Path(tmp)/'rejected.db')
        relation_guards(Path(tmp)/'relations.db')
        fenced_write(Path(tmp)/'fenced.db')
    print('[OK] type removed, preserved migration, atomic notes/evidence/links/jobs and stale snapshots')
