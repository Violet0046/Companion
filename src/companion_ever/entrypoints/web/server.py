"""api/server.py — FastAPI 应用工厂 + 启动入口。

把 Companion Ever Memory 的 application 层包成 HTTP / WebSocket，
让手机浏览器（加到主屏幕后就是 PWA）可以直接和 AI 伴侣对话。

和 CLI 的关系：
    CLI  ──► build_app(AppConfig) ──► AppContext ──► services
    Web  ──► build_app(AppConfig) ──► AppContext ──► services  ← 同一套

会话执行、持久化与恢复由业务模块拥有；本层负责宿主生命周期和传输装配。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import locale
import re
import socket
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from companion_ever.bootstrap.app import AppContext, build_app
from companion_ever.config import AppConfig, WebConfig
from companion_ever.accounts.service import AccountService
from companion_ever.accounts.usage import LimitExceeded
from companion_ever.entrypoints.web.routes.auth import authorize

# 静态资源目录：src/companion_ever/entrypoints/web/static/
WEB_DIR = Path(__file__).resolve().parent / "static"


def _frontend_version() -> str:
    digest = hashlib.sha256()
    for name in ('index.html', 'app.js', 'conversation-cache.js', 'notifications.js', 'styles.css', 'calls.html', 'calls.js', 'keyboard-check.html', 'sw.js', 'icon.png', 'manifest.webmanifest'):
        path = WEB_DIR / name
        if path.is_file():
            digest.update(name.encode('utf-8'))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    ctx = app.state.ctx
    await asyncio.to_thread(ctx.lifecycle.start)
    try:
        yield
    finally:
        stop = ctx.close if app.state.owns_context else ctx.lifecycle.stop
        await asyncio.to_thread(stop)


def create_app(
    ctx: AppContext | None = None,
    web_cfg: WebConfig | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        ctx: 已构造好的 AppContext。传 None 则按环境变量自己建一个。
        web_cfg: Web 配置。传 None 则按环境变量自己建一个。
    """
    owns_context = ctx is None
    if ctx is None:
        cfg = AppConfig.from_env()
        # 连接代理为每个工作线程建立独立的 SQLite 连接。
        cfg.check_same_thread = False
        ctx = build_app(cfg)
    if web_cfg is None:
        web_cfg = WebConfig.from_env()

    app = FastAPI(
        title="Companion Ever Memory",
        description="长期记忆 AI 伴侣 —— Web / PWA 接口",
        version="0.3.0",
        lifespan=_lifespan,
        dependencies=[Depends(authorize)],
    )

    # 挂到 app.state，路由里直接取
    app.state.ctx = ctx
    app.state.owns_context = owns_context
    app.state.web_cfg = web_cfg
    app.state.accounts = AccountService(ctx.conn)
    app.state.frontend_version = _frontend_version()

    @app.middleware('http')
    async def no_cached_state(request,call_next):
        response=await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        elif request.url.path in ('/','/index.html','/app.js','/conversation-cache.js','/notifications.js','/styles.css','/calls.html','/calls.js','/keyboard-check.html'):
            response.headers['Cache-Control']='no-cache'
        return response

    # --- 路由 ---
    from companion_ever.entrypoints.web.routes import auth, chat, memory, persona, calls, notifications, groups

    app.include_router(chat.router)
    app.include_router(auth.router)
    app.include_router(persona.router)
    app.include_router(memory.router)
    app.include_router(calls.router)
    app.include_router(notifications.router)
    app.include_router(groups.router)

    # 记录（抽取记忆）跑完后，服务端**主动推送** record_done 给前端。
    # 前端收到就刷新关系状态 / 记忆 / 计数——不用「退出重进」才能看到变化。
    ctx.services.session_close.on_record_done = chat.notify_record_done
    ctx.services.generation.on_proactive_message = chat.notify_proactive_message
    ctx.services.generation.on_group_message=chat.notify_group_message
    if ctx.services.summary is not None:
        ctx.services.summary.on_updated=chat.notify_summary_updated

    @app.exception_handler(PermissionError)
    async def _permission_handler(request: Request, exc: PermissionError):
        """权限错误返回 403（不是 500）。
        否则前端会看到一个无意义的 500，而不是"你没权限访问这个角色"。"""
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Validation responses must not echo passwords or private request bodies.
        errors = [{key: error[key] for key in ('loc', 'msg', 'type')} for error in exc.errors()]
        return JSONResponse(status_code=422, content={'detail': errors})

    @app.exception_handler(LimitExceeded)
    async def quota_error(request: Request, exc: LimitExceeded):
        return JSONResponse(status_code=429, content={'detail': str(exc), 'code': exc.code})

    @app.get("/api/health", include_in_schema=False)
    def health() -> JSONResponse:
        """Public readiness and frontend version; never returns account identities."""
        idle = int(getattr(app.state.ctx.cfg, "idle_session_minutes", 120) or 0)
        return JSONResponse({
            "ok": True,
            "auth_required": True,
            "auth_mode": "account",
            "frontend_version": app.state.frontend_version,
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
        @app.api_route('/apple-touch-icon.png', methods=['GET','HEAD'], include_in_schema=False)
        @app.api_route('/apple-touch-icon-precomposed.png', methods=['GET','HEAD'], include_in_schema=False)
        def apple_touch_icon() -> FileResponse:
            return FileResponse(WEB_DIR / 'icon.png', media_type='image/png', headers={'Cache-Control':'no-cache'})

        def render_shell(name: str) -> HTMLResponse:
            content = (WEB_DIR / name).read_text(encoding='utf-8')
            for asset in ('app.js', 'conversation-cache.js', 'notifications.js', 'styles.css', 'calls.js', 'apple-touch-icon.png'):
                content = content.replace(f'"/{asset}"', f'"/{asset}?v={app.state.frontend_version}"')
            return HTMLResponse(content)

        @app.get('/', include_in_schema=False)
        @app.get('/index.html', include_in_schema=False)
        def index() -> HTMLResponse:
            return render_shell('index.html')

        @app.get('/calls.html', include_in_schema=False)
        def call_page() -> HTMLResponse:
            return render_shell('calls.html')

        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:  # pragma: no cover — 打包异常时的兜底
        @app.get("/", include_in_schema=False)
        def missing_web() -> JSONResponse:
            return JSONResponse(
                {"error": "静态资源目录缺失", "expected": str(WEB_DIR)},
                status_code=500,
            )

    return app


_IPV4_RE = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')
_DOT_LEADER_RE = re.compile(r'\.\s+\.\s+\.')


def _decode_ipconfig(raw: bytes) -> str:
    """按 Windows 实际使用的代码页解码 ipconfig 的输出。

    ipconfig 是独立进程，它按控制台输出代码页写字节；认错编码的话
    适配器名会变成乱码。控制台代码页最权威，其次才是系统首选编码。
    """
    candidates: list[str] = []
    if sys.platform == 'win32':
        try:
            import ctypes
            code_page = ctypes.windll.kernel32.GetConsoleOutputCP()
            if code_page:
                candidates.append(f'cp{code_page}')
        except Exception:
            pass
    preferred = locale.getpreferredencoding(False)
    if preferred:
        candidates.append(preferred)
    candidates += ['utf-8', 'gbk']
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(candidates[0] if candidates else 'utf-8', errors='replace')


def _is_ipv4_line(line: str) -> bool:
    """ipconfig 的 IPv4 行。

    "IPv4" 这个标记在所有语言里都一样，但值前面的词是本地化的
    （中文 Windows 写"地址"，英文写"Address"），所以只靠标记加行尾
    的合法地址来判断，不去匹配本地化词。
    """
    if 'IPv4' not in line:
        return False
    match = _IPV4_RE.match(line.rsplit(':', 1)[-1].strip())
    return bool(match) and all(int(part) <= 255 for part in match.groups())


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
        raw = subprocess.run(["ipconfig"], capture_output=True).stdout
        text = _decode_ipconfig(raw)
        current_iface = ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _is_ipv4_line(stripped):
                ip = stripped.rsplit(":", 1)[-1].strip()
                if ip and not ip.startswith("127.") and ip not in seen:
                    seen.add(ip)
                    out.append({"iface": current_iface, "ip": ip})
                continue
            # 适配器标题行：以冒号结尾，且没有 ipconfig 的 ". . ." 引导点
            # （"特定配置 DNS 后缀 . . . :" 这种行也以冒号结尾，不能当标题）
            if stripped.endswith(":") and not _DOT_LEADER_RE.search(stripped):
                current_iface = stripped.rstrip(":").strip()
    except Exception:
        pass

    # 2) 补一个"默认出口所在网卡"的地址（ipconfig 解析失败时兑底）
    if not out:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # UDP connect 不发包，只用来问系统"走默认路由时源地址是哪个"
                sock.connect(("8.8.8.8", 1))
                ip = sock.getsockname()[0]
                if ip and not ip.startswith("127."):
                    out.append({"iface": "默认出口", "ip": ip})
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
    from dataclasses import replace
    web_cfg = replace(web_cfg, host=host or web_cfg.host, port=port or web_cfg.port)

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
    print("  访问认证:   账号登录（旧 PIN 已停用）")
    if not ctx.conn.execute('SELECT 1 FROM user WHERE login_name IS NOT NULL AND disabled=0').fetchone():
        print("  首次使用:   请先用 account create --bind-user 绑定原用户")
    print("  手机浏览器打开后 → 菜单里选「添加到主屏幕」")
    print("=" * 52)

    uvicorn.run(app, host=web_cfg.host, port=web_cfg.port, log_level="info")


__all__ = ["create_app", "run_server", "local_ip_addresses", "WEB_DIR"]
