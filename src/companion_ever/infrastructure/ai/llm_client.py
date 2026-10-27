"""infrastructure/ai/llm_client.py — LLM 客户端协议与工厂。

设计原则：
- LLMClient 是抽象接口（Protocol），便于将来换实现
- 工厂 create_llm_client(cfg) 根据配置返回具体实例
- 支持同步和流式两种调用
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from companion_ever.config import LLMConfig

from .openai_client import OpenAICompatibleClient


class LLMClient(Protocol):
    """LLM 客户端协议（接口）。"""

    def chat(self, messages: list[dict], **kwargs) -> str:
        """同步调用，返回完整回复文本。"""
        ...

    def stream_chat(self, messages: list[dict], **kwargs) -> Iterator[str]:
        """流式调用，yield 文本片段（增量）。"""
        ...


def create_llm_client(cfg: LLMConfig) -> LLMClient:
    """根据配置构造 LLM 客户端。

    当前仅支持 OpenAI 兼容协议（OpenAI / DeepSeek / MiniMax / Qwen 等）。
    将来可在此扩展按 cfg 字段分派不同实现。
    """
    return OpenAICompatibleClient(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        timeout=cfg.timeout,
    )


__all__ = ["LLMClient", "create_llm_client"]