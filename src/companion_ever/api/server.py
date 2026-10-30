"""api/server.py — FastAPI 应用工厂 + 启动入口。

把 Companion Ever Memory 的 application 层包成 HTTP / WebSocket，
让手机浏览器（加到主屏幕后就是 PWA）可以直接和 AI 伴侣对话。

和 CLI 的关系：
    CLI  ──► build_app(AppConfig) ──► AppContext ──► services
    Web  ──► build_app(AppConfig) ──► AppContext ──► services  ← 同一套

业务逻辑一行没改，只是多了一层传输。
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from companion_ever.app import AppContext, build_app
from companion_ever.config import AppConfig, WebConfig

# 静态资源目录：src/companion_ever/web/
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


async def _session_sweeper(app: FastAPI, interval: int = 15) -> None:
    """定期清扫：把空闲超时的 session 自动关掉（并触发抽取）。

    这是「用户离开后再也不回来」的唯一保障——
    没有它，那段对话的记忆永远不会被抽取。

    抽取在后台线程里跑，不阻塞任何请求。
    """
    while True:
        try:
            await asyncio.to_thread(_sweep_services,app.state.ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.getLogger(__name__).exception("后台任务恢复失败")
        await asyncio.sleep(interval)


def _sweep_services(ctx):
    from companion_ever.api.routes.chat import resume_queued_replies
    try:
        resume_queued_replies(ctx)
        ctx.services.chat.generations.recover()
        if ctx.services.background is not None:
            ctx.services.background.tick()
        else:
            ctx.services.session_close.resume_pending()
            if ctx.services.summary is not None:
                ctx.services.summary.resume_pending()
        idle=int(getattr(ctx.cfg,'idle_session_minutes',120) or 0)
        for session in ctx.services.conversation.list_active_sessions():
            ctx.services.session_close.close_if_stale(session['user_id'],session['persona_id'],idle)
    finally:
        ctx.conn.close_current()


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """启动后台清扫任务，停止时取消。"""
    from companion_ever.services.task_runtime import runtime_for
    runtime_for(app.state.ctx.conn).start()
    task = asyncio.create_task(_session_sweeper(app))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.to_thread(runtime_for(app.state.ctx.conn).shutdown)


def create_app(
    ctx: AppContext | None = None,
    web_cfg: WebConfig | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        ctx: 已构造好的 AppContext。传 None 则按环境变量自己建一个。
        web_cfg: Web 配置。传 None 则按环境变量自己建一个。
    """
    if ctx is None:
        cfg = AppConfig.from_env()
        # Web 层多线程共用一条连接
        cfg.check_same_thread = False
        ctx = build_app(cfg)
    if web_cfg is None:
        web_cfg = WebConfig.from_env()

    app = FastAPI(
        title="Companion Ever Memory",
        description="长期记忆 AI 伴侣 —— Web / PWA 接口",
        version="0.3.0",
        lifespan=_lifespan,
    )

    # 挂到 app.state，路由里直接取
    app.state.ctx = ctx
    app.state.web_cfg = web_cfg

    @app.middleware('http')
    async def no_cached_state(request,call_next):
        response=await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        elif request.url.path in ('/','/index.html','/app.js','/styles.css'):
            response.headers['Cache-Control']='no-cache'
        return response

    # --- 路由 ---
    from companion_ever.api.routes import chat, memory

    app.include_router(chat.router)
    app.include_router(memory.router)

    # 记录（抽取记忆）跑完后，服务端**主动推送** record_done 给前端。
    # 前端收到就刷新关系状态 / 记忆 / 计数——不用「退出重进」才能看到变化。
    ctx.services.session_close.on_record_done = chat.notify_record_done
    if ctx.services.summary is not None:
        ctx.services.summary.on_updated=chat.notify_summary_updated

    @app.exception_handler(PermissionError)
    async def _permission_handler(request: Request, exc: PermissionError):
        """权限错误返回 403（不是 500）。
        否则前端会看到一个无意义的 500，而不是"你没权限访问这个角色"。"""
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.get("/api/health", include_in_schema=False)
    def health() -> JSONResponse:
        """健康检查——顺便告诉前端 PIN 是否生效、自动记录间隔多长。"""
        idle = int(getattr(app.state.ctx.cfg, "idle_session_minutes", 120) or 0)
        return JSONResponse({
            "ok": True,
            "auth_required": bool(web_cfg.pin),
            "idle_minutes": idle,
            "auto_memory": app.state.ctx.cfg.background.enabled,
            "memory_idle_seconds": app.state.ctx.cfg.background.memory_idle_seconds,
            "schema_version": app.state.ctx.conn.execute('PRAGMA user_version').fetchone()[0],
        })

    # manifest 要用 application/manifest+json，否则 PWA 安装不了
    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        path = WEB_DIR / "manifest.webmanifest"
        return FileResponse(path, media_type="application/manifest+json")

    # service worker 不能被缓存，否则改了前端也刷不出来
    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        path = WEB_DIR / "sw.js"
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    # --- 静态资源（PWA 壳）---
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:  # pragma: no cover — 打包异常时的兜底
        @app.get("/", include_in_schema=False)
        def missing_web() -> JSONResponse:
            return JSONResponse(
                {"error": "静态资源目录缺失", "expected": str(WEB_DIR)},
                status_code=500,
            )

    return app


def local_ip_addresses() -> list[dict]:
    """列出本机所有可用的 IPv4 地址（含适配器名称）。

    手机要访问电脑上的服务，就用这里的地址。

    现在会有多个适配器（WiFi / 有线 / 移动点热点 / 虚拟网卡），
    它们的 IP 都可能是手机能达到的，所以全部列出来让用户自行选。

    Returns:
        [{"iface": "适配器名", "ip": "10.51.151.105"}, ...]
    """
    out: list[dict] = []
    seen: set[str] = set()

    # 1) 通过读适配器列表拿所有 IPv4
    try:
        import subprocess
        raw = subprocess.run(
            ["ipconfig"], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        ).stdout
        current_iface = ""
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.endswith(":") and ("adapter" in stripped.lower() or "适配器" in stripped):
                current_iface = stripped.rstrip(":")
                continue
            if "IPv4" in stripped and "Address" in stripped:
                parts = stripped.split(":", 1)
                if len(parts) == 2:
                    ip = parts[1].strip()
                    if ip and not ip.startswith("127.") and ip not in seen:
                        seen.add(ip)
                        out.append({"iface": current_iface, "ip": ip})
    except Exception:
        pass

    # 2) 补一个"默认出口所在网卡"的地址（ipconfig 解析失败时兑底）
    if not out:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("10.255.255.255", 1))
                ip = sock.getsockname()[0]
                if ip and not ip.startswith("127."):
                    out.append({"iface": "default", "ip": ip})
            finally:
                sock.close()
        except OSError:
            pass

    return out


def run_server(
    host: str | None = None,
    port: int | None = None,
    db_path: str | None = None,
) -> None:
    """启动 Web 服务（阻塞）。"""
    import uvicorn

    app_cfg = AppConfig.from_env()
    if db_path:
        app_cfg.db_path = Path(db_path)
    app_cfg.check_same_thread = False

    web_cfg = WebConfig.from_env()
    if host:
        web_cfg = WebConfig(
            host=host, port=port or web_cfg.port,
            pin=web_cfg.pin, user=web_cfg.user, persona=web_cfg.persona,
        )
    elif port:
        web_cfg = WebConfig(
            host=web_cfg.host, port=port,
            pin=web_cfg.pin, user=web_cfg.user, persona=web_cfg.persona,
        )

    ctx = build_app(app_cfg)
    app = create_app(ctx, web_cfg)

    print("=" * 52)
    print("  Companion Ever Memory — Web 服务已启动")
    print("=" * 52)
    print(f"  本机访问:   http://127.0.0.1:{web_cfg.port}")
    addrs = local_ip_addresses()
    if addrs:
        print("  手机访问（选一个手机能达到的）：")
        for a in addrs:
            print(f"      http://{a['ip']}:{web_cfg.port}    <- {a['iface']}")
    else:
        print("  手机访问:   （未找到可用的局域网地址）")
    if web_cfg.pin:
        print(f"  访问 PIN:   {web_cfg.pin}")
    else:
        print("  访问 PIN:   未设置（任何人同网段都能访问）")
    print("  手机浏览器打开后 → 菜单里选「添加到主屏幕」")
    print("=" * 52)

    uvicorn.run(app, host=web_cfg.host, port=web_cfg.port, log_level="info")


__all__ = ["create_app", "run_server", "local_ip_addresses", "WEB_DIR"]
