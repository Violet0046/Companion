"""domain — 共享内核：两个及以上业务模块共用的值对象与枚举。

只放"跨模块传递"的东西：
- entities: User、Note、Block、ConversationMessage
- enums: MemoryKind、LinkType、BlockType

只属于单个业务模块的规则不放这里，避免共享包变成垃圾桶：
- 关系方向契约（RELATION_KINDS / valid_relation）→ memory/relations.py
- 角色模板（PERSONA_TEMPLATES）→ characters/templates.py

本包不依赖任何业务模块，也不含业务逻辑与数据库访问。
"""
from __future__ import annotations

from .entities import Block, ConversationMessage, Note, User
from .enums import BlockType, LinkType, MemoryKind

__all__ = [
    # entities
    "User",
    "Note",
    "Block",
    "ConversationMessage",
    # enums
    "MemoryKind",
    "LinkType",
    "BlockType",
]
