"""PWA 前端资源的冒烟测试。

保证：
- 静态文件都在（少一个文件前端就白屏）
- manifest 合法（否则装不了 PWA）
- app.js 里用到的 API 路径都真实存在（防止前后端路径写错）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def main() -> int:
    from companion_ever.api.server import WEB_DIR

    assert WEB_DIR.is_dir(), f"静态目录不存在: {WEB_DIR}"

    # --- 1. 必需文件都在 ---
    required = [
        "index.html", "app.js", "styles.css",
        "manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png",
    ]
    missing = [f for f in required if not (WEB_DIR / f).exists()]
    assert not missing, f"缺少静态文件: {missing}"
    print(f"[OK] 静态文件齐全（{len(required)} 个）")

    # --- 2. manifest 合法 ---
    manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    for key in ("name", "start_url", "display", "icons", "theme_color"):
        assert key in manifest, f"manifest 缺少 {key}"
    assert manifest["display"] == "standalone", "manifest 应该是 standalone（独立窗口）"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert "192x192" in sizes and "512x512" in sizes, f"图标尺寸不全: {sizes}"
    print(f"[OK] manifest 合法（display={manifest['display']}, 图标 {sorted(sizes)}）")

    # --- 3. 图标是有效 PNG ---
    for name in ("icon-192.png", "icon-512.png"):
        raw = (WEB_DIR / name).read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是合法 PNG"
        # IHDR 里的宽高
        w = int.from_bytes(raw[16:20], "big")
        h = int.from_bytes(raw[20:24], "big")
        assert w == h, f"{name} 不是正方形（{w}x{h}）"
        expected = int(name.split("-")[1].split(".")[0])
        assert w == expected, f"{name} 尺寸不对（{w}，应为 {expected}）"
    print("[OK] 图标是有效 PNG 且尺寸正确")

    # --- 4. app.js 里引用的 API 路径都存在 ---
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    used = set()
    for marker in ("/api/health", "/api/users", "/api/session/close",
                   "/api/settings/", "/api/persona/", "/ws/chat"):
        if marker in js:
            used.add(marker)

    # 用 app 的 OpenAPI 拿全量路由（包含 server.py 自己定义的，如 /api/health）
    from companion_ever.api.server import create_app
    from companion_ever.config import WebConfig
    # Route introspection must not initialize/migrate the user's .env database.
    import tempfile
    from pathlib import Path
    from test_chat_workflow import _make_ctx
    with tempfile.TemporaryDirectory(prefix='web-assets-') as tmp:
        ctx=_make_ctx(str(Path(tmp)/'test.db'),check_same_thread=False)
        try:
            app=create_app(ctx=ctx,web_cfg=WebConfig())
        finally:
            ctx.conn.close()
    server_paths = set(app.openapi()["paths"].keys())
    # include_in_schema=False 的路由（如 /api/health）不在 OpenAPI 里
    for r in app.routes:
        sp = getattr(r, "path", None)
        if sp:
            server_paths.add(sp)
    # WebSocket 路由不在 OpenAPI 里
    from companion_ever.api.routes import chat as chat_routes
    for r in chat_routes.router.routes:
        sp = getattr(r, "path", None)
        if sp:
            server_paths.add(sp)
    server_paths = {p.replace("{user_id}", "").replace("{persona_id}", "") for p in server_paths}

    for marker in sorted(used):
        head = marker.split("{")[0].rstrip("/")
        assert any(head in sp for sp in server_paths), (
            f"app.js 用了 {marker}，但服务端没有对应路由（服务端: {sorted(server_paths)}）"
        )
    print(f"[OK] app.js 引用的 {len(used)} 个接口都真实存在")

    # --- 5. index.html 里的元素 id 和 app.js 对得上 ---
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    import re
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r"\$\('([^']+)'\)", js))
    # 这些 id 是运行时动态创建的（角色资料页 / 通讯录页按需渲染）
    DYNAMIC_IDS = {
        "rel-card", "date-list", "cell-new-persona", "cell-group-chat",
        "btn-record-now", "last-record-val", "task-attempts",
    }
    missing_ids = js_ids - html_ids - DYNAMIC_IDS
    assert not missing_ids, f"app.js 引用了不存在的元素: {sorted(missing_ids)}"
    print(f"[OK] app.js 引用的 {len(js_ids)} 个元素 id 都存在于 index.html")

    print("[ALL] PWA 前端资源冒烟测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
