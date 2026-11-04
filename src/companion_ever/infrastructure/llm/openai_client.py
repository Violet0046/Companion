"""OpenAI 兼容协议的 LLM 客户端。"""
from __future__ import annotations

from collections.abc import Iterator
import threading
import time




class OpenAICompatibleClient:
    """OpenAI 兼容协议的 LLM 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        stream_usage: bool = True,
        reasoning_effort: str = "low",
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.stream_usage = stream_usage
        self.reasoning_effort = reasoning_effort
        self._local = threading.local()
        self._client = None  # lazy load
        self._client_lock = threading.Lock()
        self._closed = False
        self.last_usage = None  # 最近一次调用的 token 用量（给 trace 包装层读）
        # 模型单独返回的思考字段（如 DeepSeek/MiniMax 的 reasoning_content）。
        # 没有这个字段的模型就是 None，说明它把思考写在正文里了。
        self.last_reasoning = None

    @property
    def last_reasoning(self):
        return getattr(self._local, 'reasoning', None)

    @last_reasoning.setter
    def last_reasoning(self, value):
        self._local.reasoning = value

    @property
    def last_usage(self):
        return getattr(self._local, 'usage', None)

    @last_usage.setter
    def last_usage(self, value):
        self._local.usage = value

    @property
    def client(self):
        with self._client_lock:
            if self._closed:
                raise RuntimeError('模型客户端已关闭')
            if self._client is None:
                try:
                    from openai import OpenAI
                    self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
                except ImportError as e:
                    raise ImportError('请先安装 openai 包：pip install openai') from e
            return self._client

    def close(self):
        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            client, self._client = self._client, None
        close = getattr(client, 'close', None)
        if callable(close):
            close()

    def _model_options(self,kwargs):
        thinking=kwargs.pop('background_thinking',None)
        model=self.model.lower()
        # M3.1 requires thinking; lower its effort instead of disabling it.
        # Shared by sync, streaming and request descriptions, across all layers.
        if model=='minimax-m3.1-flash':
            kwargs.setdefault('reasoning_effort',self.reasoning_effort)
        elif thinking is not None and model=='minimax-m3':
            kwargs['extra_body']={**kwargs.get('extra_body',{}),'thinking':{'type':'adaptive' if thinking else 'disabled'}}
        return kwargs

    def request_description(self,messages,kwargs):
        options=self._model_options(dict(kwargs))
        if options.get('stream') and self.stream_usage:
            options.setdefault('stream_options',{'include_usage':True})
        client={k:options.pop(k) for k in ('timeout','max_retries','total_timeout','extra_headers','extra_query') if k in options}
        extra=options.pop('extra_body',{})
        return {'body':{'model':self.model,'messages':messages,**options,**extra},'client_options':client}

    def chat(self, messages: list[dict], **kwargs) -> str:
        """同步调用。"""
        if kwargs.pop('stream',False):
            return ''.join(self.stream_chat(messages,**kwargs))
        self.last_reasoning = None
        self.last_usage = None
        kwargs=self._model_options(kwargs)
        client = self.client
        if 'max_retries' in kwargs:
            client = client.with_options(max_retries=kwargs.pop('max_retries'))
        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs,
        )
        message = response.choices[0].message
        text = message.content or ""
        self.last_usage = getattr(response, "usage", None)
        self.last_reasoning = (
            getattr(message, "reasoning_content", None)
            or getattr(message, "reasoning", None)
        )
        return text

    def stream_chat(self, messages: list[dict], **kwargs) -> Iterator[str]:
        """流式调用。"""
        self.last_reasoning = None
        self.last_usage = None
        kwargs=self._model_options(kwargs)
        client=self.client
        if self.stream_usage:
            kwargs.setdefault('stream_options',{'include_usage':True})
        if 'max_retries' in kwargs:
            client=client.with_options(max_retries=kwargs.pop('max_retries'))
        total_timeout=kwargs.pop('total_timeout',None)
        started=time.monotonic()
        from companion_ever.runtime.tasks import checkpoint
        checkpoint()
        stream = client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        reasoning_parts: list[str] = []
        finish_reason=None
        first_frame=True;first_content=True
        try:
            for chunk in stream:
                checkpoint('model_first_frame' if first_frame else None)
                first_frame=False
                if total_timeout is not None and time.monotonic()-started>total_timeout:
                    raise TimeoutError('模型持续生成超过本次任务的总时限')
                if getattr(chunk,'usage',None) is not None:
                    self.last_usage=chunk.usage
                if not chunk.choices:
                    continue
                if chunk.choices[0].finish_reason:
                    finish_reason=chunk.choices[0].finish_reason
                delta = chunk.choices[0].delta
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    reasoning_parts.append(rc)
                    self.last_reasoning = "".join(reasoning_parts)
                if delta.content:
                    if first_content:
                        checkpoint('model_first_content');first_content=False
                    yield delta.content
        finally:
            stream.close()
        if finish_reason=='length':
            raise ValueError('模型输出达到长度上限，未保存不完整结果')
        if finish_reason is None:
            raise ConnectionError('模型响应流提前中断，未收到完成标记')
        if finish_reason not in ('stop','tool_calls'):
            raise ValueError('模型未正常完成回复：'+str(finish_reason))
        self.last_reasoning = "".join(reasoning_parts) or None


__all__ = ["OpenAICompatibleClient"]
