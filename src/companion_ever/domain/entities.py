"""domain/entities.py — 领域实体（dataclass）。

四个核心实体 + 一个 PersonBasics 包装：
- User: 用户 + 拥有的 persona_ids 列表
- Note: 笔记（14 种类型之一；PERSONA_BASICS 类型的 content 是 dict）
- Block: memory block（fields 已 JSON 解析）
- ConversationMessage: 对话原文消息
- PersonaBasics: PERSONA_BASICS 笔记的内容包装

每个实体都有 to_llm_dict() 方法——只把 LLM 需要的字段送出去。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .enums import BlockType, NoteType


@dataclass(frozen=True)
class User:
    """一个用户 + 他拥有的 persona_ids 列表。"""

    id: str
    persona_ids: list[str] = field(default_factory=list)


@dataclass
class Note:
    """一条笔记（domain 核心实体）。

    注：
    - PERSONA_BASICS 类型的 content 是 dict[str, Any]；其他类型是 str。
    - 读时由 mapper 自动 json.loads 解析；写时用 content_as_text() 反序列化。
    - 实体内部保留所有 DB 字段；LLM 边界用 to_llm_dict() 过滤。
    """

    id: str
    user_id: str
    persona_id: str
    type: NoteType
    content: str | dict[str, Any]
    source_quote: Optional[str]
    importance: float
    decay_score: float
    is_archived: bool
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    last_accessed_at: Optional[datetime]

    @property
    def is_persona_basics(self) -> bool:
        return self.type == NoteType.PERSONA_BASICS

    def content_as_text(self) -> str:
        """把 content 序列化为字符串（用于写入 DB）。"""
        if isinstance(self.content, dict):
            return json.dumps(self.content, ensure_ascii=False)
        return self.content

    def to_llm_dict(self) -> dict:
        """给 LLM 的简化视图（去掉时间戳、归档标记、衰减分等）。"""
        content = (
            self.content
            if isinstance(self.content, str)
            else json.dumps(self.content, ensure_ascii=False)
        )
        return {
            "id": self.id,
            "type": self.type.value,
            "content": content,
            "source_quote": self.source_quote,
            "importance": self.importance,
        }


@dataclass
class Block:
    """memory block（运行时状态块）。

    fields 是已 JSON 解析的 dict（不是字符串），由 repo 负责序列化。
    """

    id: str
    user_id: str
    persona_id: str
    block_type: BlockType
    fields: dict[str, Any] = field(default_factory=dict)
    updated_at: Optional[datetime] = None

    def to_llm_dict(self) -> dict:
        return {
            "block_type": self.block_type.value,
            "fields": self.fields,
        }


@dataclass
class ConversationMessage:
    """对话原文消息（一行 conversation_log）。"""

    id: int
    user_id: str
    persona_id: str
    turn_index: int
    role: str  # "user" | "assistant"
    content: str
    created_at: Optional[datetime] = None

    def to_llm_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class PersonaBasics:
    """PERSONA_BASICS 笔记的内容包装（dict[str, Any]）。"""

    fields: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.fields, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> "PersonaBasics":
        """从 JSON 字符串构造（解析失败时返回空 dict）。"""
        if not s:
            return cls(fields={})
        try:
            return cls(fields=json.loads(s))
        except (json.JSONDecodeError, TypeError):
            return cls(fields={"_raw": s})


__all__ = ["User", "Note", "Block", "ConversationMessage", "PersonaBasics"]