"""api/routes/chat.py — 对话相关的 HTTP + WebSocket。

- WS  /ws/chat     流式对话（主通道，手机端用这个）
- POST /api/messages 持久接收并启动回复；断线后可查询生成快照
- POST /api/chat    非流式对话（调试 / 兜底）
- POST /api/session/close  结束 session（写 sentinel + 触发记忆抽取）
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from companion_ever.bootstrap.app import AppContext
from companion_ever.infrastructure.llm.json_output import strip_thinking
from companion_ever.entrypoints.web.deps import resolve_target
from companion_ever.accounts.service import AuthError, COOKIE
from companion_ever.entrypoints.web.schemas import ChatIn, CloseSessionIn, CloseSessionOut, ReadIn, ProactiveIn
from companion_ever.memory.extraction.sessions import SessionCloseReport

router = APIRouter()


@router.post('/api/persona/{user_id}/{persona_id}/read')
def mark_read(request: Request,user_id: str,persona_id: str,payload: ReadIn):
    ctx=_authorized_ctx(request,user_id,persona_id)
    ctx.services.conversation.mark_read(user_id,persona_id,payload.message_id)
    return {'ok':True}


@router.get('/api/persona/{user_id}/{persona_id}/proactive')
def get_proactive(request: Request,user_id: str,persona_id: str):
    ctx=_authorized_ctx(request,user_id,persona_id)
    if ctx.services.proactive is None:
        return {'enabled':False}
    return ctx.services.proactive.setting(user_id,persona_id)


@router.put('/api/persona/{user_id}/{persona_id}/proactive')
def set_proactive(request: Request,user_id: str,persona_id: str,payload: ProactiveIn):
    ctx=_authorized_ctx(request,user_id,persona_id)
    if ctx.services.proactive is None:
        raise HTTPException(503,'主动联系暂未启用')
    return ctx.services.proactive.setting(user_id,persona_id,payload.enabled)


def _authorized_ctx(request, user_id, persona_id):
    ctx = request.app.state.ctx
    ctx.services.conversation.authorizer.require_persona(user_id,persona_id)
    return ctx


@router.get('/api/persona/{user_id}/{persona_id}/generations/{generation_id}')
def generation_status(request: Request,user_id: str,persona_id: str,generation_id: str):
    ctx=_authorized_ctx(request,user_id,persona_id)
    try:
        return _present_snapshot(ctx.services.generation.snapshot(user_id,persona_id,generation_id))
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@router.get('/api/persona/{user_id}/{persona_id}/generation')
def latest_generation(request: Request,user_id: str,persona_id: str):
    ctx = _authorized_ctx(request,user_id,persona_id)
    return _present_snapshot(ctx.services.generation.latest(user_id,persona_id))


@router.post('/api/persona/{user_id}/{persona_id}/generations/{generation_id}/retry')
def retry_generation(request: Request,user_id: str,persona_id: str,generation_id: str):
    ctx=_authorized_ctx(request,user_id,persona_id)
    try:
        return _present_snapshot(ctx.services.generation.retry(user_id,persona_id,generation_id))
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


def _present_message(message):
    return {**message, 'created_at': message['created_at'].replace(' ', 'T') + 'Z'}


def _present_snapshot(snapshot):
    if snapshot is None:
        return None
    return {**snapshot, 'messages': [_present_message(message) for message in snapshot['messages']]}


@router.post('/api/messages')
def receive_message(request: Request,payload: ChatIn):
    ctx=request.app.state.ctx
    user,persona=resolve_target(ctx,request.app.state.web_cfg,request.state.account['user_id'],payload.persona_id)
    try:
        g,created=ctx.services.generation.submit(user,persona,payload.content,payload.client_message_id)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    return {'message_id':g['trigger_message_id'],'generation_id':g['id'],'status':g['status'],'duplicate':not created}


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
            try:
                await run_in_threadpool(self.ws.app.state.accounts.authenticate, self.ws.cookies.get(COOKIE, ''))
            except AuthError:
                await self.ws.close(code=4401)
                return
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

    send = conn.send(payload)
    try:
        fut = asyncio.run_coroutine_threadsafe(send, conn.loop)
    except RuntimeError:  # pragma: no cover —— loop 已关闭
        send.close()
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


def revalidate_connections(user_id):
    """A logout/password change immediately closes sockets with revoked sessions."""
    broadcast(user_id, '', {'type': 'auth_check'})


def notify_summary_updated(user_id,persona_id):
    broadcast(user_id,persona_id,{'type':'summary_updated','user_id':user_id,'persona_id':persona_id})


def notify_proactive_message(user_id,persona_id,message):
    broadcast(user_id,persona_id,{'type':'proactive_message','user_id':user_id,'persona_id':persona_id,'message':message})


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
        "notes_updated": ext.notes_updated,
        "job_id": ext.job_id,
        "job_status": ext.job_status,
        "more_pending": report.more_pending,
        "blocks_updated": ext.blocks_updated,
        "conversations_processed": ext.conversations_processed,
        "errors": list(ext.errors),
    })



# ============ 非流式（调试 / 兜底）============

@router.post("/api/chat")
def post_chat(
    request: Request,
    payload: ChatIn,
) -> dict[str, Any]:
    """一次性拿到完整回复（不走 WebSocket）。"""
    ctx: AppContext = request.app.state.ctx
    cfg = request.app.state.web_cfg

    user_id, persona_id = resolve_target(ctx, cfg, request.state.account['user_id'], payload.persona_id)
    try:
        raw = ctx.services.chat.chat(user_id, persona_id, payload.content, payload.client_message_id)
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
) -> CloseSessionOut:
    """结束当前 session：写 session_close sentinel + 立即触发记忆抽取。

    仅供调试维护；正常整理由后台调度触发，不解析聊天中的退出命令。
    """
    ctx: AppContext = request.app.state.ctx
    cfg = request.app.state.web_cfg

    user_id, persona_id = resolve_target(ctx, cfg, request.state.account['user_id'], payload.persona_id)
    try:
        # **后台执行**：抽取是一次完整 LLM 调用（几十秒），
        # 同步等会让用户干等着、没法继续聊天。
        # 完成后通过 record_done 通知，状态也可通过持久任务查询恢复。
        started = ctx.services.session_close.close_session_background(
            user_id, persona_id, retry_failed=True,
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
) -> None:
    """流式对话通道。

    客户端 → 服务端：
        {"type": "chat", "content": "在干嘛"}
        {"type": "close_session"}
        {"type": "ping"}

    服务端 → 客户端：
        {"type": "ready", "user_id": ..., "persona_id": ...}
        {"type": "received", "message_id": ..., "generation_id": ...}
        {"type": "start", "generation_id": ...}
        {"type": "delta", "content": "在等你呀"}
        {"type": "thinking", "content": "本轮的思考过程"}
        {"type": "message_done", "message": {...}, "generation_id": ...}
        {"type": "done", "generation_id": ...}
        {"type": "error", "message": "..."}
        {"type": "session_closed", "facts_created": 3, ...}
    """
    ctx: AppContext = websocket.app.state.ctx
    cfg = websocket.app.state.web_cfg

    try:
        user_id, persona_id = resolve_target(ctx, cfg, websocket.state.account['user_id'], persona)
        ctx.services.conversation.authorizer.require_persona(user_id, persona_id)
    except HTTPException as e:
        await websocket.close(code=4404, reason=str(e.detail))
        return
    except PermissionError:
        await websocket.close(code=4403, reason='角色不可访问')
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

    reply_tasks = set()
    active_generation = None
    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            except asyncio.TimeoutError:
                msg = {'type': 'ping'}
            try:
                await run_in_threadpool(websocket.app.state.accounts.authenticate, websocket.cookies.get(COOKIE, ''))
            except AuthError:
                await websocket.close(code=4401)
                break
            mtype = (msg or {}).get("type")

            if mtype == "chat":
                content = str((msg or {}).get("content") or "").strip()
                if not content:
                    continue
                if len(content) > 16000:
                    await conn.send({'type':'error','message':'消息过长，请控制在 16000 字以内'})
                    continue
                channel, emit = _reply_channel()
                try:
                    generation, created = await run_in_threadpool(
                        ctx.services.generation.submit, user_id, persona_id, content,
                        str(msg.get('client_message_id') or '') or None, on_event=emit)
                except (ValueError, PermissionError) as exc:
                    channel[1].set()
                    await conn.send({'type':'error','message':str(exc)})
                    continue
                try:
                    if created:
                        for task in list(reply_tasks):
                            task.cancel()
                        if active_generation:
                            await conn.send({'type':'cancelled','generation_id':active_generation})
                    active_generation = generation['id']
                    await conn.send({'type':'received','generation_id':generation['id'],
                                     'message_id':generation['trigger_message_id'],
                                     'client_message_id':msg.get('client_message_id'),
                                     'status':generation['status'],'duplicate':not created})
                except BaseException:
                    channel[1].set()
                    raise
                task=asyncio.create_task(_stream_reply(conn,ctx,generation,channel))
                reply_tasks.add(task)
                task.add_done_callback(reply_tasks.discard)
                task.add_done_callback(lambda _, stop=channel[1]: stop.set())

            elif mtype == "close_session":
                ctx.services.session_close.close_session_background(user_id,persona_id,retry_failed=True)
                await conn.send({'type':'record_started'})

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
        for task in reply_tasks:
            task.cancel()
        if reply_tasks:
            await asyncio.gather(*reply_tasks,return_exceptions=True)
        _unregister(conn, key)



def _reply_channel():
    """Bridge a transport-independent callback into this socket's event loop."""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    stop = threading.Event()
    def emit(event):
        if not stop.is_set() and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass
    return (queue, stop), emit


async def _stream_reply(conn, ctx, generation, channel) -> None:
    queue, stop = channel
    gid = generation['id']
    try:
        while True:
            event=await queue.get()
            if ctx.services.generation.state(generation['user_id'],generation['persona_id'],gid)['status']=='cancelled':
                await conn.send({'type':'cancelled','generation_id':gid})
                return
            if event['type']=='message_done':
                event={**event,'message':_present_message(event['message'])}
            await conn.send(event)
            if event['type'] in ('done','error','cancelled','failed','running','queued'):
                return
    except WebSocketDisconnect:
        return
    except RuntimeError:
        return
    except PermissionError:
        await conn.send({'type':'cancelled','generation_id':gid})
    finally:
        stop.set()



__all__ = ["router", "broadcast", "notify_record_done"]
