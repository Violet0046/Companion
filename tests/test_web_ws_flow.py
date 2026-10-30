"""WebSocket 消息流的端到端测试（node 里跑真实 app.js）

为什么要有这个：
之前出过一个小 bug —— 去掉气泡时间戳时把一个变量删了，
但下面的代码还在用它 → `done` 处理器抛 ReferenceError
→ **发送按钮永久变灰，能打字但发不出去**，只能关页面重进。

静态检查（node --check）发现不了这种错，
所以这里用最小 DOM stub 在 node 里真的跑一遍消息流。

覆盖：
- start / delta / done / error 全流程不抛异常
- done 之后发送按钮恢复、S.sending 归零
- 等待回复时顶栏显示"对方正在输入"，done 后恢复名字
- 连接断开时按钮也能恢复
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 最小 DOM stub：够 app.js 跑起来
STUB = r"""
const listeners = {};
function makeEl(id) {
  const el = {
    id, textContent: '', value: '', checked: false,
    dataset: {}, style: { setProperty() {}, height: '' },
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) {
        const has = this._s.has(c);
        const want = force === undefined ? !has : !!force;
        if (want) this._s.add(c); else this._s.delete(c);
        return want;
      },
    },
    children: [], childNodes: [],
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.push(c); return c; },
    removeChild(c) {}, remove() {}, focus() {}, blur() {}, click() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener(t, f) { (listeners[id] = listeners[id] || {})[t] = f; },
    removeEventListener() {},
    getBoundingClientRect() { return { top: 0, left: 0, width: 402, height: 800, bottom: 800, right: 402 }; },
    scrollTop: 0, scrollHeight: 0, clientHeight: 800, offsetTop: 0,
    scrollTo() {}, scrollIntoView() {},
    set onclick(v) { this._onclick = v; }, get onclick() { return this._onclick; },
  };
  // innerHTML 赋值时同步 textContent（把标签剥掉），模拟浏览器
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._html || ''; },
    set(v) { el._html = String(v); el.textContent = String(v).replace(/<[^>]*>/g, ''); },
  });
  Object.defineProperty(el, 'firstChild', { get() { return el.children[0] || null; } });
  Object.defineProperty(el, 'lastChild', { get() { return el.children[el.children.length - 1] || null; } });
  return el;
}
const els = {};
global.document = {
  getElementById(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl('new-' + tag); },
  addEventListener() {}, removeEventListener() {},
  documentElement: { style: { setProperty() {} } },
  body: makeEl('body'),
  visibilityState: 'visible',
};
global.window = {
  addEventListener() {}, removeEventListener() {},
  scrollY: 0, scrollTo() {},
  location: { search: '', protocol: 'http:', host: 'localhost' },
  getComputedStyle: () => ({}),
};
global.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] === undefined ? null : this._d[k]; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};
global.navigator = { serviceWorker: null };
global.location = { search: '', protocol: 'http:', host: 'localhost', href: 'http://localhost/' };
global.URL = URL; global.URLSearchParams = URLSearchParams;
global.WebSocket = class { constructor() { this.readyState = 0; } send() {} close() {} };
global.fetch = async () => { throw new Error('no network in test'); };
global.setTimeout = setTimeout; global.clearTimeout = clearTimeout;
global.console = console;
"""

HARNESS = r"""
// --- 准备 ---
const btnSend = document.getElementById('btn-send');
const topName = document.getElementById('persona-name');
const chatBox = document.getElementById('chat');
S.user = 'u1'; S.persona = '小柳'; S.personaTitle = '小柳';
S.connected = true;
S.ws = { readyState: 1, send() {}, close() {} };

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exitCode = 1; }
  else console.log('  ok - ' + msg);
}

// --- 1. start：顶栏变成"对方正在输入"，不插空气泡 ---
const before = chatBox.children.length;
handleWs({ type: 'start' });
assert(S.typing === true, 'start 后 S.typing = true');
assert(topName.textContent.indexOf('对方正在输入') === 0, '顶栏显示"对方正在输入…"');
assert(chatBox.children.length === before, 'start 时不插空气泡（那个框一直在跳，很吵）');

// --- 2. delta：才真正建气泡 ---
handleWs({ type: 'delta', content: '你好呀' });
assert(chatBox.children.length === before + 1, 'delta 时才建气泡');
assert(S.streamEl !== null, 'S.streamEl 已创建');

// --- 3. done：必须不抛异常，并且恢复发送按钮 ---
let threw = null;
try { handleWs({ type: 'done' }); } catch (e) { threw = e; }
assert(threw === null, 'done 处理器不抛异常' + (threw ? ' -> ' + threw.message : ''));
assert(S.sending === false, 'done 后 S.sending = false');
assert(btnSend.disabled === false, 'done 后发送按钮恢复可用');
assert(S.typing === false, 'done 后不再显示"正在输入"');
assert(topName.textContent === '小柳', 'done 后顶栏恢复角色名');

// --- 4. error：也要恢复 ---
handleWs({ type: 'start' });
handleWs({ type: 'delta', content: 'x' });
let threw2 = null;
try { handleWs({ type: 'error', message: 'boom' }); } catch (e) { threw2 = e; }
assert(threw2 === null, 'error 处理器不抛异常' + (threw2 ? ' -> ' + threw2.message : ''));
assert(btnSend.disabled === false, 'error 后发送按钮恢复可用');
assert(S.typing === false, 'error 后不再显示"正在输入"');

// --- 5. 连接断了：按钮也要能恢复（resetSending） ---
handleWs({ type: 'start' });
S.sending = true;
btnSend.disabled = true;
resetSending();
assert(btnSend.disabled === false, '断线后发送按钮恢复可用');
assert(S.sending === false, '断线后 S.sending = false');
assert(S.typing === false, '断线后不再显示"正在输入"');

// --- 6. 顶栏不应该出现"在线" ---
assert((topName.textContent || '').indexOf('在线') < 0, '顶栏没有"在线"字样');

// Continuous input and generation fencing.
WebSocket.OPEN = 1;
const sent = [];
S.ws = {readyState: 1, send(x) {sent.push(JSON.parse(x));}};
S.connected = true;
S.messages = [];
document.getElementById('input').value = 'first'; send();
document.getElementById('input').value = 'second'; send();
assert(sent.length === 2, 'waiting for reply still allows consecutive sends');
assert(sent[0].client_message_id !== sent[1].client_message_id, 'each input has an idempotency key');
assert(btnSend.disabled === false, 'send remains enabled');
handleWs({type:'received', client_message_id:sent[1].client_message_id, message_id:12, generation_id:'new'});
handleWs({type:'delta',generation_id:'old',content:'stale'});
assert(S.streamEl === null, 'late old-generation delta ignored');
handleWs({type:'message_done',generation_id:'new',message:{id:901,role:'assistant',content:'saved',thinking:null,created_at:'2026-10-29T10:00:00Z'}});
handleWs({type:'message_done',generation_id:'new',message:{id:901,role:'assistant',content:'saved',thinking:null,created_at:'2026-10-29T10:00:00Z'}});
assert(S.messages.filter(x=>x.id===901).length===1, 'canonical saved message replay is deduplicated');

// End the simulated page lifecycle; pending retries belong to an open page.
writeOutbox({}); stopReplyRecovery(); resetSending(); clearTimeout(S.reconnectTimer);

if (process.exitCode) { console.error('有断言失败'); process.exit(1); }
console.log('[OK] WS 消息流全流程正常');
"""


def main() -> int:
    import shutil

    from companion_ever.api.server import WEB_DIR

    node = shutil.which("node")
    if not node:
        print("[SKIP] 环境里没有 node，跳过")
        return 0

    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # 源码层面的检查
    assert "insertBefore(buildThinkBlock" not in js or "st)" not in js, \
        "插入思考块时又引用了已删除的时间戳变量 st"
    assert "function resetSending" in js, "缺少 resetSending（断线后恢复发送按钮）"
    assert "S.sending = false" in js, "缺少恢复 S.sending 的地方"

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(STUB + "\n" + js + "\n" + HARNESS)
        path = f.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise AssertionError(f"WS 消息流测试失败:\n{out}")
    print(out.strip())
    print("[ALL] WebSocket 消息流测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
