"""api — Web / PWA 层（FastAPI）。

把已有的 application 层 service 暴露成 HTTP + WebSocket，
让手机浏览器可以直接和 AI 伴侣对话。

设计原则：
- **不改业务逻辑**——只是把 build_app() 出来的 AppContext 接到 HTTP 上
- CLI 与 Web 共用同一套 service（同一份 DB、同一套记忆）
- 静态资源（index.html / PWA manifest / service worker）放在 companion_ever.web 包内
"""
from companion_ever.api.server import create_app, run_server

__all__ = ["create_app", "run_server"]
