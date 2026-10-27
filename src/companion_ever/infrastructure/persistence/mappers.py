"""infrastructure/persistence/mappers.py — sqlite3.Row → domain entity 集中映射。

将所有 _row_to_X 助手集中在一个地方，避免每个 repo 各一份。
注意：Note.content 在 PERSONA_BASICS 类型时自动 json.loads 解析为 dict。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from companion_ever.domain.entities import Block, ConversationMessage, Note, User
from companion_ever.domain.enums import BlockType, NoteType


def row_to_user(row: sqlite3.Row) -> User:
    """user 表行 → User 实体。"""
    return User(
        id=row["id"],
        persona_ids=_parse_csv(row["persona_ids"]),
    )


def row_to_note(row: sqlite3.Row) -> Note:
    """note 表行 → Note 实体。

    PERSONA_BASICS 类型的 content 仅当看起来像 JSON（首字符为 {）时自动解析。
    实际数据中有时存的是原始文本（PersonaService.create_persona 的 definition），
    有时存的是 JSON 字符串（PersonaEditService.edit 的输出）。本函数采用启发式：
    内容以 '{' 开头才尝试 json.loads。
    """
    raw_content: str = row["content"]
    note_type = NoteType(row["type"])
    content: str | dict[str, Any]
    if note_type == NoteType.PERSONA_BASICS and raw_content.lstrip().startswith("{"):
        try:
            content = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError):
            content = {"_raw": raw_content}
    else:
        content = raw_content

    return Note(
        id=row["id"],
        user_id=row["user_id"],
        persona_id=row["persona_id"],
        type=note_type,
        content=content,
        source_quote=row["source_quote"],
        importance=row["importance"],
        decay_score=row["decay_score"],
        is_archived=bool(row["is_archived"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_accessed_at=row["last_accessed_at"],
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