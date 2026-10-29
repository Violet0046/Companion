"""assistant 回复的拆分与清洗。"""
from __future__ import annotations

from companion_ever.utils.text import (
    extract_thinking,
    strip_format_prefix,
    strip_invisible,
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

    # 模型单独返回的思考字段（优先于从正文里解析）
    if reasoning and reasoning.strip():
        reasoning = strip_invisible(reasoning).strip()
        think = f"{reasoning}\n\n{think}" if think else reasoning

    return body, (think or None)


__all__ = ["split_assistant_reply"]
