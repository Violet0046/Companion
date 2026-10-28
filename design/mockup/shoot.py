# -*- coding: utf-8 -*-
"""用 Chrome DevTools Protocol 精确截图（视口尺寸完全可控）。

为什么不用 `chrome --headless --screenshot --window-size=W,H`：
Chrome 在 Windows 上有最小窗口宽度（实测 ~490px），--window-size 给小了不生效，
截出来的图会被裁掉右边一截。CDP 的 Emulation.setDeviceMetricsOverride
可以精确设定视口，和真机一模一样。

用法：
    python design/mockup/shoot.py            # 截全部屏
    python design/mockup/shoot.py 01         # 只截某屏
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = pathlib.Path(__file__).resolve().parent
SHOTS = HERE / "screenshots"
PORT = 9333

SCREENS = [
    ("01-messages", "消息"),
    ("02-contacts", "通讯录"),
    ("03-memory", "她的记忆"),
    ("04-me", "我"),
    ("05-conversation", "会话"),
    ("06-profile", "角色资料"),
]


def _json(url: str, tries: int = 80):
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.25)
    raise RuntimeError(f"连不上 DevTools：{url} ({last})")


async def _shoot_one(ws_url: str, url: str, out: pathlib.Path, w: int, h: int, scale: int):
    # 注意：origin=None —— Chrome 拒绝带 Origin 头的 DevTools WS 连接
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, origin=None,
                                  open_timeout=15) as ws:
        n = 0

        async def cmd(method: str, params=None):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if msg.get("id") == n:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        await cmd("Page.enable")
        await cmd("Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": scale,
            "mobile": True, "screenWidth": w, "screenHeight": h,
        })
        await cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        await cmd("Page.navigate", {"url": url})
        await asyncio.sleep(1.6)   # 等字体 / 动画 / 布局稳定
        res = await cmd("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": False, "fromSurface": True,
        })
        out.write_bytes(base64.b64decode(res["data"]))
        return out


async def main():
    only = sys.argv[1:] or None
    if not pathlib.Path(CHROME).exists():
        print("找不到 Chrome:", CHROME)
        return 1
    SHOTS.mkdir(exist_ok=True)
    profile = tempfile.mkdtemp(prefix="ce-shot-")
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
        "--disable-lcd-text", "--font-render-hinting=none", "--no-first-run",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 直接用 Chrome 启动时开好的那个 page target（省掉 browser 级 WS）
        page_ws = None
        for _ in range(80):
            for t in _json(f"http://127.0.0.1:{PORT}/json/list"):
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    page_ws = t["webSocketDebuggerUrl"]
                    break
            if page_ws:
                break
            time.sleep(0.25)
        if not page_ws:
            raise RuntimeError("找不到 page target")

        screens = SCREENS
        if only:
            screens = [s for s in SCREENS if only[0] in s[0]]

        for stem, title in screens:
            src = HERE / f"{stem}.html"
            out = SHOTS / f"{stem}.png"
            await _shoot_one(page_ws, src.as_uri(), out, 402, 874, 2)
            print(f"[OK] {out.name}  ({out.stat().st_size//1024} KB)  — {title}", flush=True)

        src = HERE / "00-overview.html"
        if src.exists() and not only:
            out = SHOTS / "00-overview.png"
            await _shoot_one(page_ws, src.as_uri(), out, 2680, 946, 1)
            print(f"[OK] {out.name}  ({out.stat().st_size//1024} KB)  — 总览", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))