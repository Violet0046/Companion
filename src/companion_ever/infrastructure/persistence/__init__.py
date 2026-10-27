"""infrastructure/persistence — SQLite 持久化层。"""
from __future__ import annotations

from .block_repo import BlockRepo
from .conversation_repo import ConversationRepo
from .db import apply_schema, connect, init_db
from .mappers import (
    row_to_block,
    row_to_conversation_message,
    row_to_note,
    row_to_user,
)
from .note_repo import NoteRepo
from .user_repo import UserRepo

__all__ = [
    "BlockRepo",
    "ConversationRepo",
    "UserRepo",
    "NoteRepo",
    "init_db",
    "connect",
    "apply_schema",
    "row_to_block",
    "row_to_conversation_message",
    "row_to_note",
    "row_to_user",
]