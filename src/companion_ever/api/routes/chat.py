"""api/routes/chat.py — 对话相关的 HTTP + WebSocket。

- WS  /ws/chat     流式对话（主通道，手机端用这个）
- POST /api/chat    非流式对话（调试 / 兜底）
- POST /api/session/close  结束 session（写 sentinel + 触发记忆抽取）
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from companion_ever.app import AppContext
from companion_ever.application.chat.assistant_reply import split_assistant_reply
from companion_ever.application.llm_json import strip_thinking
from companion_ever.api.deps import check_pin, resolve_target
from companion_ever.api.schemas import ChatIn, CloseSessionIn, CloseSessionOut
from companion_ever.api.think_filter import ThinkFilter

router = APIRouter()


# ============ 非流式（调试 / 兜底）============

@router.post("/api/chat")
def post_chat(
    request: Request,
    payload: ChatIn,
    x_pin: str = Header(default=""),
) -> dict[str, Any]:
    """一次性拿到完整回复（不走 WebSocket）。"""
    ctx: AppContext = request.app.state.ctx
    cfg = request.app.state.web_cfg
    check_pin(x_pin, cfg)

    user_id, persona_id = resolve_target(ctx, cfg, payload.user_id, payload.persona_id)
    try:
        raw = ctx.services.chat.chat(user_id, persona_id, payload.content)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return {
        "user_id": user_id,
        "persona_id": persona_id,
        "response": strip_thinking(raw),
    }


# ============ 结束 session ============

@router.post("/api/session/close", response_model=CloseSessionOut)
async def post_close_session(
    request: Request,
    payload: CloseSessionIn,
    x_pin: str = Header(default=""),
) -> CloseSessionOut:
    """结束当前 session：写 session_close sentinel + 立即触发记忆抽取。

    对应 CLI 里的 /quit。前端应该做成一个按钮（"这次先聊到这"）。
    """
    ctx: AppContext = request.app.state.ctx
    cfg = request.app.state.web_cfg
    check_pin(x_pin, cfg)

    user_id, persona_id = resolve_target(ctx, cfg, payload.user_id, payload.persona_id)
    try:
        # **后台执行**：抽取是一次完整 LLM 调用（几十秒），
        # 同步等会让用户干等着、没法继续聊天。
        # 前端轮询 last_record_at 来知道记好了没有。
        started = ctx.services.session_close.close_session_background(
            user_id, persona_id,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    return CloseSessionOut(
        started=started,
        errors=[] if started else ["已经在记录中了"],
    )


# ============ WebSocket（主通道）============

@router.websocket("/ws/chat")
async def ws_chat(
    websocket: WebSocket,
    user: str = Query(default=""),
    persona: str = Query(default=""),
    pin: str = Query(default=""),
) -> None:
    """流式对话通道。

    客户端 → 服务端：
        {"type": "chat", "content": "在干嘛"}
        {"type": "close_session"}
        {"type": "ping"}

    服务端 → 客户端：
        {"type": "ready", "user_id": ..., "persona_id": ...}
        {"type": "start", "content": "在干嘛"}
        {"type": "delta", "content": "在等你呀"}
        {"type": "thinking", "content": "本轮的思考过程"}
        {"type": "done"}
        {"type": "error", "message": "..."}
        {"type": "session_closed", "facts_created": 3, ...}
    """
    ctx: AppContext = websocket.app.state.ctx
    cfg = websocket.app.state.web_cfg

    # 鉴权（WebSocket 不能用 HTTPException，要用 close code）
    if cfg.pin and pin != cfg.pin:
        await websocket.close(code=4401, reason="PIN 不正确")
        return

    try:
        user_id, persona_id = resolve_target(ctx, cfg, user, persona)
    except HTTPException as e:
        await websocket.close(code=4404, reason=str(e.detail))
        return

    await websocket.accept()
    await websocket.send_json({
        "type": "ready",
        "user_id": user_id,
        "persona_id": persona_id,
    })

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = (msg or {}).get("type")

            if mtype == "chat":
                content = str((msg or {}).get("content") or "").strip()
                if not content:
                    continue
                await _stream_reply(websocket, ctx, user_id, persona_id, content)

            elif mtype == "close_session":
                await _close_session(websocket, ctx, user_id, persona_id)

            elif mtype == "ping":
                await websocket.send_json({"type": "pong"})

            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"未知消息类型: {mtype!r}",
                })
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001 — 兜底，避免连接悬挂
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
        return


def _extract_thinking(ctx: AppContext, raw: str) -> str | None:
    """从原始回复里取出思考过程。

    复用入库时用的同一个拆分函数，保证「前端看到的」和「数据库存的」是同一份。
    """
    try:
        client = getattr(ctx.services.chat, "llm_client", None)
        reasoning = getattr(client, "last_reasoning", None)
        _body, think = split_assistant_reply(raw, reasoning)
        return think
    except Exception:  # noqa: BLE001
        return None


async def _stream_reply(
    websocket: WebSocket,
    ctx: AppContext,
    user_id: str,
    persona_id: str,
    content: str,
) -> None:
    """把 ChatService.stream_chat 的增量输出推到 WebSocket。

    LLM 调用是同步阻塞的，放到工作线程里跑，用 queue 回传给事件循环。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def producer() -> None:
        try:
            filt = ThinkFilter()
            raw_parts: list[str] = []
            for chunk in ctx.services.chat.stream_chat(user_id, persona_id, content):
                raw_parts.append(chunk)
                delta = filt.feed(chunk)
                if delta:
                    loop.call_soon_threadsafe(queue.put_nowait, ("delta", delta))
            tail = filt.flush()
            if tail:
                loop.call_soon_threadsafe(queue.put_nowait, ("delta", tail))
            # 思考过程单独推一条事件：前端自己决定要不要显示（不显示 != 不保存）
            think = _extract_thinking(ctx, "".join(raw_parts))
            if think:
                loop.call_soon_threadsafe(queue.put_nowait, ("thinking", think))
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

    threading.Thread(target=producer, daemon=True).start()

    await websocket.send_json({"type": "start", "content": content})

    while True:
        kind, payload = await queue.get()
        if kind == "delta":
            await websocket.send_json({"type": "delta", "content": payload})
        elif kind == "thinking":
            await websocket.send_json({"type": "thinking", "content": payload})
        elif kind == "done":
            await websocket.send_json({"type": "done"})
            return
        elif kind == "error":
            await websocket.send_json({"type": "error", "message": payload})
            return


async def _close_session(
    websocket: WebSocket,
    ctx: AppContext,
    user_id: str,
    persona_id: str,
) -> None:
    """在 WebSocket 上处理"结束 session"。"""
    try:
        report = await run_in_threadpool(
            ctx.services.session_close.close_session, user_id, persona_id,
        )
    except Exception as exc:  # noqa: BLE001
        await websocket.send_json({"type": "error", "message": str(exc)})
        return

    ext = report.extraction
    await websocket.send_json({
        "type": "session_closed",
        "marker_id": report.marker_id,
        "facts_created": ext.facts_created,
        "blocks_updated": ext.blocks_updated,
        "conversations_processed": ext.conversations_processed,
        "errors": list(ext.errors),
    })


__all__ = ["router"]
