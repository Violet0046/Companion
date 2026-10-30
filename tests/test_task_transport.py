"""Exercise the installed SDK against a local SSE server, plus real task writers."""
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_durable_flow import context
from test_kind_storage import draft,proposal
from test_summary_evolution import empty,item,service
from companion_ever.llm.openai_client import OpenAICompatibleClient
from companion_ever.services.task_errors import can_retry,describe_error


def main():
    state={'mode':'slow','calls':[],'memory':0,'summary':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(request)
            assert request['stream'] is True
            mode=state['mode']
            result='{}'
            if mode=='tasks':
                payload=json.loads(request['messages'][1]['content'])
                if 'new_message_ids' in payload:
                    state['memory']+=1
                    source=next(m for m in payload['messages'] if m['role']=='user')
                    state['source_quote']=source['content']
                    quote=source['content'] if state['memory']>1 else 'invented quote'
                    result=json.dumps(draft([proposal(source['id'],quote,content='User always prefers tea.')]))
                else:
                    state['summary']+=1
                    assert all('evidence' not in f for f in payload['current_facts'])
                    assert payload['output_limits']['user_profile']['preferences']==8
                    doc=empty();claim=item(payload['current_facts'][0])
                    if state['summary']==1 or state.get('invalid_summary'):
                        claim['citations'][0]['quote']=state['source_quote']
                    doc['user_profile']['preferences']=[claim]*(9 if state['summary']==1 else 1)
                    result=json.dumps(doc)
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Connection','close')
            self.end_headers()
            chunks=[{'reasoning_content':'thinking'}]*4 if mode=='slow' else []
            chunks.append({'content':result})
            for delta in chunks:
                self.wfile.write(('data: '+json.dumps({'id':'mock','choices':[{'index':0,'delta':delta,'finish_reason':None}]})+'\n\n').encode())
                self.wfile.flush()
                if mode=='slow':
                    time.sleep(.06)
            if mode=='truncated':
                return
            finish='length' if mode=='length' else 'stop'
            self.wfile.write(('data: '+json.dumps({'id':'mock','choices':[{'index':0,'delta':{},'finish_reason':finish}]})+'\n\ndata: [DONE]\n\n').encode())
            self.wfile.flush()

    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=OpenAICompatibleClient('test-no-network',f'http://127.0.0.1:{server.server_port}/v1','test',timeout=.15)
    try:
        started=time.monotonic()
        assert client.chat([{'role':'user','content':'ping'}],stream=True,timeout=.15,total_timeout=2,max_retries=0)=='{}'
        assert time.monotonic()-started>.15
        client.model='MiniMax-M3'
        client.chat([],stream=True,timeout=2,total_timeout=2,max_retries=0,background_thinking=False)
        assert state['calls'][-1]['thinking']=={'type':'disabled'}
        client.chat([],stream=True,timeout=2,total_timeout=2,max_retries=0)
        assert 'thinking' not in state['calls'][-1],'ordinary chat must retain its configured defaults'
        client.model='MiniMax-M3.1-Flash'
        client.chat([],stream=True,timeout=2,total_timeout=2,max_retries=0,background_thinking=False)
        assert 'thinking' not in state['calls'][-1],'do not disable a model that requires adaptive thinking'
        client.model='test'
        for mode,expected,retryable in [('truncated',ConnectionError,True),('length',ValueError,False)]:
            state['mode']=mode
            try:
                client.chat([],stream=True,timeout=2,total_timeout=2,max_retries=0)
                raise AssertionError('incomplete output was accepted')
            except expected as exc:
                assert can_retry(exc)==retryable

        state['mode']='tasks'
        client.model='MiniMax-M3'
        with tempfile.TemporaryDirectory() as tmp:
            ctx=context(Path(tmp)/'tasks.db')
            try:
                ctx.services.extraction.llm_client=client
                ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=1,role='user',content='I always prefer tea.')
                result=ctx.services.extraction.run({'user_id':'alice','persona_id':'one'})
                assert result['job_status']=='succeeded',result['errors']
                assert result['facts_created']==1 and state['memory']==2
                assert '未通过校验' in state['calls'][-1]['messages'][-1]['content']
                assert 'message_id=1' in state['calls'][-1]['messages'][-1]['content']
                assert '缺失 message_id' in state['calls'][-1]['messages'][-1]['content'],'one correction receives all known evidence and coverage errors'
                assert state['calls'][-2]['thinking']=={'type':'disabled'}
                assert state['calls'][-1]['thinking']=={'type':'adaptive'},'only the single corrective call escalates thinking'
                svc=service(ctx);svc.llm=client
                request=svc.request('alice','one',background=False)
                result=svc.run(request['job_id'])
                assert result['status']=='succeeded',result['last_error']
                assert state['summary']==2 and svc.status('alice','one')['current']
                feedback=state['calls'][-1]['messages'][-1]['content']
                assert 'at most 8 items' in feedback and 'current_facts' in feedback,'capacity errors must not hide quote errors from the single correction'
                before=svc.repo.checkpoint('alice','one')['last_change_id']
                nid=ctx.conn.execute('SELECT id FROM note').fetchone()[0]
                ctx.repos.note.update(nid,content='User now prefers water.')
                state['invalid_summary']=True
                result=svc.run(svc.request('alice','one',background=False)['job_id'])
                assert result['status']=='failed' and result['retry_at'] is None
                assert svc.repo.checkpoint('alice','one')['last_change_id']==before
                attempts=state['summary'];svc.resume_pending();time.sleep(.1)
                assert state['summary']==attempts,'permanent validation failure must not call the model again'
            finally:
                ctx.conn.close()
        error=ConnectionError('Connection error.')
        error.__cause__=OSError(10061,'connection refused')
        assert '10061' in describe_error(error) and can_retry(error)
        assert not can_retry(ValueError('invalid evidence'))
    finally:
        server.shutdown();server.server_close();thread.join(2)
        client.client.close()
    print('[OK] SDK SSE idle timeout, truncation rejection, targeted correction and permanent failure stop')


if __name__=='__main__':
    main()
