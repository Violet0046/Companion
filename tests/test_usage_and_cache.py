"""Actual usage, unknown cache counters and explicit retrieval context only."""
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_durable_flow import context
from companion_ever.infrastructure.llm.openai_client import OpenAICompatibleClient
from companion_ever.infrastructure.llm.traced_client import TracedLLMClient,usage_metrics,summarize_calls
from companion_ever.infrastructure.tracing import TraceStore,trace_scope
from companion_ever.domain.enums import MemoryKind
from companion_ever.memory.retrieval.query import MemorySearch


def counters(root):
    usage=NS(prompt_tokens=2000,completion_tokens=100,prompt_tokens_details=NS(cached_tokens=1500),
             completion_tokens_details=NS(reasoning_tokens=32))
    class Stream:
        def __iter__(self):
            yield NS(choices=[NS(delta=NS(content='hello',reasoning_content=None),finish_reason=None)],usage=None)
            yield NS(choices=[NS(delta=NS(content=None,reasoning_content=None),finish_reason='stop')],usage=None)
            yield NS(choices=[],usage=usage)
        def close(self):pass
    class SDK:
        def __init__(self):self.calls=[];self.chat=NS(completions=NS(create=self.create))
        def create(self,**kwargs):self.calls.append(kwargs);return Stream()
    sdk=SDK();client=OpenAICompatibleClient('test','http://127.0.0.1:9','test');client._client=sdk
    store=TraceStore(root/'traces')
    with trace_scope('chat','u','p',store=store) as trace:
        assert ''.join(TracedLLMClient(client).stream_chat([{'role':'user','content':'test'}]))=='hello'
    assert sdk.calls[0]['stream_options']=={'include_usage':True}
    data=store.find(trace.trace_id)[1];call=data['llm_calls'][0]
    assert call['usage']=={'input_tokens':2000,'output_tokens':100,'cached_input_tokens':1500,
        'uncached_input_tokens':500,'reasoning_tokens':32,'source':'provider'}
    assert call['elapsed_ms']>=0 and summarize_calls([call])['cache_hit_ratio']==.75
    unknown=usage_metrics({'prompt_tokens':20,'completion_tokens':5})
    assert unknown['cached_input_tokens'] is None
    totals=summarize_calls([call,{'usage':unknown}])
    assert totals['input_tokens']==2020 and totals['cache_hit_ratio'] is None
    assert totals['cached_input_tokens_reported_calls']==1
    assert usage_metrics({'prompt_tokens':20,'prompt_tokens_details':{'cached_tokens':21}})['cached_input_tokens'] is None
    client.stream_usage=False
    assert client.request_description([],{'stream':True})['body'].get('stream_options') is None
    assert ''.join(client.stream_chat([]))=='hello' and 'stream_options' not in sdk.calls[-1]
    original=Path.replace;attempts=[]
    def busy_once(path,target):
        attempts.append(1)
        if len(attempts)==1:raise PermissionError('temporary Windows reader')
        return original(path,target)
    with patch.object(Path,'replace',busy_once):
        TraceStore._atomic_write(root/'atomic.txt','complete')
    assert (root/'atomic.txt').read_text(encoding='utf-8')=='complete' and len(attempts)==2
    assert not list(root.glob('*.tmp'))


def explicit_context_only(root):
    ctx=context(root/'prompt.db')
    try:
        conv=ctx.services.conversation
        nid=ctx.services.note.create_note('alice','one',MemoryKind.EPISODE,'青禾科技面试后收到录用意向。')
        conv.log_turn(user_id='alice',persona_id='one',role='user',content='刚才说到青禾科技的面试。',turn_index=5)
        follow=ctx.services.prompt.build('alice','one','后来呢')
        assert '青禾科技面试后收到录用意向' not in follow['system_prompt']
        assert any('刚才说到青禾科技' in m['content'] for m in follow['messages'])
        explicit=ctx.services.retrieval.search('alice','one',MemorySearch(query='后来呢',context_query='青禾科技面试'))
        assert nid in {note.id for note in explicit.notes}
    finally:ctx.conn.close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        counters(Path(tmp));explicit_context_only(Path(tmp))
    print('[OK] actual usage/cache coverage, stream opt-out and no implicit topic guessing')
