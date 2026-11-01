"""assistant 回复的拆分与清洗。"""
from __future__ import annotations
import re

from companion_ever.utils.text import (
    extract_thinking,
    strip_format_prefix,
    strip_invisible,
    strip_tool_calls,
)


def split_assistant_reply(
    text: str, reasoning: str | None = None,
) -> tuple[str, str | None]:
    """把 LLM 原始回复拆成 (入库正文, think 原文)。

    Args:
        text: LLM 返回的原始文本（可能含思考块和模仿前缀）
        reasoning: 模型单独返回的思考字段（如 reasoning_content）。
            一些推理模型不把思考写在正文里，而是单独一个字段。

    Returns:
        (干净正文, think 原文 or None)

    关键：**没有 think 的模型也能正常工作** ——
    此时 think 返回 None，写入路径就不会产生 thinking_log 记录。
    """
    body, think = extract_thinking(text)
    body, _ = strip_format_prefix(body)
    body = strip_invisible(body).strip()
    # 聊天没有工具，模型自编的 tool_call 块是垃圾；正文为空时用块里的文本兜底
    body, fallback = strip_tool_calls(body)
    if not body and fallback:
        body = fallback

    # 模型单独返回的思考字段（优先于从正文里解析）
    if reasoning and reasoning.strip():
        reasoning = strip_invisible(reasoning).strip()
        think = f"{reasoning}\n\n{think}" if think else reasoning

    return body, (think or None)


def split_messages(body: str) -> list[str]:
    """Explicit boundaries win; blank-line fallback applies only to unstructured text."""
    if '<message_break>' in body:
        parts = body.split('<message_break>')
    elif re.search(r'(?m)^\s*(?:```|~~~|#{1,6}\s|[-*+]\s|\d+[.)、]\s|>|\|)', body):
        return [body.strip()] if body.strip() else []
    else:
        parts = re.split(r'\n[ \t]*\n+', body.replace('\r\n','\n'))
    parts = [p.strip() for p in parts if p.strip()]
    return parts[:2] + ['\n\n'.join(parts[2:])] if len(parts)>3 else parts


__all__ = ["split_assistant_reply", "split_messages"]
