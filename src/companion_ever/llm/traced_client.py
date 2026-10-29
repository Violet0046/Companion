"""给任意 LLM 客户端套一层 trace 记录。"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any, Optional

from companion_ever.trace import current_trace


def render_request(messages: list[dict], model: str, kwargs: dict) -> str:
    """把实际发给 LLM 的 messages 渲染成可读文本。"""
    lines = [
        "===== 实际发给 LLM 的请求 =====",
        f"# model  : {model}",
        f"# kwargs : {json.dumps(kwargs, ensure_ascii=False, default=str)}",
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


def record_llm_call(
    kind: str,
    messages: list[dict],
    kwargs: dict,
    response_text: str,
    usage: Any,
    ms: float,
    model: str,
) -> None:
    """把一次 LLM 调用记进当前 trace（没有 trace 时静默跳过）。"""
    tr = current_trace()
    if tr is None:
        return

    request_text = render_request(messages, model, kwargs)
    tr.artifact("request", request_text)
    tr.artifact("response", response_text)

    tokens_in = getattr(usage, "prompt_tokens", None) if usage else None
    tokens_out = getattr(usage, "completion_tokens", None) if usage else None

    detail: dict[str, Any] = {
        "model": model,
        "mode": kind,
        "messages": len(messages),
        "request_chars": len(request_text),
        "response_chars": len(response_text),
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

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "") or "")

    @property
    def last_reasoning(self) -> str | None:
        """内层客户端单独返回的思考字段（如 reasoning_content）。"""
        return getattr(self.inner, "last_reasoning", None)

    def chat(self, messages: list[dict], **kwargs) -> str:
        t0 = time.perf_counter()
        text = self.inner.chat(messages, **kwargs)
        record_llm_call(
            kind="chat",
            messages=messages,
            kwargs=kwargs,
            response_text=text,
            usage=getattr(self.inner, "last_usage", None),
            ms=(time.perf_counter() - t0) * 1000,
            model=self.model,
        )
        return text

    def stream_chat(self, messages: list[dict], **kwargs) -> Iterator[str]:
        t0 = time.perf_counter()
        parts: list[str] = []
        for chunk in self.inner.stream_chat(messages, **kwargs):
            parts.append(chunk)
            yield chunk
        record_llm_call(
            kind="stream",
            messages=messages,
            kwargs=kwargs,
            response_text="".join(parts),
            usage=getattr(self.inner, "last_usage", None),
            ms=(time.perf_counter() - t0) * 1000,
            model=self.model,
        )


__all__ = ["TracedLLMClient", "record_llm_call", "render_request", "estimate_tokens"]
