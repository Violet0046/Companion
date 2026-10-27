"""infrastructure/ai/openai_client.py — OpenAI 兼容协议的 LLM 客户端。

支持：OpenAI / DeepSeek / MiniMax / Qwen / 智谱 等
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
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs,
        )
        return response.choices[0].message.content or ""

    def stream_chat(
        self, messages: list[dict], **kwargs
    ) -> Iterator[str]:
        """流式调用。"""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


__all__ = ["OpenAICompatibleClient"]