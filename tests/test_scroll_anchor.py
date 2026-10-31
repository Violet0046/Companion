"""聊天区滚动锚定回归测试

背景
----
`.chat` 上曾经声明了 `scroll-behavior: smooth`。按 CSSOM-View 规范，
`scrollTo({behavior: 'auto'})` 里的 "auto" 并不是"立即滚动"，
而是"由元素自身的 scroll-behavior CSS 属性决定"。
于是 `scrollBottom(true)` 里的 instant 参数形同虚设，
页面打开时会从顶部平滑滚几千像素到最底，iOS Safari 还会被
syncViewport() 改高度打断，最终停在中间而不是底部。

这里锁死两件事：
1. CSS 里 .chat 不许再出现 scroll-behavior: smooth
2. JS 的"立即到底"必须是赋值 scrollTop，并且有 anchorBottom() 兜底
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def main() -> int:
    from companion_ever.entrypoints.web.server import WEB_DIR

    raw_css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # 先剥掉注释，免得 CSS 里那段"别再加 scroll-behavior"的说明把测试自己绊住
    css = re.sub(r"/\*.*?\*/", "", raw_css, flags=re.S)

    # --- 1. .chat 块里不许有 scroll-behavior: smooth ---
    chat_blocks = re.findall(r"\.chat\s*\{[^}]*\}", css)
    assert chat_blocks, "没找到 .chat 的 CSS 块"
    for blk in chat_blocks:
        assert "scroll-behavior: smooth" not in blk, (
            f".chat 里又出现了 scroll-behavior: smooth，会让 scrollBottom(true) 变成平滑滚动:\n{blk}"
        )
    print(f"[OK] .chat 的 {len(chat_blocks)} 个 CSS 块都没有 scroll-behavior: smooth")

    # --- 2. scrollBottom 的立即分支必须是赋值 scrollTop ---
    fn = re.search(r"function scrollBottom\(instant\)\s*\{(.*?)\n\}", js, re.S)
    assert fn, "找不到 scrollBottom 函数"
    body = fn.group(1)
    assert "box.scrollTop = box.scrollHeight" in body, (
        "scrollBottom 的立即分支必须直接赋值 scrollTop，才能无视 CSS scroll-behavior"
    )
    assert "behavior: instant ? 'auto' : 'smooth'" not in js, (
        "不能用 behavior:'auto' 表示立即滚动（auto = 由 CSS scroll-behavior 决定）"
    )
    assert "behavior: 'smooth'" in body, "平滑分支要显式写 behavior:'smooth'"
    print("[OK] scrollBottom 立即分支赋值 scrollTop，平滑分支显式 smooth")

    # --- 3. anchorBottom 必须存在，且在渲染后被调用 ---
    assert re.search(r"function anchorBottom\(\)", js), "缺少 anchorBottom()"
    anchor = re.search(r"function anchorBottom\(\)\s*\{(.*?)\n\}", js, re.S).group(1)
    assert anchor.count("scrollBottom(true)") >= 2, (
        "anchorBottom 应该在多帧后重复贴底，否则布局变化后又掉出底部"
    )
    assert "requestAnimationFrame" in anchor, "anchorBottom 缺少 requestAnimationFrame 兜底"

    load_hist = re.search(r"async function loadHistory\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert load_hist, "找不到 loadHistory"
    assert "anchorBottom()" in load_hist.group(1), (
        "loadHistory 渲染完必须调 anchorBottom()，否则打开页面停在顶部"
    )
    print("[OK] anchorBottom 存在且 loadHistory 渲染后调用")

    # --- 4. 高度变化时要重新贴底（键盘弹出 / app 高度重算） ---
    sv = re.search(r"function syncViewport\(\)\s*\{(.*?)\n\}", js, re.S)
    assert sv, "找不到 syncViewport"
    assert "anchorBottom()" in sv.group(1), (
        "syncViewport 在高度变化后必须重新贴底，否则 app 高度一重算就掉出底部"
    )
    print("[OK] syncViewport 高度变化后重新贴底")

    # --- 5. scrollIntoView 要显式指定行为，否则跳转到消息变成瞬移 ---
    for m in re.finditer(r"scrollIntoView\(([^)]*)\)", js):
        assert "behavior" in m.group(1), (
            f"scrollIntoView({m.group(1)}) 没有显式 behavior，"
            "CSS 去掉 scroll-behavior 后会变成瞬移"
        )
    print("[OK] scrollIntoView 全部显式指定了 behavior")

    print("[ALL] 滚动锚定回归测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
