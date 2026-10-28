"""application/llm_json.py — LLM 响应处理的横切工具。

合并三处重复实现：
- chat_service.THINK_PATTERN + _strip_thinking
- extraction_service._clean_response
- persona_edit_service._clean_response

公开 API：
- THINK_PATTERN: 匹配 <think>/<thinking>/<reasoning> 块的正则（定义在 utils/text.py）
- strip_thinking(text): 移除思考块
- clean_response(text): 清理 JSON 响应（剥 think / 剥 markdown / 提取首个 {...}）
"""
from __future__ import annotations

import json
import re
from typing import Any

# 思考块模式定义在 utils/text.py（共享，换模型时只改那里）


# === Thinking 块剥离 ===

# pattern 定义在 utils/text.py（支持 think / thinking / reasoning 三种标签）。
# 这里再导出一份，为了保持旧的导入路径不拉下。
from companion_ever.utils.text import (  # noqa: E402
    THINK_PATTERN,
    strip_thinking,
)


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