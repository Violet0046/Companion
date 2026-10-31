"""给任意 LLM 客户端套一层 trace 记录。"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from companion_ever.infrastructure.tracing import current_trace
from companion_ever.runtime.errors import describe_error


def diagnostic_options(options):
    """Keep model parameters, but never persist credential-bearing options."""
    if isinstance(options, dict):
        return {key: '[隐藏]' if key.lower().replace('-', '_') in
                {'authorization', 'api_key', 'x_api_key', 'cookie', 'proxy_authorization', 'access_token'}
                else diagnostic_options(value) for key, value in options.items()}
    if isinstance(options, (list, tuple)):
        return [diagnostic_options(value) for value in options]
    return options


def render_request(messages: list[dict], model: str, kwargs: dict) -> str:
    """把实际发给 LLM 的 messages 渲染成可读文本。"""
    lines = [
        "===== 实际发给 LLM 的请求 =====",
        f"# model  : {model}",
        f"# call options (含客户端预算；协议字段见 payload): {json.dumps(kwargs, ensure_ascii=False, default=str)}",
        f"# msgs   : {len(messages)} 条",
        "",
    ]
    for i, m in enumerate(messages, 1):
        role = m.get("role", "?")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        lines.append(f"+--------- [{i}] {role} " + "-" * 36)
        lines.append(content)
        lines.append("+" + "-" * 50)
        lines.append("")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（中文按 ~1.5 字符/token）。"""
    return int(len(text) / 1.5)


def usage_metrics(usage):
    """Keep unavailable counters unknown, including a missing cache counter."""
    def field(value,key):
        return value.get(key) if isinstance(value,dict) else getattr(value,key,None)
    def count(value):
        return value if type(value) is int and value>=0 else None
    incoming=count(field(usage,'prompt_tokens'))
    outgoing=count(field(usage,'completion_tokens'))
    cached=count(field(field(usage,'prompt_tokens_details'),'cached_tokens'))
    if incoming is None or (cached is not None and cached>incoming):
        cached=None
    return {'input_tokens':incoming,'output_tokens':outgoing,'cached_input_tokens':cached,
            'uncached_input_tokens':incoming-cached if incoming is not None and cached is not None else None,
            'reasoning_tokens':count(field(field(usage,'completion_tokens_details'),'reasoning_tokens')),
            'source':'provider' if incoming is not None and outgoing is not None else 'partial' if usage else 'missing'}


def summarize_calls(calls):
    """Aggregate actual counters with explicit coverage; never add estimates."""
    result={'calls':len(calls),'elapsed_ms':round(sum(c.get('elapsed_ms',0) for c in calls),2)}
    for key in ('input_tokens','output_tokens','cached_input_tokens','uncached_input_tokens','reasoning_tokens'):
        values=[c.get('usage',{}).get(key) for c in calls]
        known=[v for v in values if v is not None]
        result[key]=sum(known) if known else None
        result[key+'_reported_calls']=len(known)
    if calls and result['cached_input_tokens_reported_calls']==len(calls) and result['input_tokens']:
        result['cache_hit_ratio']=result['cached_input_tokens']/result['input_tokens']
    else:
        result['cache_hit_ratio']=None
    return result


def record_llm_call(
    kind: str,
    messages: list[dict],
    kwargs: dict,
    response_text: str,
    usage: Any,
    ms: float,
    model: str,
    preserve_request: bool = False,
) -> None:
    """把一次 LLM 调用记进当前 trace（没有 trace 时静默跳过）。"""
    tr = current_trace()
    if tr is None:
        return

    request_text = tr.artifacts.get('request','') if preserve_request else render_request(messages, model, kwargs)
    if not preserve_request:
        tr.artifact("request", request_text)
    tr.artifact("response", response_text)

    metrics=usage_metrics(usage)
    tokens_in,tokens_out=metrics['input_tokens'],metrics['output_tokens']

    detail: dict[str, Any] = {
        "model": model,
        "mode": kind,
        "messages": len(messages),
        "request_chars": len(request_text),
        "response_chars": len(response_text),
        'usage':metrics,
    }
    if tokens_in is not None:
        detail["tokens_in"] = tokens_in
    else:
        detail["tokens_in_est"] = estimate_tokens(request_text)
    if tokens_out is not None:
        detail["tokens_out"] = tokens_out
    else:
        detail["tokens_out_est"] = estimate_tokens(response_text)

    tr.step(f"llm.{kind}", ms=ms, **detail)


class TracedLLMClient:
    """LLM 客户端的 trace 包装层。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def close(self):
        close = getattr(self.inner, 'close', None)
        if callable(close):
            close()

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "") or "")

    @property
    def last_reasoning(self) -> str | None:
        """内层客户端单独返回的思考字段（如 reasoning_content）。"""
        return getattr(self.inner, "last_reasoning", None)

    @property
    def last_usage(self):
        return getattr(self.inner,'last_usage',None)

    def chat(self, messages: list[dict], **kwargs) -> str:
        if kwargs.get('stream') and callable(getattr(self.inner,'stream_chat',None)):
            options={k:v for k,v in kwargs.items() if k!='stream'}
            return ''.join(self._stream(messages,options,kind='chat'))
        t0 = time.perf_counter()
        tr,call=self._begin(messages,kwargs,'chat')
        try:
            text=self.inner.chat(messages,**kwargs)
        except BaseException as exc:
            self._end(tr,call,messages,kwargs,'chat','',t0,exc)
            raise
        self._end(tr,call,messages,kwargs,'chat',text,t0)
        return text

    def stream_chat(self, messages: list[dict], **kwargs) -> Iterator[str]:
        yield from self._stream(messages,kwargs,kind='stream')

    def _begin(self,messages,kwargs,kind):
        tr=current_trace()
        if tr is None:
            return None,None
        call=tr.begin_call(kind,self.model)
        request=render_request(messages,self.model,diagnostic_options(kwargs))
        tr.artifact(call['id']+'.request',request)
        tr.artifact('request',request)
        tr.artifact('response','')
        describe=getattr(self.inner,'request_description',None)
        payload=describe(messages,kwargs) if callable(describe) else {'model':self.model,'messages':messages,'options':kwargs}
        tr.artifact(call['id']+'.payload',json.dumps(diagnostic_options(payload),ensure_ascii=False,indent=2,default=str))
        call.update(request_artifact=call['id']+'.request',payload_artifact=call['id']+'.payload')
        tr.step('llm.request',call_id=call['id'],model=self.model,mode=kind)
        return tr,call

    def _end(self,tr,call,messages,kwargs,kind,text,t0,error=None):
        if tr is None:
            return
        call.update(status='aborted' if isinstance(error,GeneratorExit) else 'error' if error else 'ok',
                    elapsed_ms=round((time.perf_counter()-t0)*1000,2),response_chars=len(text),
                    response_state='partial' if error and text else 'none' if error else 'complete',
                    response_artifact=call['id']+'.response')
        tr.artifact(call['id']+'.response',text)
        reasoning=self.last_reasoning
        if reasoning:
            tr.artifact(call['id']+'.reasoning',reasoning)
        if error:
            call['error']=describe_error(error)
            tr.artifact(call['id']+'.error',call['error'])
        usage=getattr(self.inner,'last_usage',None)
        call['usage']=usage_metrics(usage)
        record_llm_call(kind,messages,kwargs,text,usage,(time.perf_counter()-t0)*1000,self.model,preserve_request=True)
        tr.step('llm.result',call_id=call['id'],status=call['status'],response_state=call['response_state'],error=call.get('error'))
        tr._active_call=None
        tr.flush()

    def _stream(self,messages,kwargs,kind):
        t0 = time.perf_counter()
        tr,call=self._begin(messages,{**kwargs,'stream':True},kind)
        parts: list[str] = []
        flushed=0.0
        stream=None
        try:
            stream=self.inner.stream_chat(messages,**kwargs)
            for chunk in stream:
                parts.append(chunk)
                if tr is not None and time.perf_counter()-flushed>=.5:
                    call.update(response_state='streaming',response_chars=sum(map(len,parts)))
                    tr.artifact(call['id']+'.response',''.join(parts));flushed=time.perf_counter()
                yield chunk
        except BaseException as exc:
            self._end(tr,call,messages,kwargs,kind,''.join(parts),t0,exc)
            raise
        else:
            self._end(tr,call,messages,kwargs,kind,''.join(parts),t0)
        finally:
            if stream is not None and hasattr(stream,'close'):
                stream.close()


__all__ = ["TracedLLMClient", "record_llm_call", "render_request", "estimate_tokens", "usage_metrics", "summarize_calls"]
