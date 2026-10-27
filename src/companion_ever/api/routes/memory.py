"""api/routes/memory.py — 记忆相关的只读接口。

前端"她记得什么"面板用：
- GET /api/users                                    所有用户 + 角色
- GET /api/persona/{user}/{persona}                角色总览（人设 + 关系状态 + 计数）
- GET /api/persona/{user}/{persona}/notes          记忆列表
- GET /api/persona/{user}/{persona}/blocks         3 个 memory block
- GET /api/persona/{user}/{persona}/history        最近对话
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

from companion_ever.app import AppContext
from companion_ever.api.deps import check_pin, resolve_target
from companion_ever.api.schemas import (
    BlockOut,
    MessageOut,
    NoteOut,
    PersonaSummary,
    UserOut,
)
from companion_ever.domain.enums import NoteType

router = APIRouter()


def _ctx_and_cfg(request: Request, x_pin: str) -> tuple[AppContext, Any]:
    ctx: AppContext = request.app.state.ctx
    cfg = request.app.state.web_cfg
    check_pin(x_pin, cfg)
    return ctx, cfg


def _s(value: Any) -> Optional[str]:
    """时间戳统一转字符串（DB 里本来就是 SQLite 的 UTC 字符串）。"""
    if value is None:
        return None
    return str(value)


@router.get("/api/users", response_model=list[UserOut])
def list_users(request: Request, x_pin: str = Header(default="")) -> list[UserOut]:
    """所有用户 + 他们拥有的 persona。"""
    ctx, cfg = _ctx_and_cfg(request, x_pin)
    return [
        UserOut(id=u.id, personas=ctx.repos.user.list_personas(u.id))
        for u in ctx.repos.user.list_all()
    ]


@router.get("/api/persona/{user_id}/{persona_id}", response_model=PersonaSummary)
def persona_summary(
    request: Request,
    user_id: str,
    persona_id: str,
    x_pin: str = Header(default=""),
) -> PersonaSummary:
    """角色总览：人设宪法 + 3 个 block + 计数。"""
    ctx, _ = _ctx_and_cfg(request, x_pin)
    if not ctx.repos.user.owns_persona(user_id, persona_id):
        raise HTTPException(status_code=404, detail=f"用户 '{user_id}' 没有 persona '{persona_id}'")

    # 人设宪法（PERSONA_BASICS 类型的 note）
    basics: dict[str, Any] = {}
    basics_notes = ctx.services.note.find_notes(
        user_id=user_id,
        persona_id=persona_id,
        type=NoteType.PERSONA_BASICS,
        limit=1,
    )
    if basics_notes:
        content = basics_notes[0].content
        if isinstance(content, dict):
            basics = content
        else:
            try:
                basics = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                basics = {"_raw": content}

    blocks = [
        BlockOut(block_type=b.block_type.value, fields=b.fields)
        for b in ctx.services.block.get_blocks(user_id, persona_id)
    ]

    notes = ctx.services.note.find_notes(
        user_id=user_id, persona_id=persona_id, limit=1000,
    )
    markers = ctx.services.conversation.get_last_n_session_closes(
        user_id=user_id, persona_id=persona_id, n=1000,
    )
    last_at = ctx.services.conversation.get_last_conversation_at(user_id, persona_id)

    return PersonaSummary(
        user_id=user_id,
        persona_id=persona_id,
        persona_basics=basics,
        blocks=blocks,
        note_count=len(notes),
        message_count=ctx.services.conversation.count(user_id, persona_id),
        session_count=len(markers),
        last_conversation_at=_s(last_at),
    )


@router.get("/api/persona/{user_id}/{persona_id}/notes", response_model=list[NoteOut])
def list_notes(
    request: Request,
    user_id: str,
    persona_id: str,
    x_pin: str = Header(default=""),
    limit: int = Query(default=100, ge=1, le=1000),
    type: Optional[str] = Query(default=None),
) -> list[NoteOut]:
    """记忆列表（最新优先）。type 可选，按 note 类型过滤。"""
    ctx, _ = _ctx_and_cfg(request, x_pin)
    note_type: Optional[NoteType] = None
    if type:
        try:
            note_type = NoteType(type)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"未知的 note 类型: {type}") from e

    notes = ctx.services.note.find_notes(
        user_id=user_id,
        persona_id=persona_id,
        type=note_type,
        limit=limit,
    )
    return [
        NoteOut(
            id=n.id,
            type=n.type.value,
            content=n.content if isinstance(n.content, str) else json.dumps(n.content, ensure_ascii=False),
            importance=n.importance,
            source_quote=n.source_quote,
            created_at=_s(n.created_at),
            updated_at=_s(n.updated_at),
        )
        for n in notes
    ]


@router.get("/api/persona/{user_id}/{persona_id}/blocks", response_model=list[BlockOut])
def list_blocks(
    request: Request,
    user_id: str,
    persona_id: str,
    x_pin: str = Header(default=""),
) -> list[BlockOut]:
    """3 个 memory block（运行时状态 / 用户画像 / 关系进展）。"""
    ctx, _ = _ctx_and_cfg(request, x_pin)
    return [
        BlockOut(block_type=b.block_type.value, fields=b.fields)
        for b in ctx.services.block.get_blocks(user_id, persona_id)
    ]


@router.get("/api/persona/{user_id}/{persona_id}/history", response_model=list[MessageOut])
def list_history(
    request: Request,
    user_id: str,
    persona_id: str,
    x_pin: str = Header(default=""),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[MessageOut]:
    """最近对话（时间正序，已过滤掉 session_close marker）。"""
    ctx, _ = _ctx_and_cfg(request, x_pin)
    messages = ctx.services.conversation.get_recent_messages(
        user_id=user_id, persona_id=persona_id, limit=limit,
    )
    return [
        MessageOut(role=m.role, content=m.content, created_at=_s(m.created_at))
        for m in messages
    ]


__all__ = ["router"]
