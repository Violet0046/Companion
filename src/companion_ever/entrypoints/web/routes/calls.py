"""Authenticated, read-only model-call diagnostics; statistics use the same filters as rows."""
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from companion_ever.entrypoints.web.deps import resolve_target
from companion_ever.infrastructure.tracing import summarize_calls


router = APIRouter()


@router.get('/api/usage')
def account_usage(request: Request, date: str = ''):
    try:
        return request.app.state.ctx.services.usage.summary(request.state.account['user_id'], date or None)
    except ValueError as exc:
        raise HTTPException(400, '日期必须使用 YYYY-MM-DD') from exc


def _records(request, day, user_id, persona_id, layer, status, job_id):
    ctx = request.app.state.ctx
    cfg = request.app.state.web_cfg
    user, persona = resolve_target(ctx, cfg, request.state.account['user_id'], persona_id)
    ctx.services.note.authorizer.require_persona(user, persona)
    day = day or ctx.call_logs.today()
    try:
        records = ctx.call_logs.read(day, user_id=user, persona_id=persona,
            layer=layer, status=status, job_id=job_id)
    except ValueError as exc:
        raise HTTPException(400, '日期必须使用 YYYY-MM-DD') from exc
    return ctx.call_logs, day, user, persona, records


@router.get('/api/calls')
def calls(request: Request, date: str = '', user_id: str = '', persona_id: str = '',
          layer: Literal['chat', 'extraction', 'summary'] | None = None,
          status: Literal['ok', 'error', 'aborted'] | None = None, job_id: str | None = None,
          limit: int = Query(200, ge=1, le=500)):
    store, day, user, persona, records = _records(request, date, user_id, persona_id, layer, status, job_id)
    return {'date': day, 'dates': [d for d in store.dates() if store.read(d, user_id=user, persona_id=persona)], 'user_id': user, 'persona_id': persona,
            'summary': summarize_calls(records), 'records': records[:limit],
            'truncated': len(records) > limit, 'write_error': store.write_error}


@router.get('/api/calls/export')
def export(request: Request, date: str = '', user_id: str = '', persona_id: str = '',
           layer: Literal['chat', 'extraction', 'summary'] | None = None,
           status: Literal['ok', 'error', 'aborted'] | None = None, job_id: str | None = None):
    _, day, _, _, records = _records(request, date, user_id, persona_id, layer, status, job_id)
    return StreamingResponse((json.dumps(row, ensure_ascii=False) + '\n' for row in reversed(records)),
        media_type='application/x-ndjson', headers={'Content-Disposition': f'attachment; filename="{day}.jsonl"'})
