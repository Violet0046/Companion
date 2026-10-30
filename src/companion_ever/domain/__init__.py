"""domain — 域层（纯数据 + 类型 + 异常）。

包含：
- enums: MemoryKind, BlockType
- entities: User, Note, Block, ConversationMessage, PersonaBasics
- persona_templates: 内置人设模板
- errors: 异常（占位）
"""
from __future__ import annotations

from .entities import Block, ConversationMessage, Note, PersonaBasics, User
from .enums import BlockType, MemoryKind
from .persona_templates import (
    PERSONA_TEMPLATES,
    get_template,
    list_templates,
    template_to_json,
    validate_template_fields,
)

__all__ = [
    # enums
    "MemoryKind",
    "BlockType",
    # entities
    "User",
    "Note",
    "Block",
    "ConversationMessage",
    "PersonaBasics",
    # templates
    "PERSONA_TEMPLATES",
    "get_template",
    "template_to_json",
    "list_templates",
    "validate_template_fields",
]