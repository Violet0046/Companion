"""增量 <think> 块过滤器，供会话事件投递使用。

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

import re

from companion_ever.utils.text import (
    THINK_TAG_NAMES, strip_format_prefix, strip_tool_calls)

# 支持 think / thinking / reasoning 三种标签（换模型时无需改代码）
_TAG_ALT = "|".join(THINK_TAG_NAMES)
_OPEN_RE = re.compile(r"<(" + _TAG_ALT + r")>", re.IGNORECASE)
# 聊天没有工具：模型自编的 <tool_call> 块同样要藏起来
_TOOL_OPEN_RE = re.compile(r"<[\s]*tool_call[\s]*>", re.IGNORECASE)
_TOOL_CLOSE_RE = re.compile(r"</[\s]*tool_call[\s]*>", re.IGNORECASE)


def _hide_tool_calls(text: str) -> str:
    """把模型自编的 tool_call 块整段藏起来（聊天没有工具）。"""
    m = _TOOL_OPEN_RE.search(text)
    if not m:
        return text
    cm = _TOOL_CLOSE_RE.search(text, m.end())
    if not cm:
        # 还没闭合 → 后面的内容先全部藏起来
        return text[: m.start()]
    return text[: m.start()] + _hide_tool_calls(text[cm.end():])


def visible_prefix(text: str) -> str:
    """返回 text 中"到此刻可见"的部分。

    - 已闭合的思考块整段剔除
    - 尚未闭合的思考块（后面还没到）整段隐藏
    - 模型自编的 tool_call 块也整段隐藏
    """
    out: list[str] = []
    rest = text
    while True:
        m = _OPEN_RE.search(rest)
        if not m:
            out.append(rest)
            return _hide_tool_calls("".join(out))
        out.append(rest[: m.start()])
        close = re.compile(r"</" + re.escape(m.group(1)) + r">", re.IGNORECASE)
        cm = close.search(rest, m.end())
        if not cm:
            # 未闭合 → 后面的内容先全部藏起来
            return _hide_tool_calls("".join(out))
        rest = rest[cm.end():]


def _split_safe(buf: str) -> tuple[str, str]:
    """把 buf 拆成 (可安全发出的文本, 需要暂存的尾部)。

    暂存的尾部一定是 "<think>" 或 "<tool_call>" 的真前缀。
    """
    idx = buf.rfind("<")
    if idx == -1:
        return buf, ""
    frag = buf[idx + 1:].lower()
    if not frag:
        # 只有一个 "<"，也可能是标签开头
        return buf[:idx], buf[idx:]
    # 零宽字符塞在标签里很常见（"<tool_call>"），比对前先去掉
    clean = "".join(ch for ch in frag if ch not in "﻿⁠")
    for name in THINK_TAG_NAMES:
        if name.startswith(clean) or clean.startswith(name):
            return buf[:idx], buf[idx:]
    if "tool_call".startswith(clean) or clean.startswith("tool_call"):
        return buf[:idx], buf[idx:]
    return buf, ""


class ThinkFilter:
    """流式场景下的 think 块过滤。"""

    def __init__(self) -> None:
        self._raw = ""
        self._pending = ""
        self._sent = 0
        # 模型学了历史消息的 "[2026-10-28 03:00:56] " 前缀，会在自己回复里也加上。
        # 那是模仿的，不是正文 —— 在流式输出里就把它剔掉。
        self._mimic_stripped = False

    def feed(self, chunk: str) -> str:
        """喂入一个新 chunk，返回应该发给前端的增量文本。"""
        self._pending += chunk
        safe, self._pending = _split_safe(self._pending)
        if not safe:
            return ""

        self._raw += safe
        visible = visible_prefix(self._raw)

        # 剔掉回复开头的模仿前缀（只剔一次）
        if not self._mimic_stripped:
            stripped, layers = strip_format_prefix(visible)
            if layers:
                visible = stripped
                self._mimic_stripped = True
                self._sent = 0  # 重置已发送位置

        if len(visible) <= self._sent:
            # 模型只吐了 tool_call：真正的回复藏在块里，兜底显示出来
            _, fallback = strip_tool_calls(self._raw)
            if fallback and not visible.strip():
                return fallback
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
        if not self._mimic_stripped:
            stripped, layers = strip_format_prefix(visible)
            if layers:
                visible = stripped
                self._mimic_stripped = True
                self._sent = 0
        if len(visible) <= self._sent:
            return ""
        delta = visible[self._sent :]
        self._sent = len(visible)
        return delta


__all__ = ["ThinkFilter", "visible_prefix"]
