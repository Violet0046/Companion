"""entrypoints/web/routes/persona.py — 角色的创建、删除（停用）与恢复。

前端"新建角色"与角色资料面板用：
- GET    /api/persona-templates                 模板列表 + 自定义表单字段描述
- POST   /api/persona/{user}                    创建（用模板或自己填的 JSON）
- DELETE /api/persona/{user}/{persona}          删除（= 停用：撤销访问，保留数据）
- POST   /api/persona/{user}/{persona}/restore  恢复已删除的角色
- GET    /api/archived-personas/{user}          已删除的角色清单

路由只做输入转换与错误映射，业务规则在 characters/ 里。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from companion_ever.entrypoints.web.schemas import PersonaCreateIn

router = APIRouter()


def _ctx_and_cfg(request: Request) -> tuple[Any, Any]:
    ctx: Any = request.app.state.ctx
    cfg: Any = request.app.state.web_cfg
    return ctx, cfg


def _fail(exc: Exception) -> HTTPException:
    """业务异常 → HTTP 状态码。"""
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=400, detail=str(exc).strip("'"))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/api/persona-templates")
def persona_templates(request: Request) -> dict:
    """角色模板目录 + 自定义表单的字段描述。"""
    ctx, _ = _ctx_and_cfg(request)
    return ctx.services.persona.templates()


@router.post("/api/persona/{user_id}")
def create_persona(
    request: Request,
    user_id: str,
    payload: PersonaCreateIn,
) -> dict:
    """创建角色。

    两条路径：用 template_id 走内置模板，或用 definition 传自己填的 JSON。
    """
    ctx, _ = _ctx_and_cfg(request)
    persona_id = (payload.persona_id or "").strip()
    try:
        persona_id=ctx.services.persona.create_named(user_id,persona_id,payload.definition,payload.template_id)
    except Exception as exc:
        raise _fail(exc)
    return {"user_id": user_id, "persona_id": persona_id, "status": "created"}


@router.delete("/api/persona/{user_id}/{persona_id}")
def delete_persona(
    request: Request,
    user_id: str,
    persona_id: str,
) -> dict:
    """删除角色 = 停用：撤销访问与在途执行，数据全部保留，可恢复。"""
    ctx, _ = _ctx_and_cfg(request)
    try:
        ctx.services.persona.deactivate(user_id, persona_id)
    except Exception as exc:
        raise _fail(exc)
    return {"user_id": user_id, "persona_id": persona_id, "status": "deactivated"}


@router.post("/api/persona/{user_id}/{persona_id}/restore")
def restore_persona(
    request: Request,
    user_id: str,
    persona_id: str,
) -> dict:
    """恢复已删除（停用）的角色。"""
    ctx, _ = _ctx_and_cfg(request)
    try:
        ctx.services.persona.restore(user_id, persona_id)
    except Exception as exc:
        raise _fail(exc)
    return {"user_id": user_id, "persona_id": persona_id, "status": "restored"}


@router.get("/api/archived-personas/{user_id}")
def archived_personas(
    request: Request,
    user_id: str,
) -> list[dict]:
    """已删除（停用）的角色清单——设定还在，可以恢复。"""
    ctx, _ = _ctx_and_cfg(request)
    try:
        return ctx.services.persona.list_archived(user_id)
    except Exception as exc:
        raise _fail(exc)


@router.delete('/api/persona/{user_id}/{persona_id}/permanent')
def purge_persona(request:Request,user_id:str,persona_id:str):
    ctx,_=_ctx_and_cfg(request)
    try:ctx.services.persona.purge(user_id,persona_id)
    except Exception as exc:raise _fail(exc)
    return {'status':'deleted','persona_id':persona_id}
