"""api/schemas.py — HTTP 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatIn(BaseModel):
    """发送一条消息。"""

    content: str = Field(min_length=1)
    user_id: str = ""
    persona_id: str = ""


class CloseSessionIn(BaseModel):
    """结束当前 session（写 sentinel + 触发记忆抽取）。"""

    user_id: str = ""
    persona_id: str = ""


class CloseSessionOut(BaseModel):
    """结束 session 的结果——告诉前端"她记住了什么"。"""

    marker_id: int
    facts_created: int
    blocks_updated: int
    conversations_processed: int
    errors: list[str] = Field(default_factory=list)


class NoteOut(BaseModel):
    """一条记忆。"""

    id: str
    type: str
    content: str
    importance: float
    source_quote: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BlockOut(BaseModel):
    """一个 memory block。"""

    block_type: str
    fields: dict[str, Any] = Field(default_factory=dict)


class MessageOut(BaseModel):
    """一条历史消息。

    thinking: 模型的思考过程（仅 assistant 消息可能有）。
    是否返回由用户的 show_thinking 设置控制，
    也可用 include_thinking 参数强制指定。
    """

    id: int = Field(description="消息 id，用作分页游标（before_id / after_id）")
    role: str
    content: str
    created_at: Optional[str] = None
    thinking: Optional[str] = None


class DateEntry(BaseModel):
    """有对话的一天。"""

    date: str
    count: int


class SearchHit(BaseModel):
    """搜索结果项。"""

    kind: str = Field(description="message | memory")
    id: str
    content: str
    created_at: Optional[str] = None
    role: Optional[str] = None
    type: Optional[str] = None
    importance: Optional[float] = None


class SettingsOut(BaseModel):
    """用户设置。"""

    show_thinking: bool = True


class SettingsIn(BaseModel):
    """修改用户设置。"""

    show_thinking: bool = True


class PersonaSummary(BaseModel):
    """角色总览——前端顶栏 + "她记得什么"面板用。"""

    user_id: str
    persona_id: str
    persona_basics: dict[str, Any] = Field(default_factory=dict)
    blocks: list[BlockOut] = Field(default_factory=list)
    note_count: int = 0
    message_count: int = 0
    session_count: int = 0
    last_conversation_at: Optional[str] = None


class UserOut(BaseModel):
    """一个用户 + 他的角色列表。"""

    id: str
    personas: list[str] = Field(default_factory=list)


__all__ = [
    "ChatIn",
    "CloseSessionIn",
    "CloseSessionOut",
    "NoteOut",
    "BlockOut",
    "MessageOut",
    "DateEntry",
    "SearchHit",
    "SettingsOut",
    "SettingsIn",
    "PersonaSummary",
    "UserOut",
]
