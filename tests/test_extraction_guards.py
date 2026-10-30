"""Evidence, atomicity, bounded batches and retry regressions without model calls."""
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock

from test_durable_flow import context
from companion_ever.config import AppConfig
from companion_ever.domain.enums import BlockType
from companion_ever.llm.openai_client import OpenAICompatibleClient


def evidence(path):
    from test_kind_storage import draft, proposal
    ctx = context(path)
    try:
        conv,svc=ctx.services.conversation,ctx.services.extraction
        mid=conv.log_turn(user_id='alice',persona_id='one',turn_index=1,role='user',content='我喜欢茶，也喜欢音乐')
        aid=conv.log_turn(user_id='alice',persona_id='one',turn_index=1,role='assistant',content='我喜欢茶，也喜欢音乐')
        ctx.services.persona.create_persona('alice','two','{}')
        foreign=conv.log_turn(user_id='alice',persona_id='two',turn_index=1,role='user',content='我喜欢茶，也喜欢音乐')
        job=svc.jobs.enqueue('alice','one')
        late=conv.log_turn(user_id='alice',persona_id='one',turn_index=2,role='user',content='我喜欢茶，也喜欢音乐')
        valid=proposal(mid,'我喜欢茶',content='用户喜欢茶')
        citations=[[],[{'message_id':mid,'quote':''}],[{'message_id':mid,'quote':' '}],
                   [{'message_id':mid,'quote':'不存在的原话'}]]
        citations += [[{'message_id':i,'quote':'我喜欢茶'}] for i in (aid,foreign,late,True,str(mid))]
        for evidence in citations:
            svc.jobs.retry(job['id'])
            ctx.llm.chat.return_value=json.dumps(draft([valid,{**valid,'id':'p2','evidence':evidence}]))
            out=svc.run({'user_id':'alice','persona_id':'one'})
            assert out['job_status']=='failed' and out['facts_created']==0,out
            assert ctx.conn.execute('SELECT count(*) FROM note').fetchone()[0]==0
            assert ctx.conn.execute('SELECT count(*) FROM note_evidence').fetchone()[0]==0
        for proposals in ([valid,{**valid,'id':'p2'}],[{**valid,'id':f'p{i}','content':f'不同事实{i}'} for i in range(5)]):
            svc.jobs.retry(job['id'])
            ctx.llm.chat.return_value=json.dumps(draft(proposals))
            out=svc.run({'user_id':'alice','persona_id':'one'})
            assert out['job_status']=='failed' and not out['facts_created'],out
        svc.jobs.retry(job['id'])
        ctx.llm.chat.return_value=json.dumps(draft([valid]))
        out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='succeeded' and out['facts_created']==1,out
        nid=out['note_ids']['p1']
        ctx.llm.chat.return_value=json.dumps(draft([proposal(late,'我喜欢茶',content='用户喜欢茶',importance=.1)]))
        out=svc.run({'user_id':'alice','persona_id':'one'})
        assert out['job_status']=='succeeded' and out['facts_created']==0,out
        assert ctx.conn.execute('SELECT count(*) FROM note_evidence WHERE note_id=?',(nid,)).fetchone()[0]==2
        assert ctx.repos.note.get(nid).importance==.5
        options=ctx.llm.chat.call_args.kwargs
        assert options['timeout']==120.0 and options['max_retries']==0 and options['stream'] is True
        assert 0<options['total_timeout']<=360.0
    finally:
        ctx.conn.close()


def batching(path):
    ctx = context(path)
    finished = threading.Event()
    try:
        conv = ctx.services.conversation
        for i in range(85):
            conv.log_turn(user_id='alice', persona_id='one', turn_index=i, role='user', content=f'消息{i}')
        from fake_memory import no_memory
        ctx.llm.chat.side_effect = no_memory
        svc = ctx.services.session_close
        original = ctx.conn.close_current
        def close_current():
            original()
            if threading.current_thread().name == 'memory-job':
                finished.set()
        ctx.conn.close_current = close_current
        assert svc.close_session_background('alice', 'one')
        assert finished.wait(10), 'backlog worker did not finish'
        rows = ctx.conn.execute('SELECT * FROM memory_job ORDER BY through_message_id').fetchall()
        assert len(rows) == 8 and all(r['status'] == 'succeeded' for r in rows)
        assert [r['through_message_id'] for r in rows] == [12,24,36,48,60,72,84,85]
        assert [r['after_message_id'] for r in rows] == [0,12,24,36,48,60,72,84]
        assert ctx.llm.chat.call_count == 8

        for i in range(3):
            conv.log_turn(user_id='alice', persona_id='one', turn_index=100+i, role='user', content='长' * 9000)
        job = svc.jobs.enqueue('alice', 'one')
        msgs = conv.get_messages_between(user_id='alice', persona_id='one', since_id=job['after_message_id'], until_id=job['through_message_id'])
        assert len(msgs) == 1 and len(msgs[0].content) == 9000
    finally:
        finished.wait(1)
        ctx.conn.close()


def timeout_options():
    cfg = AppConfig.from_env({'COMPANION_EXTRACTION_TIMEOUT': '180'}, dotenv_path=None)
    assert cfg.extraction_timeout == 180
    client = OpenAICompatibleClient('fake', 'http://unused', 'fake')
    client._client = MagicMock()
    client._client.with_options.return_value.chat.completions.create.return_value.choices[0].message.content = '{}'
    assert client.chat([], timeout=180, max_retries=0) == '{}'
    client._client.with_options.assert_called_once_with(max_retries=0)
    assert client._client.with_options.return_value.chat.completions.create.call_args.kwargs['timeout'] == 180
    assert 'max_retries' not in client._client.with_options.return_value.chat.completions.create.call_args.kwargs


def manual_retry(path):
    from fastapi.testclient import TestClient
    from companion_ever.api.server import create_app
    from companion_ever.config import WebConfig
    ctx = context(path)
    finished = threading.Event()
    try:
        svc = ctx.services.session_close
        ctx.services.conversation.log_turn(user_id='alice', persona_id='one', turn_index=1,
                                           role='user', content='重新整理这批消息')
        job = svc.jobs.enqueue('alice', 'one')
        ctx.conn.execute("UPDATE memory_job SET status='failed',attempt_count=3,last_error='timeout',retry_at=NULL WHERE id=?", (job['id'],))
        from fake_memory import no_memory
        ctx.llm.chat.side_effect = no_memory
        original = ctx.conn.close_current
        def close_current():
            original()
            if threading.current_thread().name == 'memory-job':
                finished.set()
        ctx.conn.close_current = close_current
        with TestClient(create_app(ctx, WebConfig())) as client:
            response = client.post('/api/session/close', json={'user_id': 'alice', 'persona_id': 'one'})
            assert response.status_code == 200 and response.json()['started']
            assert finished.wait(5)
            retried = svc.jobs.get(job['id'])
            assert retried['status'] == 'succeeded' and retried['attempt_count'] == 1
            assert retried['through_message_id'] == job['through_message_id']
            assert retried['after_message_id'] == job['after_message_id']
    finally:
        ctx.conn.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as tmp:
        evidence(Path(tmp) / 'evidence.db')
        batching(Path(tmp) / 'batching.db')
        manual_retry(Path(tmp) / 'retry.db')
    timeout_options()
    print('[OK] evidence guards, rollback, dedup evidence, batching and timeout options')
