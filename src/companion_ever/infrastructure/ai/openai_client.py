"""infrastructure/ai/openai_client.py — OpenAI 兼容协议的 LLM 客户端。

支持：OpenAI / DeepSeek / Qwen / 智谱 / MiniMax 等

**可观测性**：所有 LLM 调用都会把"实际发送的请求"和"实际收到的回复"记进当前 trace。
这是在**调用发生的那一刻**捕获的，不是事后重建 —— 因为重建会因为 think 剥离、
时间戳转换等处理而失真。
"""
from __future__ import annotations

from collections.abc import Iterator




class OpenAICompatibleClient:
    """OpenAI 兼容协议的 LLM 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self._client = None  # lazy load
        self.last_usage = None  # 最近一次调用的 token 用量（给 trace 包装层读）

    @property
    def client(self):
        if self._client is None:
            try:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=self.timeout,
                )
            except ImportError as e:
                raise ImportError(
                    "请先安装 openai 包：pip install openai"
                ) from e
        return self._client

    def chat(self, messages: list[dict], **kwargs) -> str:
        """同步调用。"""
        t0 = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs,
        )
        text = response.choices[0].message.content or ""
        self.last_usage = getattr(response, "usage", None)
        return text

    def stream_chat(self, messages: list[dict], **kwargs) -> Iterator[str]:
        """流式调用。"""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        parts: list[str] = []
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                piece = chunk.choices[0].delta.content
                parts.append(piece)
                yield piece

        self.last_usage = None


__all__ = ["OpenAICompatibleClient"]
