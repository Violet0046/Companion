"""application/chat/assistant_reply.py — assistant 回复的拆分与清洗。

一次 LLM 回复里有两种东西：

1. **可见正文** —— 她说的话
2. **<think> 思考过程** —— 推理模型���内心戏

两者去的地方不同：

| 内容 | 去哪 | 原因 |
|---|---|---|
| 可见正文 | `conversation_log` | 要送进 prompt（模型看的），也要存档 |
| 思考过程 | `thinking_log` | 只给用户看，**绝不进 prompt**（纯污染，占历史 token 的 90%+）|

另外还要剥掉模型模仿旧格式吐出的 `[时间戳] assistant: ` 前缀
（prompt 里历史消息是这个格式，模型看多了会学去）。
"""
from __future__ import annotations

from companion_ever.utils.text import extract_thinking, strip_format_prefix


def split_assistant_reply(text: str) -> tuple[str, str | None]:
    """把 LLM 原始回复拆成 (入库正文, think 原文)。

    Args:
        text: LLM 返回的原始文本（可能含 <think> 块和模仿前缀）

    Returns:
        (干净正文, think 原文 or None)
    """
    body, think = extract_thinking(text)
    body, _ = strip_format_prefix(body)
    return body.strip(), think


__all__ = ["split_assistant_reply"]
