"""Task traces preserve pre-call inputs, partial outputs and correction history."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from companion_ever.trace import TraceStore, trace_scope, current_trace, set_default_store
from companion_ever.llm.traced_client import TracedLLMClient
from companion_ever.services.structured_generation import checked_json


def main():
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);store=TraceStore(root/'traces')
        class Broken:
            model='fake';last_reasoning=None
            def stream_chat(self,messages,**kwargs):
                path,data=store.find(current_trace().trace_id)
                call=data['llm_calls'][0]
                assert call['status']=='running'
                assert 'source text' in store.artifact_path(path,call['request_artifact']).read_text(encoding='utf-8')
                yield 'partial raw output'
                # The first chunk has already been written while the call is running.
                path,data=store.find(current_trace().trace_id)
                assert data['llm_calls'][0]['response_state']=='streaming'
                assert store.artifact_path(path,'llm-01.response').read_text(encoding='utf-8')=='partial raw output'
                raise TimeoutError('no more frames')
        try:
            with trace_scope('memory_attempt','u','p',store=store,job_id='j',attempt_id='a') as tr:
                TracedLLMClient(Broken()).chat([{'role':'user','content':'source text'}],stream=True,
                    extra_headers={'Authorization':'Bearer must-not-be-saved','X-API-Key':'also-secret'})
            raise AssertionError('timeout swallowed')
        except TimeoutError:
            pass
        path,data=store.find(tr.trace_id)
        assert data['status']=='error' and data['llm_calls'][0]['response_state']=='partial'
        assert data['context']=={'job_id':'j','attempt_id':'a'}
        assert store.artifact_path(path,'response').read_text(encoding='utf-8')=='partial raw output'
        for name in ('request','llm-01.request','llm-01.payload'):
            text=store.artifact_path(path,name).read_text(encoding='utf-8')
            assert 'must-not-be-saved' not in text and 'also-secret' not in text

        class Correction:
            model='fake';last_reasoning=None;calls=0
            def chat(self,messages,**kwargs):
                self.calls+=1
                return '{bad json' if self.calls==1 else '{"accepted":true}'
        def validate(value):
            if not value:
                raise ValueError('not valid JSON')
            return value
        with trace_scope('extraction','u','p',store=store) as parent:
            with trace_scope('memory_attempt','u','p',store=store) as child:
                result=checked_json(TracedLLMClient(Correction()),[{'role':'user','content':'original source'}],
                                    validate,repair_attempts=1)
        assert result['accepted']
        path,data=store.find(child.trace_id)
        assert data['context']['parent_trace_id']==parent.trace_id
        assert len(data['llm_calls'])==2 and all(c['status']=='ok' for c in data['llm_calls'])
        assert store.artifact_path(path,'llm-01.response').read_text(encoding='utf-8')=='{bad json'
        assert 'not valid JSON' in store.artifact_path(path,'llm-02.request').read_text(encoding='utf-8')
        assert store.artifact_path(path,'validation-01.error').read_text(encoding='utf-8')=='not valid JSON'
        assert json.loads(store.artifact_path(path,'validation-02.parsed').read_text(encoding='utf-8'))['accepted']
        import contextlib,io
        from companion_ever.cli import main as cli_main
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            assert cli_main(['trace','--dir',str(store.base_dir),'show',child.trace_id,'--artifact','validation-01.error'])==0
        assert output.getvalue().strip()=='not valid JSON'

        # A process can die without leaving the context manager: the input and
        # latest partial output must still be on disk, with honest running status.
        code='''
import os,sys
from companion_ever.trace import TraceStore,trace_scope
from companion_ever.llm.traced_client import TracedLLMClient
class Killed:
    model='fake'
    def stream_chat(self,messages,**kwargs):
        yield 'saved before hard exit'
        os._exit(17)
with trace_scope('memory_attempt','u','p',store=TraceStore(sys.argv[1])):
    TracedLLMClient(Killed()).chat([{'role':'user','content':'source before hard exit'}],stream=True)
'''
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
        run=subprocess.run([sys.executable,'-c',code,str(root/'killed')],env=env,timeout=10,capture_output=True)
        assert run.returncode==17,run.stderr
        killed=TraceStore(root/'killed');path,data=killed.list_traces()[0]
        assert data['status']=='running'
        assert 'source before hard exit' in killed.artifact_path(path,'llm-01.request').read_text(encoding='utf-8')
        assert killed.artifact_path(path,'llm-01.response').read_text(encoding='utf-8')=='saved before hard exit'

        # Filesystem diagnostics cannot turn an otherwise successful call into failure.
        blocked=root/'not-a-directory';blocked.write_text('occupied')
        with trace_scope('test','u','p',store=TraceStore(blocked)):
            assert TracedLLMClient(Correction()).chat([])=='{bad json'
        old=os.environ.get('COMPANION_TRACE')
        try:
            os.environ['COMPANION_TRACE']='0'
            with trace_scope('test','u','p',store=TraceStore(root/'disabled')):
                assert current_trace() is None
            assert not (root/'disabled').exists()
        finally:
            if old is None: os.environ.pop('COMPANION_TRACE',None)
            else: os.environ['COMPANION_TRACE']=old

        # Actual services link attempts and trigger traces, including failures.
        from test_durable_flow import context
        from test_summary_evolution import service
        ctx=context(root/'tasks.db');set_default_store(store)
        try:
            ctx.services.extraction.llm_client=TracedLLMClient(Correction())
            ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=1,role='user',content='I prefer tea.')
            report=ctx.services.session_close.close_session('alice','one')
            assert report.extraction.job_status=='failed'
            path,trigger=store.list_traces(kind='extraction',user_id='alice')[0]
            assert trigger['status']=='error' and trigger['error']
            child=store.find(trigger['context']['attempt_trace_id'])[1]
            assert child['status']=='error' and child['context']['parent_trace_id']==trigger['trace_id']
            events=ctx.conn.execute("SELECT detail_json FROM task_event WHERE stage='trace'").fetchall()
            assert any(json.loads(row[0])['trace_id']==child['trace_id'] for row in events)
            # Looking up an already failed task must link its existing execution,
            # rather than suggesting a new model call took place.
            again=ctx.services.session_close.close_session('alice','one')
            assert again.extraction.job_id==report.extraction.job_id
            latest=store.list_traces(kind='extraction',user_id='alice')[0][1]
            assert latest['status']=='error' and latest['context']['attempt_trace_id']==child['trace_id']
            from companion_ever.domain.enums import MemoryKind
            from test_summary_evolution import empty,item
            ctx.services.note.create_note('alice','one',MemoryKind.FACT,'User prefers tea.')
            svc=service(ctx)
            class Summary:
                model='fake';last_reasoning=None
                def chat(self,messages,**kwargs):
                    payload=json.loads(messages[1]['content'])
                    doc=empty();doc['user_profile']['preferences']=[item(payload['current_facts'][0])]
                    return json.dumps(doc)
            svc.llm=TracedLLMClient(Summary())
            result=svc.run(svc.request('alice','one',background=False)['job_id'])
            assert result['status']=='succeeded',result['last_error']
            path,data=store.list_traces(kind='summary')[0]
            assert data['context']['job_id']==result['id'] and data['context']['attempt_id']==result['lease_token']
            assert data['status']=='ok' and store.artifact_path(path,'input_case').exists()
            assert store.artifact_path(path,'llm-01.response').exists()
        finally:
            ctx.conn.close();set_default_store(None)
    print('[OK] pre-call payloads, timeout partials, hard exits, correction history and task/trigger correlation')


if __name__=='__main__':
    main()
