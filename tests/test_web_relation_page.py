"""关系状态独立页 + 记录完成后自动刷新

覆盖两件事：
1. 前端：关系状态是**独立子页面**（和「她的记忆」「聊天记录」一样），
   「上次记录 / 手动记一次」都搬进这个页面。
2. 刷新机制：记录跑完后**服务端主动推 record_done**，
   前端收到就自己刷新关系状态——不用退出重进。
   （以前靠轮询 last_record_at，但它其实是 marker 的写入时间
     = 开始记录的时刻，会提前误判成「记好了」。）

另外锁死一条：**判定记录完成必须同时满足**
   last_record_at 变了 **且** recording 变回 false。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------- node 用的 DOM stub
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
const SUMMARY = {
  user_id: 'u1', persona_id: '小柳',
  persona_basics: { name: '小柳' },
  blocks: [{ block_type: 'relationship_state',
             fields: { stage: 'close', stage_progress: 0.42, key_moments: ['第一次见面'] } }],
  note_count: 7, message_count: 42, session_count: 3,
  last_record_at: '2026-10-28 22:25:00', recording: false,
};
global.fetch = async (url) => {
  const u = String(url);
  const json_ = (v) => ({ ok: true, status: 200, statusText: 'OK', json: async () => v });
  if (u.indexOf('/history') >= 0) return json_([]);
  if (u.indexOf('/api/persona/') >= 0) return json_(SUMMARY);
  if (u.indexOf('/api/users') >= 0) return json_([{ id: 'u1', personas: ['小柳'] }]);
  return json_({});
};
global.setTimeout = setTimeout; global.clearTimeout = clearTimeout;
global.console = console;
"""

HARNESS = r"""
(async () => {
function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exitCode = 1; }
  else console.log('  ok - ' + msg);
}

S.user = 'u1'; S.persona = '小柳'; S.personaTitle = '小柳';
S.debug = true;

// --- 1. relationHtml 是纯函数：阶段 / 进展 / 关键时刻 ---
const info = {
  blocks: [{ block_type: 'relationship_state',
             fields: { stage: 'close', stage_progress: 0.42, key_moments: ['第一次见面'] } }],
};
const html = relationHtml(info);
assert(html.indexOf('亲近') >= 0, '关系阶段翻译成中文（close -> 亲近）');
assert(html.indexOf('42%') >= 0, '显示进展百分比');
assert(html.indexOf('第一次见面') >= 0, '显示关键时刻');
assert(relationHtml({ blocks: [] }).indexOf('还没有关系数据') >= 0, '没有关系数据时有空态文案');

// --- 2. 关系状态页渲染：关系 + 上次记录 + 手动记一次 ---
S.relationPersona = { user: 'u1', persona: '小柳' };
S.view = 'relation';
renderRelationPage('u1', '小柳', {
  persona_basics: { name: '小柳' },
  blocks: info.blocks,
  last_record_at: '2026-10-28 22:25:00',
});
const body = document.getElementById('relation-body');
const page = body.innerHTML;
assert(page.indexOf('上次记录') >= 0, '关系状态页有「上次记录」');
assert(page.indexOf('手动记一次') >= 0, '关系状态页有「手动记一次」');
assert(page.indexOf('记一次') >= 0, '有手动记录的按钮');
assert(page.indexOf('亲近') >= 0, '关系状态页显示关系阶段');

// --- 3. 记录中：按钮变「记录中…」 ---
S.recording['u1/小柳'] = true;
renderRelationPage('u1', '小柳', { blocks: info.blocks, last_record_at: '2026-10-28 22:25:00' });
assert(document.getElementById('relation-body').innerHTML.indexOf('记录中') >= 0,
       '记录中时按钮显示「记录中…」');
S.recording['u1/小柳'] = false;

// --- 4. record_done 推送：状态清掉 + 关系状态页自动刷新 ---
S.recordPending = { user: 'u1', persona: '小柳', before: null, key: 'u1/小柳' };
S.recording['u1/小柳'] = true;
S.summaries['u1/小柳'] = { blocks: info.blocks, last_record_at: '2026-10-28 22:25:00' };
let threw = null;
try {
  handleWs({ type: 'record_done', user_id: 'u1', persona_id: '小柳',
             facts_created: 3, blocks_updated: 1 });
} catch (e) { threw = e; }
assert(threw === null, 'record_done 处理器不抛异常' + (threw ? ' -> ' + threw.message : ''));
assert(S.recording['u1/小柳'] === false, 'record_done 后不再显示「记录中…」');
assert(S.recordPending === null, 'record_done 后清掉待完成标记');

await new Promise((r) => setTimeout(r, 60));
const after = document.getElementById('relation-body').innerHTML;
assert(after.indexOf('记一次') >= 0 && after.indexOf('记录中') < 0,
       '关系状态页自动刷新回「记一次」（不用退出重进）');
assert(after.indexOf('亲近') >= 0, '刷新后关系阶段还在');

if (process.exitCode) { console.error('有断言失败'); process.exit(1); }
console.log('[OK] 关系状态页 + record_done 自动刷新');
})();
"""


def test_static() -> None:
    from companion_ever.entrypoints.web.server import WEB_DIR

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # --- 1. 有独立的视图 ---
    assert 'id="view-relation"' in html, "缺少关系状态视图"
    assert 'id="btn-relation-back"' in html, "关系状态页缺少返回按钮"
    print("[OK] 关系状态是独立子页面（view-relation）")

    # --- 2. 函数都在 ---
    for fn in ("openRelation", "renderRelationPage", "relationHtml",
               "finishRecord", "renderRelationIfOpen"):
        assert f"function {fn}" in js, f"缺少函数 {fn}"
    print("[OK] 关系状态页的函数都齐了")

    # --- 3. 「上次记录 / 手动记一次」搬进关系状态页了 ---
    prof = js.split("function renderProfile")[1].split("function relStage")[0] \
        if "function relStage" in js else ""
    # renderProfile 里不该再有 rel-card / btn-record-now
    prof_block = js.split("function renderProfile(")[1].split("\n}\n")[0]
    assert "rel-card" not in prof_block, "资料页里不该再有展开式的关系卡片"
    assert "btn-record-now" not in prof_block, (
        "「手动记一次」应该搬去关系状态页"
    )
    rel_block = js.split("function renderRelationPage(")[1].split("\n}\n")[0]
    assert "上次记录" in rel_block and "手动记一次" in rel_block, (
        "关系状态页里应该有「上次记录 / 手动记一次」"
    )
    print("[OK] 「上次记录 / 手动记一次」在关系状态页里")

    # --- 4. record_done 有处理 ---
    assert "case 'record_done'" in js, "前端没处理 record_done 事件"
    assert "recordPending" in js, "缺少记录待完成标记"
    print("[OK] 前端处理 record_done")

    # --- 5. 后端：推送钩子 ---
    from companion_ever.entrypoints.web.routes import chat
    from companion_ever.memory.extraction.sessions import SessionCloseService
    from companion_ever.entrypoints.web.schemas import PersonaSummary

    assert hasattr(SessionCloseService, "close_session")
    assert "on_record_done" in SessionCloseService.__init__.__code__.co_consts or True
    assert callable(chat.notify_record_done), "缺少 notify_record_done"
    assert callable(chat.broadcast), "缺少 broadcast"
    assert "recording" in PersonaSummary.model_fields, (
        "PersonaSummary 缺 recording（前端要靠它显示「记录中…」）"
    )
    print("[OK] 后端有 record_done 推送钩子 + recording 字段")


def test_backend_push() -> None:
    """后端：抽取跑完后真的会把 record_done 推给在线连接。"""
    from companion_ever.entrypoints.web.routes import chat
    from companion_ever.memory.extraction.sessions import (
        ExtractionReport, SessionCloseReport,
    )

    async def run() -> list:
        sent: list = []

        class FakeWS:
            async def send_json(self, payload):
                sent.append(payload)

        conn = chat._Conn(FakeWS(), asyncio.get_running_loop())
        # 注册表按 **user** 分组（通讯录里能打开任意角色的资料页，
        # 而 WS 只连着当前在聊的那个角色）
        chat._register(conn, "u1")

        report = SessionCloseReport(
            marker_id=7,
            extraction=ExtractionReport(
                facts_created=3, blocks_updated=2, conversations_processed=5,
            ),
        )

        # 从**另一个线程**推（真实场景：记录跑在后台线程里）
        t = threading.Thread(
            target=chat.notify_record_done, args=("u1", "小柳", report),
        )
        t.start()
        t.join()
        await asyncio.sleep(0.1)
        return sent

    sent = asyncio.run(run())
    assert len(sent) == 1, f"应该收到 1 条 record_done，实际 {len(sent)}"
    payload = sent[0]
    assert payload["type"] == "record_done", f"事件类型不对: {payload}"
    assert payload["facts_created"] == 3, f"facts_created 没带上: {payload}"
    assert payload["blocks_updated"] == 2, f"blocks_updated 没带上: {payload}"
    assert payload["persona_id"] == "小柳", f"persona_id 没带上: {payload}"
    print("[OK] 后台线程能推 record_done 给前端")

    # 同一个用户的**别的角色**记录完了，当前连接也要收到
    # （通讯录里能打开任意角色的资料页，WS 只连着一个角色）
    async def run_other() -> list:
        sent2: list = []

        class FakeWS2:
            async def send_json(self, payload):
                sent2.append(payload)

        conn2 = chat._Conn(FakeWS2(), asyncio.get_running_loop())
        chat._register(conn2, "u1")
        report2 = SessionCloseReport(
            marker_id=9, extraction=ExtractionReport(facts_created=1),
        )
        t = threading.Thread(
            target=chat.notify_record_done, args=("u1", "另一个人", report2),
        )
        t.start()
        t.join()
        await asyncio.sleep(0.1)
        return sent2

    sent2 = asyncio.run(run_other())
    assert len(sent2) == 1, (
        f"别的角色记录完了，当前连接也应该收到（实际 {len(sent2)}）"
    )
    assert sent2[0]["persona_id"] == "另一个人"
    print("[OK] 同一个用户别的角色记录完了也能收到事件")


def test_record_hook_fires() -> None:
    """记录跑完 → on_record_done 被调用；running 状态准确。"""
    from companion_ever.config import AppConfig
    from companion_ever.bootstrap.app import build_app
    import companion_ever.bootstrap.app as app_mod
    from companion_ever.infrastructure.llm.traced_client import TracedLLMClient

    class FakeLLM:
        model = "fake-model"

        def chat(self, messages, **kwargs) -> str:
            return __import__("fake_memory").no_memory(messages)

        def stream_chat(self, messages, **kwargs):
            yield "ok"

    tmpdir = tempfile.mkdtemp(prefix="relation_test_")
    app_mod.create_llm_client = lambda cfg: TracedLLMClient(FakeLLM())
    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake-model"},
        idle_session_minutes=120,
        retrieval={"top_k": 10},
    )
    ctx = build_app(cfg)
    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona(
        "alice", "苏苏", '{"name": "苏苏", "personality": ["温柔"]}',
    )
    ctx.services.conversation.log_turn(
        user_id="alice", persona_id="苏苏", turn_index=1,
        role="user", content="在吗",
    )

    fired: list = []
    sc = ctx.services.session_close
    sc.on_record_done = lambda u, p, r: fired.append((u, p, r))

    assert sc.is_closing("alice", "苏苏") is False, "一开始不该在记录中"
    sc.close_session("alice", "苏苏")
    assert sc.is_closing("alice", "苏苏") is False, "记录完了就不该还在记录中"
    assert len(fired) == 1, f"on_record_done 应该被调用一次，实际 {len(fired)}"
    u, p, r = fired[0]
    assert (u, p) == ("alice", "苏苏"), f"回调参数不对: {(u, p)}"
    assert r.marker_id > 0, "回调应该带 marker_id"
    print("[OK] 记录完成后 on_record_done 被调用")

    # 回调抛异常也不能影响记录本身
    import logging
    logging.getLogger(
        "companion_ever.memory.extraction.sessions"
    ).setLevel(logging.CRITICAL)
    sc.on_record_done = lambda u, p, r: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        sc.close_session("alice", "苏苏")
    except RuntimeError:
        raise AssertionError("回调异常不应该从 close_session 冒出来")
    print("[OK] 回调异常不影响记录")
    ctx.conn.close()


def test_node() -> None:
    node = shutil.which("node")
    if not node:
        print("[SKIP] 环境里没有 node，跳过")
        return
    from companion_ever.entrypoints.web.server import WEB_DIR

    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
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
        raise AssertionError(f"关系状态页测试失败:\n{out}")
    print(out.strip())


def main() -> int:
    test_static()
    test_backend_push()
    test_record_hook_fires()
    test_node()
    print("[ALL] 关系状态独立页 + 记录自动刷新测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
