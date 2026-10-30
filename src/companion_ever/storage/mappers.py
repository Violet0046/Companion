"""sqlite3.Row → domain entity 集中映射。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from companion_ever.domain.entities import Block, ConversationMessage, Note, User
from companion_ever.domain.enums import BlockType, MemoryKind


def row_to_user(row: sqlite3.Row) -> User:
    """user 表行 → User 实体。"""
    return User(
        id=row["id"],
        persona_ids=_parse_csv(row["persona_ids"]),
    )


def row_to_note(row: sqlite3.Row) -> Note:
    """note 表行 → 经历/事实实体；角色配置不经过这里。"""
    return Note(
        id=row['id'], user_id=row['user_id'], persona_id=row['persona_id'],
        kind=MemoryKind(row['kind']), content=row['content'], source_quote=row['source_quote'],
        importance=row['importance'], decay_score=row['decay_score'], is_archived=bool(row['is_archived']),
        created_at=row['created_at'], updated_at=row['updated_at'], last_accessed_at=row['last_accessed_at'],
        reality=row['reality'], event_time=row['event_time'], time_basis=row['time_basis'],
        revision=row['revision'], pinned=bool(row['pinned']),
    )


def row_to_block(row: sqlite3.Row) -> Block:
    """memory_block 表行 → Block 实体（fields 已 JSON 解析）。"""
    raw_fields_json: str = row["fields_json"]
    try:
        fields = json.loads(raw_fields_json) if raw_fields_json else {}
    except (json.JSONDecodeError, TypeError):
        fields = {}

    return Block(
        id=row["id"],
        user_id=row["user_id"],
        persona_id=row["persona_id"],
        block_type=BlockType(row["block_type"]),
        fields=fields,
        updated_at=row["updated_at"],
    )


def row_to_conversation_message(row: sqlite3.Row) -> ConversationMessage:
    """conversation_log 表行 → ConversationMessage 实体。"""
    return ConversationMessage(
        id=row["id"],
        user_id=row["user_id"],
        persona_id=row["persona_id"],
        turn_index=row["turn_index"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
        client_message_id=row['client_message_id'] if 'client_message_id' in row.keys() else None,
    )


# === 内部 ===

def _parse_csv(s: str) -> list[str]:
    """'a,b,c' → ['a', 'b', 'c']; '' 或 None → []。"""
    if not s:
        return []
    return [item.strip() for item in s.split(",") if item.strip()]


__all__ = [
    "row_to_user",
    "row_to_note",
    "row_to_block",
    "row_to_conversation_message",
]
