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
from companion_ever.services.assistant_reply import split_assistant_reply
from companion_ever.services.llm_json import strip_thinking
from companion_ever.api.deps import check_pin, resolve_target
from companion_ever.api.schemas import ChatIn, CloseSessionIn, CloseSessionOut
from companion_ever.api.think_filter import ThinkFilter
from companion_ever.services.session_close_service import SessionCloseReport

router = APIRouter()


# ============ WS 连接注册表（把后台事件推给前端） ============
#
# 为什么需要：
#   记录（抽取记忆）是**后台线程**跑的，前端是 WebSocket 连着的。
#   记录做完后，关系状态 / 记忆 / 计数都变了，前端要能自己刷新，
#   而不是靠「退出重进」或「轮询 last_record_at」——
#   last_record_at 是 marker 的写入时间（= 开始记录的时刻），
#   不是抽取完成的时刻，轮询会提前误判成「记好了」。
#
# 所以：服务端在抽取完成后**主动推送** record_done 事件。

class _Conn:
    """一条 WS 连接。

    发送要串行化：主处理循环在发消息，后台线程也可能往里推事件，
    两个协程同时 send 会把帧交错，客户端直接解析失败。
    """

    __slots__ = ("ws", "loop", "lock")

    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        self.ws = ws
        self.loop = loop
        self.lock = asyncio.Lock()

    async def send(self, payload: dict[str, Any]) -> None:
        async with self.lock:
            await self.ws.send_json(payload)


# user_id -> 该用户的所有在线连接
#
# 为什么按 **user** 而不是按 (user, persona)：
# 通讯录里可以打开**任意一个角色**的资料页 / 关系状态页，
# 而 WS 只连着「当前在聊的那个角色」。
# 如果按 (user, persona) 分组，那么「在通讯录里点开另一个角色，
# 手动记一次」就收不到 record_done，页面还是得退出重进。
# 按 user 分组 → 同一个人的所有角色的记录事件都能收到，
# 事件里带 persona_id，前端自己判断该不该刷新。
_CONNS: dict[str, set[_Conn]] = {}
_CONN_LOCK = threading.Lock()


def _register(conn: _Conn, user_id: str) -> None:
    with _CONN_LOCK:
        _CONNS.setdefault(user_id, set()).add(conn)


def _unregister(conn: _Conn, user_id: str) -> None:
    with _CONN_LOCK:
        bucket = _CONNS.get(user_id)
        if not bucket:
            return
        bucket.discard(conn)
        if not bucket:
            _CONNS.pop(user_id, None)


def _send_threadsafe(conn: _Conn, payload: dict[str, Any]) -> None:
    """在任意线程把 payload 送到某条连接。"""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is conn.loop:
        # 已经在事件循环线程里：不能同步等自己（会死锁），丢个 task
        conn.loop.create_task(conn.send(payload))
        return

    try:
        fut = asyncio.run_coroutine_threadsafe(conn.send(payload), conn.loop)
    except RuntimeError:  # pragma: no cover —— loop 已关闭
        return

    def _swallow(f: Any) -> None:
        try:
            f.exception()
        except Exception:  # noqa: BLE001
            pass

    fut.add_done_callback(_swallow)


def broadcast(user_id: str, persona_id: str, payload: dict[str, Any]) -> None:
    """把事件推给某个用户的所有在线连接（可从任意线程调用）。

    persona_id 只用来标注事件内容（payload 里也带一份），
    推送范围是**这个用户的所有连接** —— 见上面 _CONNS 的注释。
    """
    with _CONN_LOCK:
        conns = list(_CONNS.get(user_id, ()))
    for conn in conns:
        _send_threadsafe(conn, payload)


def notify_record_done(
    user_id: str, persona_id: str, report: SessionCloseReport,
) -> None:
    """记录完成的回调（挂在 SessionCloseService.on_record_done 上）。

    前端收到后重新拉一次角色汇总 + 关系状态页面，不用退出重进。
    """
    ext = report.extraction
    broadcast(user_id, persona_id, {
        "type": "record_done",
        "user_id": user_id,
        "persona_id": persona_id,
        "facts_created": ext.facts_created,
        "blocks_updated": ext.blocks_updated,
        "conversations_processed": ext.conversations_processed,
        "errors": list(ext.errors),
    })



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

    # 注册到连接表：后台线程（记录完成）要能把事件推给这条连接
    conn = _Conn(websocket, asyncio.get_running_loop())
    key = user_id
    _register(conn, key)

    await conn.send({
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
                await _stream_reply(conn, ctx, user_id, persona_id, content)

            elif mtype == "close_session":
                await _close_session(conn, ctx, user_id, persona_id)

            elif mtype == "ping":
                await conn.send({"type": "pong"})

            else:
                await conn.send({
                    "type": "error",
                    "message": f"未知消息类型: {mtype!r}",
                })
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001 — 兜底，避免连接悬挂
        try:
            await conn.send({"type": "error", "message": str(e)})
        except Exception:
            pass
        return
    finally:
        _unregister(conn, key)


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
    conn: "_Conn",
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

    await conn.send({"type": "start", "content": content})

    while True:
        kind, payload = await queue.get()
        if kind == "delta":
            await conn.send({"type": "delta", "content": payload})
        elif kind == "thinking":
            await conn.send({"type": "thinking", "content": payload})
        elif kind == "done":
            await conn.send({"type": "done"})
            return
        elif kind == "error":
            await conn.send({"type": "error", "message": payload})
            return


async def _close_session(
    conn: "_Conn",
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
        await conn.send({"type": "error", "message": str(exc)})
        return

    ext = report.extraction
    await conn.send({
        "type": "session_closed",
        "marker_id": report.marker_id,
        "facts_created": ext.facts_created,
        "blocks_updated": ext.blocks_updated,
        "conversations_processed": ext.conversations_processed,
        "errors": list(ext.errors),
    })


__all__ = ["router", "broadcast", "notify_record_done"]
