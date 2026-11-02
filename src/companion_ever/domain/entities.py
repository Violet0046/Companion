"""domain/entities.py — 跨业务模块共享的值对象（dataclass）。

四个核心实体：
- User: 用户 + 拥有的 persona_ids 列表
- Note: 经历或事实，正文与情境元信息独立保存
- Block: memory block（fields 已 JSON 解析）
- ConversationMessage: 对话原文消息

每个实体都有 to_llm_dict() 方法——只把 LLM 需要的字段送出去。
只属于单个业务模块的规则不放这里；见 domain/__init__.py 的边界说明。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .enums import BlockType, MemoryKind


@dataclass(frozen=True)
class User:
    """一个用户 + 他拥有的 persona_ids 列表。"""

    id: str
    persona_ids: list[str] = field(default_factory=list)


@dataclass
class Note:
    """一条笔记（domain 核心实体）。

    注：
    - content 是独立可理解的文本，角色配置单独存储。
    - 实体内部保留所有 DB 字段；LLM 边界用 to_llm_dict() 过滤。
    """

    id: str
    user_id: str
    persona_id: str
    kind: MemoryKind
    content: str
    source_quote: Optional[str]
    importance: float
    decay_score: float
    is_archived: bool
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    last_accessed_at: Optional[datetime]

    reality: str = 'uncertain'
    event_time: Optional[str] = None
    time_basis: str = ''
    revision: int = 1

    def to_llm_dict(self) -> dict:
        """给 LLM 的简化视图（去掉时间戳、归档标记、衰减分等）。"""
        content = (
            self.content
            if isinstance(self.content, str)
            else json.dumps(self.content, ensure_ascii=False)
        )
        return {
            "id": self.id,
            "kind": self.kind.value,
            "reality": self.reality, "event_time": self.event_time, "time_basis": self.time_basis,
            "content": content,
            "source_quote": self.source_quote,
            "importance": self.importance,
        }


@dataclass
class Block:
    """用户画像或关系进展摘要。

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
    client_message_id: Optional[str] = None
    quoted_message_id: Optional[int] = None
    conversation_id: Optional[int] = None
    sender_kind: Optional[str] = None
    sender_id: Optional[str] = None

    def to_llm_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


__all__ = ["User", "Note", "Block", "ConversationMessage"]
