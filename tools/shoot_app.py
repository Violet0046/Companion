"""给**真实运行**的 App 截图（Chrome DevTools Protocol）。

用途：改完前端后，不用手动在手机上点一遍，
直接起个服务跑这个脚本就能看到每个页面长什么样。

用法：
    # 1) 先起服务
    $env:COMPANION_WEB_PORT=8796
    .venv/Scripts/companion-cli.exe serve

    # 2) 截图（另开一个终端）
    python tools/shoot_app.py                      # 默认 http://127.0.0.1:8796
    python tools/shoot_app.py --url http://127.0.0.1:8000
    python tools/shoot_app.py --out D:/Temp/shots

为什么不用 `chrome --headless --screenshot --window-size=W,H`：
Chrome 在 Windows 上有最小窗口宽度（实测 ~490px），--window-size 给小了不生效，
截出来的图右边会被裁掉。CDP 的 Emulation.setDeviceMetricsOverride
能精确设定视口（402x874 @2x），和真机一致。

坑：DevTools 的 WebSocket 连接不能带 Origin 头（要 origin=None）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.request

import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# 要截的页面：(文件名, 执行的 JS)
SHOTS = [
    ("1-messages", "showView('messages')"),
    ("2-contacts", "showView('contacts'); renderContacts();"),
    ("3-me", "showView('me'); renderMe();"),
    ("4-profile", "openProfile(S.user, S.persona)"),
    ("5-memory", "openMemory(S.user, S.persona)"),
    ("6-chat", "openChat(S.user, S.persona)"),
    ("7-calendar", "openCalendar(S.user, S.persona)"),
]


def _json(url: str, tries: int = 80):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"连不上 DevTools：{url}")


async def shoot(url: str, out: pathlib.Path, width: int, height: int, port: int):
    profile = tempfile.mkdtemp(prefix="ce-app-shot-")
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
        "--disable-lcd-text", "--font-render-hinting=none", "--no-first-run",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page_ws = None
        for _ in range(80):
            for t in _json(f"http://127.0.0.1:{port}/json/list"):
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    page_ws = t["webSocketDebuggerUrl"]
                    break
            if page_ws:
                break
            time.sleep(0.25)
        if not page_ws:
            raise RuntimeError("找不到 page target")

        async with websockets.connect(page_ws, max_size=64 * 1024 * 1024,
                                      origin=None) as ws:
            n = 0

            async def cmd(method: str, params=None):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if msg.get("id") == n:
                        if "error" in msg:
                            print(f"  [CDP 错] {method}: {msg['error']}")
                            return {}
                        return msg.get("result", {})

            async def ev(expr: str):
                r = await cmd("Runtime.evaluate", {"expression": expr})
                ex = r.get("exceptionDetails")
                if ex:
                    print(f"  [JS 异常] {expr[:60]} | {ex.get('text')}")
                return (r.get("result") or {}).get("value")

            async def shot(name: str):
                r = await cmd("Page.captureScreenshot",
                              {"format": "png", "captureBeyondViewport": False})
                if r.get("data"):
                    (out / f"{name}.png").write_bytes(base64.b64decode(r["data"]))
                    print(f"[OK] {name}.png")
                else:
                    print(f"[FAIL] {name}")

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": 2,
                "mobile": True, "screenWidth": width, "screenHeight": height,
            })
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(4.0)
            print("  user =", await ev("String(S.user)"), " persona =", await ev("String(S.persona)"))
            for name, expr in SHOTS:
                await ev(expr)
                await asyncio.sleep(1.8)
                await shot(name)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8796/")
    ap.add_argument("--out", default="")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--width", type=int, default=402)
    ap.add_argument("--height", type=int, default=874)
    args = ap.parse_args()

    if not pathlib.Path(CHROME).exists():
        print("找不到 Chrome:", CHROME)
        return 1
    out = pathlib.Path(args.out) if args.out else pathlib.Path("tools/screenshots")
    out.mkdir(parents=True, exist_ok=True)
    asyncio.run(shoot(args.url, out, args.width, args.height, args.port))
    print("输出目录:", out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
