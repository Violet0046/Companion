"""api/deps.py — FastAPI 依赖：取 AppContext、鉴权、解析 user/persona。"""
from __future__ import annotations

from fastapi import HTTPException, Request

from companion_ever.bootstrap.app import AppContext
from companion_ever.config import WebConfig


def get_ctx(request: Request) -> AppContext:
    """从 app.state 取 AppContext。"""
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        raise HTTPException(status_code=500, detail="AppContext 未初始化")
    return ctx


def get_web_cfg(request: Request) -> WebConfig:
    """从 app.state 取 WebConfig。"""
    cfg = getattr(request.app.state, "web_cfg", None)
    if cfg is None:
        raise HTTPException(status_code=500, detail="WebConfig 未初始化")
    return cfg


def resolve_target(
    ctx: AppContext,
    cfg: WebConfig,
    user_id: str = "",
    persona_id: str = "",
) -> tuple[str, str]:
    """Resolve a persona inside an already authenticated user's namespace."""
    if not user_id:
        raise HTTPException(401, '请先登录')
    if not persona_id and cfg.user == user_id:
        persona_id = cfg.persona
    if not persona_id:
        personas = ctx.repos.user.list_personas(user_id)
        if not personas:
            raise HTTPException(
                status_code=404,
                detail="还没有角色，请先在通讯录中创建",
            )
        persona_id = personas[0]

    return user_id, persona_id


__all__ = [
    "get_ctx",
    "get_web_cfg",
    "resolve_target",
]
