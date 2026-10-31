"""entrypoints.web — Web / PWA 层（FastAPI）。

把已有的业务服务暴露成 HTTP + WebSocket，
让手机浏览器可以直接和 AI 伴侣对话。

设计原则：
- **不改业务逻辑**——只是把 build_app() 出来的 AppContext 接到 HTTP 上
- CLI 与 Web 共用同一套 service（同一份 DB、同一套记忆）和同一个生命周期入口
- 静态资源（index.html / PWA manifest / service worker）放在 companion_ever.entrypoints.web.static 内
"""
from companion_ever.entrypoints.web.server import create_app, run_server

__all__ = ["create_app", "run_server"]
