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
    DateEntry,
    MessageOut,
    NoteOut,
    PersonaSummary,
    SearchHit,
    SettingsIn,
    SettingsOut,
    UserOut,
)
from companion_ever.domain.enums import NoteType
from companion_ever.utils.timeutil import utc_ts_to_local

router = APIRouter()


def _ctx_and_cfg(request: Request, x_pin: str) -> tuple[AppContext, Any]:
    ctx: AppContext = request.app.state.ctx
    cfg = request.app.state.web_cfg
    check_pin(x_pin, cfg)
    return ctx, cfg


def _s(value: Any, tz: int = 0) -> Optional[str]:
    """时间戳转字符串，并**转成用户本地时区**。

    DB 里存的是 SQLite 的 UTC 时间（``CURRENT_TIMESTAMP`` 返回 UTC）。
    出库一律转本地时区 —— 否则前端会把 UTC 当本地显示，
    东八区就会看到"8 小时前"这种错位。
    """
    if value is None:
        return None
    if tz:
        return utc_ts_to_local(str(value), tz) or str(value)
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
    tz = ctx.cfg.tz_offset_hours
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
        last_conversation_at=_s(last_at, tz),
        # markers 是时间升序，最后一个就是最近一次记录
        last_record_at=_s(markers[-1].created_at, tz) if markers else None,
        # 现在正在记录吗？（前端用来显示「记录中…」）
        recording=ctx.services.session_close.is_closing(user_id, persona_id),
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

    tz = ctx.cfg.tz_offset_hours
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
            created_at=_s(n.created_at, tz),
            updated_at=_s(n.updated_at, tz),
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
    before_id: Optional[int] = Query(
        default=None, description="取这之前的（上翻加载）",
    ),
    after_id: Optional[int] = Query(
        default=None, description="取这之后的（从某条往后追）",
    ),
    date: Optional[str] = Query(
        default=None, description="只看某一天（YYYY-MM-DD）",
    ),
    include_thinking: Optional[str] = Query(
        default=None,
        description="auto（跟随用户设置）/ 1 强制包含 / 0 强制不包含",
    ),
) -> list[MessageOut]:
    """按游标分页的对话历史（时间正序）。

    - 不传游标 → 最新的 limit 条（首次加载）
    - before_id → 紧跟着它上面的 limit 条（上翻）
    - after_id → 从它往后的 limit 条（搜索结果跳转）
    - date → 只看某天

    思考过程（thinking）默认跟随用户的 show_thinking 设置；
    此到此，**prompt 里从不会出现 think 内容**（它另存在 thinking_log）。
    """
    ctx, _ = _ctx_and_cfg(request, x_pin)
    tz = ctx.cfg.tz_offset_hours

    want_thinking = ctx.services.settings.get_show_thinking(user_id, persona_id)
    if include_thinking is not None:
        want_thinking = include_thinking not in ("0", "false", "off", "no")

    messages = ctx.services.conversation.get_page(
        user_id=user_id, persona_id=persona_id,
        before_id=before_id, after_id=after_id, date=date, limit=limit,
        tz_offset_hours=tz,
    )

    thinking_map: dict[int, str] = {}
    if want_thinking:
        thinking_map = ctx.services.conversation.get_thinking_map(
            user_id=user_id, persona_id=persona_id,
            message_ids=[m.id for m in messages],
        )

    return [
        MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            created_at=_s(m.created_at, tz),
            thinking=thinking_map.get(m.id),
        )
        for m in messages
    ]


# ============ 历史检索（微信式）============

@router.get("/api/persona/{user_id}/{persona_id}/dates", response_model=list[DateEntry])
def list_dates(
    request: Request,
    user_id: str,
    persona_id: str,
    x_pin: str = Header(default=""),
) -> list[DateEntry]:
    """有对话的日期（降序）+ 每天的消息数。"""
    ctx, _ = _ctx_and_cfg(request, x_pin)
    tz = ctx.cfg.tz_offset_hours
    return [
        DateEntry(
            date=d["date"], count=d["count"],
            first_id=d.get("first_id"), last_id=d.get("last_id"),
        )
        for d in ctx.services.conversation.list_dates(
            user_id, persona_id, tz_offset_hours=tz,
        )
    ]


@router.get("/api/persona/{user_id}/{persona_id}/search", response_model=list[SearchHit])
def search_history(
    request: Request,
    user_id: str,
    persona_id: str,
    q: str = Query(description="关键词"),
    scope: str = Query(default="all", description="all | messages | memories"),
    limit: int = Query(default=50, ge=1, le=200),
    x_pin: str = Header(default=""),
) -> list[SearchHit]:
    """搜索对话记录 + 记忆条目。

    关键词抽取与记忆检索同一套逻辑（utils/text.py）。
    """
    ctx, _ = _ctx_and_cfg(request, x_pin)
    tz = ctx.cfg.tz_offset_hours
    q = (q or "").strip()
    if not q:
        return []

    hits: list[SearchHit] = []

    if scope in ("all", "messages"):
        for m in ctx.services.conversation.search_messages(user_id, persona_id, q, limit=limit):
            hits.append(SearchHit(
                kind="message", id=str(m.id), content=m.content,
                created_at=_s(m.created_at, tz), role=m.role,
            ))

    if scope in ("all", "memories"):
        for n in ctx.services.note.search_notes(user_id, persona_id, q, limit=limit):
            content = n.content if isinstance(n.content, str) else json.dumps(n.content, ensure_ascii=False)
            hits.append(SearchHit(
                kind="memory", id=n.id, content=content,
                created_at=None, type=n.type.value, importance=n.importance,
            ))

    return hits[: limit * 2]


# ============ 设置 ============

@router.get("/api/settings/{user_id}/{persona_id}", response_model=SettingsOut)
def get_settings(
    request: Request,
    user_id: str,
    persona_id: str,
    x_pin: str = Header(default=""),
) -> SettingsOut:
    """读设置（当前只有 show_thinking）。"""
    ctx, _ = _ctx_and_cfg(request, x_pin)
    return SettingsOut(**ctx.services.settings.get_settings(user_id, persona_id))


@router.put("/api/settings/{user_id}/{persona_id}", response_model=SettingsOut)
def put_settings(
    request: Request,
    user_id: str,
    persona_id: str,
    payload: SettingsIn,
    x_pin: str = Header(default=""),
) -> SettingsOut:
    """改设置（当前只有 show_thinking）。"""
    ctx, _ = _ctx_and_cfg(request, x_pin)
    return SettingsOut(**ctx.services.settings.set_show_thinking(
        user_id, persona_id, payload.show_thinking,
    ))


__all__ = ["router"]
