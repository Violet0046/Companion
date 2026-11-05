"""按链路组织的 LLM 提示词，首次读取后缓存。

conversations/：私聊、群聊、协调、主动触达的完整模板。
memory/：记忆整理、画像与关系摘要的完整模板。
fragments/：外部上下文、群整理视角、两类校验纠错的按需片段。

调用方显式 load('目录/名称.txt')；不自动继承或追加公共规则。
compose 按二级标题填充占位符，空节连标题移除，插入值不再次展开。
本轮引用和触发类型由 conversations.context 组装；结构化输出的
Schema 及校验错误由对应链路附加，目录只存静态规则。
"""
from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """加载包内的 .txt 提示词文件。"""
    return (
        resources.files("companion_ever.prompts")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def compose(template: str, values: dict[str, str]) -> str:
    """按 '## ' 标题切块填充模板；正文为空的小节连标题一起丢弃。

    标题留在模板里，行为指引才能引用到真实存在的章节。
    填充用一次性替换，插入的正文不会被再次扫描（JSON 正文里的 {} 不受影响）。
    """
    blocks: list[str] = []
    sections = {}
    for section in ("\n" + template).split("\n## ")[1:]:
        title, _, body = section.partition("\n")
        sections[title] = body
    # Appended mode templates override named sections without duplicating the base prompt.
    for title, body in sections.items():
        filled = _PLACEHOLDER.sub(
            lambda m: values.get(m.group(1), m.group(0)), body
        ).strip("\n")
        if filled.strip():
            blocks.append(f"## {title}\n{filled}".strip())
    return "\n\n".join(blocks)


# 模块级常量（首次访问时加载；之后命中 lru_cache）
CHAT_SYSTEM: str = load("conversations/private.txt")


__all__ = [
    "load",
    "compose",
    "CHAT_SYSTEM",
]
