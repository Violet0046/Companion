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


async def _session_sweeper(app: FastAPI, interval: int = 60) -> None:
    """定期清扫：把空闲超时的 session 自动关掉（并触发抽取）。

    这是「用户离开后再也不回来」的唯一保障——
    没有它，那段对话的记忆永远不会被抽取。

    抽取在后台线程里跑，不阻塞任何请求。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            ctx = app.state.ctx
            idle = int(getattr(ctx.cfg, "idle_session_minutes", 120) or 0)
            if idle <= 0:
                continue
            for s in ctx.services.conversation.list_active_sessions():
                ctx.services.session_close.close_if_stale(
                    s["user_id"], s["persona_id"], idle,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """启动后台清扫任务，停止时取消。"""
    task = asyncio.create_task(_session_sweeper(app))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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

    # --- 路由 ---
    from companion_ever.api.routes import chat, memory

    app.include_router(chat.router)
    app.include_router(memory.router)

    @app.exception_handler(PermissionError)
    async def _permission_handler(request: Request, exc: PermissionError):
        """权限错误返回 403（不是 500）。
        否则前端会看到一个无意义的 500，而不是"你没权限访问这个角色"。"""
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.get("/api/health", include_in_schema=False)
    def health() -> JSONResponse:
        """健康检查——顺便告诉前端 PIN 是否生效。"""
        return JSONResponse({
            "ok": True,
            "auth_required": bool(web_cfg.pin),
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


def local_ip_addresses() -> list[str]:
    """列出本机可用于局域网访问的 IPv4 地址。

    手机要访问电脑上的服务，就用这里的地址。
    """
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 连一个不会真发包的地址，用来问系统"我从哪个网卡出去"
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    return sorted(ips)


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
    for ip in local_ip_addresses():
        print(f"  手机访问:   http://{ip}:{web_cfg.port}")
    if web_cfg.pin:
        print(f"  访问 PIN:   {web_cfg.pin}")
    else:
        print("  访问 PIN:   未设置（任何人同网段都能访问）")
    print("  手机浏览器打开后 → 菜单里选「添加到主屏幕」")
    print("=" * 52)

    uvicorn.run(app, host=web_cfg.host, port=web_cfg.port, log_level="info")


__all__ = ["create_app", "run_server", "local_ip_addresses", "WEB_DIR"]
