"""application/llm_json.py — LLM 响应处理的横切工具。

合并三处重复实现：
- chat_service.THINK_PATTERN + _strip_thinking
- extraction_service._clean_response
- persona_edit_service._clean_response

公开 API：
- THINK_PATTERN: 匹配 <think>...</think> 的正则
- strip_thinking(text): 移除思考块
- clean_response(text): 清理 JSON 响应（剥 think / 剥 markdown / 提取首个 {...}）
"""
from __future__ import annotations

import json
import re
from typing import Any


# === Thinking 块剥离 ===

THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(text: str) -> str:
    """剥掉 LLM 的 <think>...</think> 思考块（用于持久化）。

    实时的 chat() / stream_chat() 返回原值（用户可见思考过程）。
    但写入 conversation_log 时调用此函数剥掉（保持干净）。
    """
    return THINK_PATTERN.sub("", text).strip()


# === JSON 响应清理 ===

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def clean_response(text: str) -> str:
    """清理 LLM JSON 响应：剥 think 块、剥 markdown 包装、提取 JSON。

    策略：
    1. 剥 <think>...</think> 块
    2. 优先匹配 ```json ... ``` 代码块
    3. 兜底：从第一个 { 到最后一个 } 截取
    4. 都失败：返回原文（让上层 try/except 兜）
    """
    text = THINK_PATTERN.sub("", text)
    match = _JSON_FENCE_RE.search(text)
    if match:
        return match.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    return text


def parse_json_response(text: str) -> dict[str, Any] | None:
    """清理 + 解析 JSON 响应。失败返回 None。"""
    cleaned = clean_response(text)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None


__all__ = [
    "THINK_PATTERN",
    "strip_thinking",
    "clean_response",
    "parse_json_response",
]