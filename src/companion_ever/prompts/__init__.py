"""prompts — LLM 提示词模板。

使用 importlib.resources 从包内读取 .txt 文件。
模块加载时缓存（@lru_cache），无运行时 IO。

可用模板：
- chat_system.txt: 聊天 system prompt 的完整骨架（章节标题 + 行为指引）
- memory_draft_system.txt: 后台整理两层笔记的系统提示
- summary_system.txt: 从 fact 操作独立演化两个摘要的系统提示
- repair_evidence.txt / repair_citation.txt: 校验失败后的来源约束（按场景区分）

模板约定：
- chat_system.txt 用 `{name}` 占位符填充小节正文；小节标题是字面文本，
  行为指引直接引用同一批标题，因此"提示词引用的章节"与"实际渲染的章节"
  不会走偏。占位符为空的小节连同标题一起消失。
- 其余模板由调用方自行拼接（摘要、纠错说明等），只提供静态文本。
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
    for section in ("\n" + template).split("\n## ")[1:]:
        title, _, body = section.partition("\n")
        filled = _PLACEHOLDER.sub(
            lambda m: values.get(m.group(1), m.group(0)), body
        ).strip("\n")
        if filled.strip():
            blocks.append(f"## {title}\n{filled}".strip())
    return "\n\n".join(blocks)


# 模块级常量（首次访问时加载；之后命中 lru_cache）
CHAT_SYSTEM: str = load("chat_system.txt")


__all__ = [
    "load",
    "compose",
    "CHAT_SYSTEM",
]
