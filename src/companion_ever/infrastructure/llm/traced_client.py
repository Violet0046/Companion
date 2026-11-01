"""Record one small summary per model request, without retaining request/response bodies."""
from datetime import datetime, timezone
import time
import uuid

from companion_ever.infrastructure.tracing import current_call_context, summarize_calls
from companion_ever.runtime.errors import failure_kind


def usage_metrics(usage):
    def field(value, key):
        return value.get(key) if isinstance(value, dict) else getattr(value, key, None)
    def count(value):
        return value if type(value) is int and value >= 0 else None
    incoming = count(field(usage, 'prompt_tokens'))
    outgoing = count(field(usage, 'completion_tokens'))
    cached = count(field(field(usage, 'prompt_tokens_details'), 'cached_tokens'))
    if incoming is None or (cached is not None and cached > incoming):
        cached = None
    return {'input_tokens': incoming, 'output_tokens': outgoing, 'cached_input_tokens': cached,
            'uncached_input_tokens': incoming - cached if incoming is not None and cached is not None else None,
            'reasoning_tokens': count(field(field(usage, 'completion_tokens_details'), 'reasoning_tokens')),
            'source': 'provider' if incoming is not None and outgoing is not None else 'partial' if usage else 'missing'}


def error_summary(error):
    if isinstance(error, (GeneratorExit, KeyboardInterrupt, SystemExit)):
        return {'kind': 'interrupted', 'stage': 'request', 'message': '模型调用中断'}
    kind = failure_kind(error)
    messages = {'model_timeout': '模型请求超时', 'transport': '模型连接中断',
                'configuration': '模型配置或访问凭据错误', 'provider_status': '模型服务返回异常状态',
                'validation': '模型响应未正常完成', 'internal': '模型请求执行异常'}
    result = {'kind': kind, 'stage': 'request', 'message': messages.get(kind, '模型调用失败')}
    if type(getattr(error, 'status_code', None)) is int:
        result['http_status'] = error.status_code
    return result  # Never persist str(error): providers may echo private payloads and credentials.


class TracedLLMClient:
    def __init__(self, inner, store=None):
        self.inner = inner
        self.store = store

    def close(self):
        close = getattr(self.inner, 'close', None)
        if callable(close):
            close()

    @property
    def model(self):
        return str(getattr(self.inner, 'model', '') or '')

    @property
    def last_reasoning(self):
        return getattr(self.inner, 'last_reasoning', None)

    @property
    def last_usage(self):
        return getattr(self.inner, 'last_usage', None)

    def _begin(self):
        try:
            self.inner.last_usage = None
        except (AttributeError, TypeError):
            pass
        context = current_call_context()
        return context, context.next_index() if context else 1, datetime.now(timezone.utc).isoformat(), time.perf_counter()

    def _finish(self, start, error):
        context, index, started_at, clock = start
        if context is None:
            return
        usage = usage_metrics(self.last_usage)
        record = {'id': 'call_' + uuid.uuid4().hex, 'started_at': started_at,
                  'layer': context.layer, 'model': self.model[:200],
                  'status': 'ok' if error is None else 'aborted' if isinstance(error, (GeneratorExit, KeyboardInterrupt, SystemExit)) else 'error',
                  'duration_ms': round((time.perf_counter() - clock) * 1000, 2),
                  'user_id': context.user_id, 'persona_id': context.persona_id,
                  'generation_id': context.generation_id, 'job_id': context.job_id,
                  'attempt_id': context.attempt_id, 'attempt': context.attempt, 'call_index': index,
                  **{k: usage[k] for k in ('input_tokens', 'output_tokens', 'cached_input_tokens')},
                  'error': error_summary(error) if error else None}
        context.calls.append(record)
        if self.store is not None:
            self.store.append(record)

    def chat(self, messages, **kwargs):
        if kwargs.get('stream') and callable(getattr(self.inner, 'stream_chat', None)):
            return ''.join(self.stream_chat(messages, **{k: v for k, v in kwargs.items() if k != 'stream'}))
        start = self._begin()
        error = None
        try:
            return self.inner.chat(messages, **kwargs)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._finish(start, error)

    def stream_chat(self, messages, **kwargs):
        start = self._begin()
        error = None
        stream = None
        try:
            stream = self.inner.stream_chat(messages, **kwargs)
            yield from stream
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                if stream is not None and callable(getattr(stream, 'close', None)):
                    stream.close()
            except BaseException as exc:
                if error is None:
                    error = exc
                    raise
            finally:
                self._finish(start, error)
