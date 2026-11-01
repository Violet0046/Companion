"""api/schemas.py — HTTP 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatIn(BaseModel):
    """发送一条消息。"""

    content: str = Field(min_length=1)
    client_message_id: str | None = Field(default=None, max_length=128)
    user_id: str = ""
    persona_id: str = ""


class CloseSessionIn(BaseModel):
    """结束当前 session（写 sentinel + 触发记忆抽取）。"""

    user_id: str = ""
    persona_id: str = ""


class CloseSessionOut(BaseModel):
    """结束 session 的结果——告诉前端"TA 记住了什么"。

    started=True 表示**已经丢到后台执行**，请求立刻就返回了
    （抽取是一次完整 LLM 调用，要几十秒，不能让用户在那儿干等）。
    前端靠轮询 last_record_at 来知道"记好了没有"。
    """

    started: bool = True
    marker_id: int = 0
    facts_created: int = 0
    blocks_updated: int = 0
    conversations_processed: int = 0
    errors: list[str] = Field(default_factory=list)


class NoteOut(BaseModel):
    """一条记忆。"""

    id: str
    kind: str
    content: str
    importance: float
    revision: int = 1
    reality: str = 'uncertain'
    event_time: Optional[str] = None
    time_basis: str = ''
    source_quote: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class NoteEditIn(BaseModel):
    content: str = Field(min_length=1)
    revision: int = Field(ge=1)


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
    client_message_id: Optional[str] = None


class DateEntry(BaseModel):
    """有对话的一天。"""

    date: str
    count: int
    # 当天第一条 / 最后一条消息的 id（日历跳转定位用）
    first_id: Optional[int] = None
    last_id: Optional[int] = None


class SearchHit(BaseModel):
    """搜索结果项。"""

    kind: str = Field(description="message | memory")
    id: str
    content: str
    created_at: Optional[str] = None
    role: Optional[str] = None
    memory_kind: Optional[str] = None
    importance: Optional[float] = None


class ReadIn(BaseModel):
    message_id: int = Field(ge=0)


class ProactiveIn(BaseModel):
    enabled: bool


class SettingsOut(BaseModel):
    """用户设置。"""

    show_thinking: bool = True


class SettingsIn(BaseModel):
    """修改用户设置。"""

    show_thinking: bool = True


class PersonaCreateIn(BaseModel):
    """创建角色。

    两条路径：用 template_id 走内置模板，或用 definition 传自己填的 JSON。
    """

    persona_id: str
    template_id: str = ""
    definition: Optional[str] = None


class PersonaSummary(BaseModel):
    """角色总览——前端顶栏 + "TA 记得什么"面板用。"""

    user_id: str
    persona_id: str
    persona_basics: dict[str, Any] = Field(default_factory=dict)
    blocks: list[BlockOut] = Field(default_factory=list)
    note_count: int = 0
    message_count: int = 0
    session_count: int = 0
    last_conversation_at: Optional[str] = None
    # 最近一次「记录」（session_close marker）的时间。
    # 前端用它显示"上次记录：3 天前"，并提供手动记一次的入口。
    last_record_at: Optional[str] = None
    # 现在是不是正在记录中（抽取跑着）。
    # 前端用它把「记一次」按钮变成「记录中…」，也用来判断记录是否已完成。
    recording: bool = False
    summary_meta: Optional[dict[str,Any]] = None
    memory_meta: Optional[dict[str,Any]] = None


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
