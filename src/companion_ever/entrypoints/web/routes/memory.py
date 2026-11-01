"""记忆查询、显式编辑/删除与后台维护接口。

前端"TA 记得什么"面板用：
- GET /api/users                                    所有用户 + 角色
- GET /api/persona/{user}/{persona}                角色总览（人设 + 关系状态 + 计数）
- GET /api/persona/{user}/{persona}/notes          记忆列表
- PATCH /api/persona/{user}/{persona}/notes/{id}  编辑正文（检查版本）
- DELETE /api/persona/{user}/{persona}/notes/{id} 删除有效记忆（检查版本）
- GET /api/persona/{user}/{persona}/blocks         用户画像与关系进展摘要
- GET /api/persona/{user}/{persona}/history        最近对话
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from companion_ever.bootstrap.app import AppContext
from companion_ever.entrypoints.web.schemas import (
    BlockOut,
    DateEntry,
    MessageOut,
    NoteOut,
    NoteEditIn,
    PersonaSummary,
    SearchHit,
    SettingsIn,
    SettingsOut,
    UserOut,
)
from companion_ever.domain.enums import MemoryKind
from companion_ever.utils.timeutil import utc_ts_to_local
from companion_ever.memory.retrieval.query import MemorySearch
from companion_ever.memory.notes.service import NoteConflictError

router = APIRouter()


@router.get('/api/persona/{user_id}/{persona_id}/memory-jobs')
def memory_jobs(request: Request,user_id: str,persona_id: str):
    ctx,_=_ctx_and_cfg(request)
    return ctx.services.session_close.recent_jobs(user_id,persona_id)


@router.post('/api/persona/{user_id}/{persona_id}/memory-jobs/{job_id}/retry')
def retry_memory_job(request: Request,user_id: str,persona_id: str,job_id: str):
    ctx,_=_ctx_and_cfg(request)
    try:
        return ctx.services.session_close.retry_job(user_id,persona_id,job_id)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.get('/api/persona/{user_id}/{persona_id}/task-attempts')
def task_attempts(request: Request,user_id: str,persona_id: str,
                  kind: Optional[str]=Query(default=None,pattern='^(memory|summary)$'),
                  job_id: Optional[str]=Query(default=None)):
    ctx,_=_ctx_and_cfg(request)
    return ctx.services.session_close.task_attempts(user_id,persona_id,kind,job_id)


@router.post('/api/persona/{user_id}/{persona_id}/summaries/refresh')
def refresh_summaries(request: Request,user_id: str,persona_id: str):
    ctx,_=_ctx_and_cfg(request)
    return ctx.services.summary.request(user_id,persona_id)


@router.get('/api/persona/{user_id}/{persona_id}/summary-status')
def summary_status(request: Request,user_id: str,persona_id: str):
    ctx,_=_ctx_and_cfg(request)
    return ctx.services.summary.status(user_id,persona_id)


@router.get('/api/persona/{user_id}/{persona_id}/summary-jobs')
def summary_jobs(request: Request,user_id: str,persona_id: str):
    ctx,_=_ctx_and_cfg(request)
    return ctx.services.summary.recent_jobs(user_id,persona_id)


@router.get('/api/persona/{user_id}/{persona_id}/fact-changes')
def fact_changes(request: Request,user_id: str,persona_id: str,after_id: int=Query(default=0,ge=0),
                 limit: int=Query(default=50,ge=1,le=200)):
    ctx,_=_ctx_and_cfg(request)
    return ctx.services.summary.fact_changes(user_id,persona_id,after_id,limit)

@router.post('/api/persona/{user_id}/{persona_id}/memories/search')
def search_memories(request: Request, user_id: str, persona_id: str,
                    payload: MemorySearch):
    """Shared read-only search. This is not an LLM tool-call endpoint."""
    ctx, _ = _ctx_and_cfg(request)
    return ctx.services.retrieval.search(user_id, persona_id, payload).to_dict()


def _ctx_and_cfg(request: Request) -> tuple[AppContext, Any]:
    ctx: AppContext = request.app.state.ctx
    cfg = request.app.state.web_cfg
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
def list_users(request: Request) -> list[UserOut]:
    """当前登录用户及其有效角色。"""
    ctx, cfg = _ctx_and_cfg(request)
    user = request.state.account['user_id']
    return [UserOut(id=user, personas=ctx.services.persona.list_user_personas(user))]


@router.get("/api/persona/{user_id}/{persona_id}", response_model=PersonaSummary)
def persona_summary(
    request: Request,
    user_id: str,
    persona_id: str,
) -> PersonaSummary:
    """角色总览：角色配置、两种摘要与计数。"""
    ctx, _ = _ctx_and_cfg(request)
    tz = ctx.cfg.tz_offset_hours
    if not ctx.services.persona.exists(user_id, persona_id):
        raise HTTPException(status_code=404, detail=f"用户 '{user_id}' 没有 persona '{persona_id}'")

    row = ctx.services.persona.get_definition(user_id,persona_id)
    try:
        basics = json.loads(row['content']) if row else {}
    except (ValueError,TypeError):
        basics = {'_raw':row['content']}

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
    record_status = ctx.services.session_close.status(user_id,persona_id)
    return PersonaSummary(
        user_id=user_id,
        persona_id=persona_id,
        persona_basics=basics,
        blocks=blocks,
        note_count=len(notes),
        message_count=ctx.services.conversation.count(user_id, persona_id),
        session_count=len(markers),
        last_conversation_at=_s(last_at, tz),
        # marker 仅统计会话边界；整理完成时间来自成功的持久任务。
        last_record_at=_s(record_status['last_record_at'], tz),
        # 现在正在记录吗？（前端用来显示「记录中…」）
        recording=ctx.services.session_close.is_closing(user_id, persona_id),
        summary_meta=ctx.services.summary.status(user_id,persona_id),
        memory_meta=record_status['memory_meta'],
    )


@router.get("/api/persona/{user_id}/{persona_id}/notes", response_model=list[NoteOut])
def list_notes(
    request: Request,
    user_id: str,
    persona_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    kind: Optional[str] = Query(default=None),
) -> list[NoteOut]:
    """记忆列表（最新优先），只支持 episode/fact。"""
    ctx, _ = _ctx_and_cfg(request)
    if 'type' in request.query_params:
        raise HTTPException(400,'旧分类 type 已移除，请使用 kind=episode 或 kind=fact')
    note_kind: Optional[MemoryKind] = None
    if kind:
        try:
            note_kind = MemoryKind(kind)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"未知的 note 类型: {kind}") from e

    tz = ctx.cfg.tz_offset_hours
    notes = ctx.services.note.find_notes(
        user_id=user_id,
        persona_id=persona_id,
        kind=note_kind,
        limit=limit,
    )
    return [_note_out(n,tz) for n in notes]


def _note_out(n, tz):
    return NoteOut(
        id=n.id,
        kind=n.kind.value,
        reality=n.reality, event_time=n.event_time, time_basis=n.time_basis,
        content=n.content if isinstance(n.content, str) else json.dumps(n.content, ensure_ascii=False),
        importance=n.importance,
        revision=n.revision,
        source_quote=n.source_quote,
        created_at=_s(n.created_at, tz),
        updated_at=_s(n.updated_at, tz),
    )


@router.patch('/api/persona/{user_id}/{persona_id}/notes/{note_id}', response_model=NoteOut)
def edit_note(request: Request, user_id: str, persona_id: str, note_id: str,
              payload: NoteEditIn):
    ctx,_=_ctx_and_cfg(request)
    try:
        note=ctx.services.note.edit_content(user_id,persona_id,note_id,payload.content,payload.revision)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except NoteConflictError as exc:
        raise HTTPException(409,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    return _note_out(note,ctx.cfg.tz_offset_hours)


@router.delete('/api/persona/{user_id}/{persona_id}/notes/{note_id}')
def delete_note(request: Request, user_id: str, persona_id: str, note_id: str,
                revision: int=Query(ge=1)):
    ctx,_=_ctx_and_cfg(request)
    try:
        ctx.services.note.delete_note(user_id,persona_id,note_id,revision)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except NoteConflictError as exc:
        raise HTTPException(409,str(exc)) from exc
    return {'id':note_id,'deleted':True}


@router.get("/api/persona/{user_id}/{persona_id}/blocks", response_model=list[BlockOut])
def list_blocks(
    request: Request,
    user_id: str,
    persona_id: str,
) -> list[BlockOut]:
    """用户画像与关系进展两个摘要。"""
    ctx, _ = _ctx_and_cfg(request)
    return [
        BlockOut(block_type=b.block_type.value, fields=b.fields)
        for b in ctx.services.block.get_blocks(user_id, persona_id)
    ]


@router.get("/api/persona/{user_id}/{persona_id}/history", response_model=list[MessageOut])
def list_history(
    request: Request,
    user_id: str,
    persona_id: str,
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
    ctx, _ = _ctx_and_cfg(request)
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
            client_message_id=m.client_message_id,
        )
        for m in messages
    ]


@router.get('/api/persona/{user_id}/{persona_id}/unread-count')
def unread_count(request: Request,user_id: str,persona_id: str,
                 after_id: int=Query(default=0,ge=0)):
    ctx,_=_ctx_and_cfg(request)
    return ctx.services.conversation.unread_count(user_id,persona_id,after_id)


# ============ 历史检索（微信式）============

@router.get("/api/persona/{user_id}/{persona_id}/dates", response_model=list[DateEntry])
def list_dates(
    request: Request,
    user_id: str,
    persona_id: str,
) -> list[DateEntry]:
    """有对话的日期（降序）+ 每天的消息数。"""
    ctx, _ = _ctx_and_cfg(request)
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
) -> list[SearchHit]:
    """搜索对话记录 + 记忆条目。

    关键词抽取与记忆检索同一套逻辑（utils/text.py）。
    """
    ctx, _ = _ctx_and_cfg(request)
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
                created_at=None, memory_kind=n.kind.value, importance=n.importance,
            ))

    return hits[: limit * 2]


# ============ 设置 ============

@router.get("/api/settings/{user_id}/{persona_id}", response_model=SettingsOut)
def get_settings(
    request: Request,
    user_id: str,
    persona_id: str,
) -> SettingsOut:
    """读设置（当前只有 show_thinking）。"""
    ctx, _ = _ctx_and_cfg(request)
    return SettingsOut(**ctx.services.settings.get_settings(user_id, persona_id))


@router.put("/api/settings/{user_id}/{persona_id}", response_model=SettingsOut)
def put_settings(
    request: Request,
    user_id: str,
    persona_id: str,
    payload: SettingsIn,
) -> SettingsOut:
    """改设置（当前只有 show_thinking）。"""
    ctx, _ = _ctx_and_cfg(request)
    return SettingsOut(**ctx.services.settings.set_show_thinking(
        user_id, persona_id, payload.show_thinking,
    ))


__all__ = ["router"]
