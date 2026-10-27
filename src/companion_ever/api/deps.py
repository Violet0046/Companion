"""api/deps.py — FastAPI 依赖：取 AppContext、鉴权、解析 user/persona。"""
from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket

from companion_ever.app import AppContext
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


def check_pin(pin: str, cfg: WebConfig) -> None:
    """校验 PIN。cfg.pin 为空 → 不鉴权（仅建议内网 / 本机使用）。"""
    if not cfg.pin:
        return
    if pin != cfg.pin:
        raise HTTPException(status_code=401, detail="PIN 不正确")


def resolve_target(
    ctx: AppContext,
    cfg: WebConfig,
    user_id: str = "",
    persona_id: str = "",
) -> tuple[str, str]:
    """解析出要操作的 (user_id, persona_id)。

    优先级：
    1. 显式传入的 user_id / persona_id
    2. WebConfig 里配置的默认 user / persona
    3. 数据库里的第一个用户 + 它的第一个 persona

    Raises:
        HTTPException: 找不到任何 user / persona
    """
    user_id = user_id or cfg.user
    persona_id = persona_id or cfg.persona

    if not user_id:
        users = ctx.repos.user.list_all()
        if not users:
            raise HTTPException(
                status_code=404,
                detail="库里还没有用户，先跑 companion-cli setup 创建",
            )
        user_id = users[0].id

    if not persona_id:
        personas = ctx.repos.user.list_personas(user_id)
        if not personas:
            raise HTTPException(
                status_code=404,
                detail=f"用户 '{user_id}' 还没有 persona，先跑 companion-cli setup",
            )
        persona_id = personas[0]

    return user_id, persona_id


__all__ = [
    "get_ctx",
    "get_web_cfg",
    "check_pin",
    "resolve_target",
]
