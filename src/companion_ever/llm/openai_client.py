"""OpenAI 兼容协议的 LLM 客户端。"""
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
        # 模型单独返回的思考字段（如 DeepSeek/MiniMax 的 reasoning_content）。
        # 没有这个字段的模型就是 None，说明它把思考写在正文里了。
        self.last_reasoning = None

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
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        parts: list[str] = []
        reasoning_parts: list[str] = []
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_parts.append(rc)
            if delta.content:
                parts.append(delta.content)
                yield delta.content

        self.last_usage = None
        self.last_reasoning = "".join(reasoning_parts) or None


__all__ = ["OpenAICompatibleClient"]
