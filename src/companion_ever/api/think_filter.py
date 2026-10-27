"""api/think_filter.py — 增量 <think> 块过滤器。

MiniMax-M3 等推理模型会在回复里夹带 <think>...</think> 思考块。
Web 前端要把思考过程藏起来，但又不能等整段生成完再显示（那就不叫流式了）。

做法：
- 累积 raw 原文
- 每次 feed(chunk) 后算出"到此刻为止可见的文本"
- 只把新增部分发给前端
- 已闭合的 think 块整段剔除；尚未闭合的整段隐藏
- 尾部可能是 "<think>" 前缀的碎片会被暂存，避免把 "<thi" 这种半截标签漏给前端
"""
from __future__ import annotations

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def visible_prefix(text: str) -> str:
    """返回 text 中"到此刻可见"的部分。

    - 已闭合的 <think>...</think> 整段剔除
    - 尚未闭合的 <think>...（后面还没到）整段隐藏
    """
    out: list[str] = []
    rest = text
    while True:
        start = rest.find(_THINK_OPEN)
        if start == -1:
            out.append(rest)
            return "".join(out)
        out.append(rest[:start])
        end = rest.find(_THINK_CLOSE, start + len(_THINK_OPEN))
        if end == -1:
            # 未闭合 → 后面的内容先全部藏起来
            return "".join(out)
        rest = rest[end + len(_THINK_CLOSE) :]


def _split_safe(buf: str) -> tuple[str, str]:
    """把 buf 拆成 (可安全发出的文本, 需要暂存的尾部)。

    暂存的尾部一定是 "<think>" 的真前缀（最多 5 个字符）。
    """
    for k in range(min(len(buf), len(_THINK_OPEN) - 1), 0, -1):
        if _THINK_OPEN.startswith(buf[-k:]):
            return buf[:-k], buf[-k:]
    return buf, ""


class ThinkFilter:
    """流式场景下的 think 块过滤。"""

    def __init__(self) -> None:
        self._raw = ""
        self._pending = ""
        self._sent = 0

    def feed(self, chunk: str) -> str:
        """喂入一个新 chunk，返回应该发给前端的增量文本。"""
        self._pending += chunk
        safe, self._pending = _split_safe(self._pending)
        if not safe:
            return ""

        self._raw += safe
        visible = visible_prefix(self._raw)
        if len(visible) <= self._sent:
            return ""
        delta = visible[self._sent :]
        self._sent = len(visible)
        return delta

    def flush(self) -> str:
        """流结束时的收尾。

        暂存的尾部是截断的 think 标签碎片，直接丢弃。
        """
        self._pending = ""
        visible = visible_prefix(self._raw)
        if len(visible) <= self._sent:
            return ""
        delta = visible[self._sent :]
        self._sent = len(visible)
        return delta


__all__ = ["ThinkFilter", "visible_prefix"]
