"""prompts — LLM 提示词模板。

使用 importlib.resources 从包内读取 .txt 文件。
模块加载时缓存（@lru_cache），无运行时 IO。

可用模板：
- memory_draft_system.txt: 后台整理两层笔记的系统提示
- summary_system.txt: 从 fact 操作独立演化两个摘要的系统提示
- PERSONA_EDIT_SYSTEM: 人设编辑时的系统提示（含 {current} 和 {instruction} 占位符）
- CHAT_FOOTER: 拼 system prompt 时的"行为指引"脚注
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """加载包内的 .txt 提示词文件。"""
    return (
        resources.files("companion_ever.prompts")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


# 模块级常量（首次访问时加载；之后命中 lru_cache）
PERSONA_EDIT_SYSTEM: str = load("persona_edit_system.txt")
CHAT_FOOTER: str = load("chat_footer.txt")


__all__ = [
    "load",
    "PERSONA_EDIT_SYSTEM",
    "CHAT_FOOTER",
]
