/* ============================================================
   Companion Ever Memory — PWA 前端逻辑
   ============================================================ */

const $ = (id) => document.getElementById(id);

const S = {
  user: null,
  persona: null,
  pin: localStorage.getItem('ce_pin') || '',
  ws: null,
  connected: false,
  sending: false,
  reconnectTimer: null,
  notes: [],
  noteFilter: 'all',
  showThinking: true,
  info: null,
  streamEl: null,
  /* 历史浏览 */
  messages: [],
  hasMore: true,
  loadingOlder: false,
  viewMode: 'live',      // live | date | jump
  viewLabel: '',
  dates: [],
  /* 搜索 */
  searchScope: 'all',
  searchTimer: null,
  appH: 0,
  kbOpen: false,
  debug: new URLSearchParams(location.search).has('debug'),
};

const PAGE = 50;
let wasAtBottom = true;

/* ---------- 工具 ---------- */

function toast(msg, ms = 2600) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escRe(s) {
  return esc(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function dayOf(iso) {
  if (!iso) return '';
  return String(iso).slice(0, 10);
}

function dayLabel(day) {
  const today = new Date();
  const p = (n) => String(n).padStart(2, '0');
  const t = `${today.getFullYear()}-${p(today.getMonth() + 1)}-${p(today.getDate())}`;
  if (day === t) return '今天';
  const y = new Date(today.getTime() - 86400000);
  const yStr = `${y.getFullYear()}-${p(y.getMonth() + 1)}-${p(y.getDate())}`;
  if (day === yStr) return '昨天';
  const parts = day.split('-');
  return `${parts[0]}年${parts[1]}月${parts[2]}日`;
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (S.pin) headers['X-Pin'] = S.pin;
  if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

const base = () => `/api/persona/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}`;

/* ---------- 视口：用 visualViewport 精确跟随「实际可见区域」 ---------- */

/**
 * 为什么不能只用 100dvh：
 * - iOS 键盘弹起时，**布局视口不变，可见视口变小**。
 *   所以固定底部的输入栏会被键盘盖住，中间那块地方全浪费。
 * - Alook 这类浏览器自带工具栏，布局视口也可能大于真实可见区域。
 *
 * 解法：用 visualViewport.height 直接定位 app 的高度。
 * 键盘弹起 → app 缩短 → 输入栏自动升到键盘上方，聊天区占满余下的空间。
 */
function syncViewport() {
  const root = document.documentElement;
  const vv = window.visualViewport;

  if (!vv) {
    // 不支持 visualViewport：退回到布局视口
    root.style.setProperty('--app-h', window.innerHeight + 'px');
    root.style.setProperty('--vv-top', '0px');
    updateDebug();
    return;
  }

  // 可见视口的底边（client 坐标）
  const vvBottom = Math.round(vv.offsetTop + vv.height);
  // 键盘占了多少
  const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
  const kbOpen = kb > 120;

  const prev = S.appH || 0;
  let h, top;
  if (kbOpen) {
    // 有键盘：app 缩到键盘上方。
    // 底边紧贴可见视口底边，顶边跟随 offsetTop。
    // 这样无论 iOS 是「缩小视口」还是「滚动视口」，app 都不会偏。
    h = Math.max(1, Math.round(vvBottom - vv.offsetTop));
    top = Math.round(vv.offsetTop);
  } else {
    // 没键盘：app 铺满整个 web 视图。
    // 取 max 是防某些浏览器的 vv.height 比布局视口小、直接用它会留白。
    h = Math.round(Math.max(vv.height, window.innerHeight));
    top = Math.max(0, vvBottom - h);
  }

  root.style.setProperty('--app-h', h + 'px');
  root.style.setProperty('--vv-top', top + 'px');
  S.appH = h;
  S.kbOpen = kbOpen;

  // 键盘弹起导致可见区域变小 → 滚到底部（新消息不能被遮住）
  if (prev && h < prev) scrollBottom();

  updateDebug();
}

/* ---------- debug 面板（访问页面加 ?debug=1） ---------- */

function updateDebug() {
  if (!S.debug) return;
  const vv = window.visualViewport;
  const app = document.querySelector('.app');
  const rect = app ? app.getBoundingClientRect() : null;
  const dvhTest = (function () {
    const d = document.createElement('div');
    d.style.cssText = 'position:absolute;height:100dvh;visibility:hidden';
    document.body.appendChild(d);
    const h = Math.round(d.getBoundingClientRect().height);
    d.remove();
    return h;
  })();
  const vvBottom = vv ? Math.round(vv.offsetTop + vv.height) : null;
  const appBottom = rect ? Math.round(rect.bottom) : null;
  const gap = (vvBottom != null && appBottom != null) ? vvBottom - appBottom : null;
  const kb = vv ? Math.max(0, window.innerHeight - vv.height - vv.offsetTop) : null;
  const lines = [
    '--- KEY ---',
    'kb           ' + (kb != null ? Math.round(kb) : '-') + (kb > 120 ? '  (键盘开)' : '  (键盘关)'),
    'vv bottom    ' + vvBottom,
    'app bottom   ' + appBottom,
    'GAP          ' + gap + (gap === 0 ? '   OK' : '   <<<< 不对齐!'),
    '--- raw ---',
    'innerHeight  ' + window.innerHeight,
    'innerWidth   ' + window.innerWidth,
    'vv.height    ' + (vv ? Math.round(vv.height) : '-'),
    'vv.offsetTop ' + (vv ? Math.round(vv.offsetTop) : '-'),
    'scrollY      ' + window.scrollY,
    'app height   ' + (rect ? Math.round(rect.height) : '-'),
    'app top      ' + (rect ? Math.round(rect.top) : '-'),
    'screen       ' + window.screen.width + 'x' + window.screen.height,
    '100dvh       ' + dvhTest,
  ];
  const el = $('debug-panel');
  if (el) {
    el.classList.remove('hidden');
    el.textContent = lines.join('\n');
  }
}

if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', syncViewport);
  window.visualViewport.addEventListener('scroll', syncViewport);
}
window.addEventListener('resize', syncViewport);
window.addEventListener('orientationchange', () => setTimeout(syncViewport, 250));
window.addEventListener('load', () => setTimeout(syncViewport, 100));
window.addEventListener('pageshow', () => setTimeout(syncViewport, 100));
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') setTimeout(syncViewport, 120);
});

/* ---------- 启动 ---------- */

async function boot() {
  try {
    const health = await api('/api/health');
    if (health.auth_required && !S.pin) { showGate(); return; }
  } catch (e) { showGate(true); return; }
  $('gate').classList.add('hidden');
  $('app').classList.remove('hidden');
  await resolveTarget();
  await Promise.all([loadPersona(), loadSettings(), loadHistory(true)]);
  connect();
  loadNotes();
  loadDates();
}

function showGate(networkError) {
  $('gate').classList.remove('hidden');
  $('app').classList.add('hidden');
  if (networkError) $('gate-error').textContent = '连不上服务器，确认 companion-cli serve 还在跑';
}

async function resolveTarget() {
  const params = new URLSearchParams(location.search);
  let users = [];
  try { users = await api('/api/users'); } catch (e) { /* ignore */ }
  if (!users.length) { $('persona-sub').textContent = '库里还没有用户，先跑 setup'; return; }
  const u = params.get('user') || localStorage.getItem('ce_user') || users[0].id;
  const person = users.find((x) => x.id === u) || users[0];
  const p = params.get('persona') || localStorage.getItem('ce_persona') || (person.personas[0] || '');
  S.user = u; S.persona = p;
  localStorage.setItem('ce_user', u);
  localStorage.setItem('ce_persona', p);
  const initial = (p || '·').slice(0, 1);
  $('avatar').textContent = initial;
  $('gate-avatar').textContent = initial;
  $('persona-name').textContent = p || '未设置角色';
}

/* ---------- 数据加载 ---------- */

async function loadPersona() {
  try {
    S.info = await api(base());
    const basics = S.info.persona_basics || {};
    if (basics.name) $('persona-name').textContent = basics.name;
    renderRelation(); renderStats();
  } catch (e) { /* ignore */ }
}

async function loadSettings() {
  try {
    const s = await api(`/api/settings/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}`);
    S.showThinking = s.show_thinking !== false;
    $('toggle-thinking').checked = S.showThinking;
  } catch (e) { /* ignore */ }
}

async function loadHistory(reset) {
  try {
    const msgs = await api(`${base()}/history?limit=${PAGE}`);
    S.messages = msgs;
    S.hasMore = msgs.length >= PAGE;
    S.viewMode = 'live';
    S.viewLabel = '';
    updateHistoryBar();
    renderChat();
    scrollBottom(true);
  } catch (e) { /* ignore */ }
}

/** 往上翻：加载更早的一页 */
async function loadOlder() {
  if (S.loadingOlder || !S.hasMore || S.viewMode !== 'live') return;
  if (!S.messages.length) return;
  S.loadingOlder = true;
  $('load-more-hint').classList.remove('hidden');
  const box = $('chat');
  const prevH = box.scrollHeight;
  const prevTop = box.scrollTop;
  try {
    const oldest = S.messages[0].id;
    const older = await api(`${base()}/history?limit=${PAGE}&before_id=${oldest}`);
    if (!older.length) { S.hasMore = false; return; }
    S.messages = older.concat(S.messages);
    S.hasMore = older.length >= PAGE;
    renderChat();
    // 保持滚动位置（否则内容跳动）
    box.scrollTop = prevTop + (box.scrollHeight - prevH);
  } catch (e) {
    /* ignore */
  } finally {
    S.loadingOlder = false;
    $('load-more-hint').classList.add('hidden');
  }
}

async function loadNotes() {
  try {
    S.notes = await api(`${base()}/notes?limit=200`);
    renderNotes();
  } catch (e) { /* ignore */ }
}

async function loadDates() {
  try {
    S.dates = await api(`${base()}/dates`);
    renderDates();
  } catch (e) { /* ignore */ }
}

/* ---------- 渲染 ---------- */

function renderChat() {
  const box = $('chat');
  box.innerHTML = '';
  if (!S.messages.length) {
    const hint = document.createElement('div');
    hint.className = 'empty';
    hint.textContent = '还没有对话过。说点什么吧。';
    box.appendChild(hint);
    return;
  }
  let lastDay = null;
  S.messages.forEach((m) => {
    const d = dayOf(m.created_at);
    if (d && d !== lastDay) {
      lastDay = d;
      const sep = document.createElement('div');
      sep.className = 'day-sep';
      sep.textContent = dayLabel(d);
      box.appendChild(sep);
    }
    box.appendChild(renderMsg(m.role, m.content, m.thinking, m.created_at, m.id));
  });
}

function renderMsg(role, content, thinking, createdAt, id) {
  const el = document.createElement('div');
  el.className = 'msg ' + (role === 'user' ? 'me' : 'them');
  if (id) el.dataset.id = id;
  const text = document.createElement('div');
  text.textContent = content;
  el.appendChild(text);

  if (thinking) {
    const det = document.createElement('details');
    det.className = 'think';
    const sum = document.createElement('summary');
    sum.textContent = '她当时在想什么';
    const body = document.createElement('div');
    body.className = 'think-body';
    body.textContent = thinking;
    det.appendChild(sum); det.appendChild(body);
    el.appendChild(det);
  }
  if (createdAt) {
    const st = document.createElement('span');
    st.className = 'stamp';
    st.textContent = fmtTime(createdAt);
    el.appendChild(st);
  }
  return el;
}

function scrollBottom(instant) {
  const box = $('chat');
  box.scrollTo({ top: box.scrollHeight, behavior: instant ? 'auto' : 'smooth' });
}

function renderRelation() {
  const card = $('rel-card');
  if (!S.info) { card.innerHTML = ''; return; }
  const blocks = S.info.blocks || [];
  const rel = blocks.find((b) => b.block_type === 'relationship_state');
  const prof = blocks.find((b) => b.block_type === 'user_profile');
  let html = '';
  if (rel) {
    const f = rel.fields || {};
    const stageMap = { stranger: '初识', familiar: '熟悉', close: '亲近', deep_bond: '深交' };
    const stage = stageMap[f.stage] || f.stage || '—';
    const prog = Math.round((f.stage_progress || 0) * 100);
    html += `<div class="row"><span class="k">关系</span><span class="v">${esc(stage)}</span></div>`;
    html += `<div class="row"><span class="k">进展</span><span class="v">${prog}%</span></div>`;
    html += `<div class="meter"><i style="width:${prog}%"></i></div>`;
    const mom = f.key_moments || [];
    if (mom.length) {
      html += '<div class="moments"><ul>' + mom.slice(-6).map((m) => `<li>${esc(m)}</li>`).join('') + '</ul></div>';
    }
  }
  if (prof) {
    const f = prof.fields || {};
    const arr = (v) => (Array.isArray(v) ? v : v ? [v] : []);
    const p = arr(f.preferences).slice(0, 3);
    if (p.length) {
      html += `<div class="row" style="margin-top:8px"><span class="k">偏好</span><span class="v">${esc(p.join('、'))}</span></div>`;
    }
  }
  card.innerHTML = html || '<div class="empty">还没有关系数据</div>';
}

function renderStats() {
  if (!S.info) return;
  $('stat-line').innerHTML = `记忆 ${S.info.note_count} 条 · 消息 ${S.info.message_count} 条 · 共同走过 ${S.info.session_count} 段对话`;
}

function renderDates() {
  const el = $('date-list');
  if (!S.dates.length) { el.innerHTML = '<div class="empty">还没有历史对话</div>'; return; }
  el.innerHTML = S.dates.map((d) => `
    <div class="date-row" data-date="${esc(d.date)}">
      <span>${esc(dayLabel(d.date))}</span>
      <span class="n">${d.count} 条 ›</span>
    </div>`).join('');
  el.querySelectorAll('.date-row').forEach((r) => {
    r.onclick = () => jumpToDate(r.dataset.date);
  });
}

function renderNotes() {
  const list = $('note-list');
  const chips = $('note-filters');
  const counts = {};
  S.notes.forEach((n) => { counts[n.type] = (counts[n.type] || 0) + 1; });
  $('note-count').textContent = `（${S.notes.length}）`;

  const types = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  let chipHtml = `<button class="chip${S.noteFilter === 'all' ? ' active' : ''}" data-t="all">全部</button>`;
  types.slice(0, 6).forEach((t) => {
    chipHtml += `<button class="chip${S.noteFilter === t ? ' active' : ''}" data-t="${esc(t)}">${esc(t)} ${counts[t]}</button>`;
  });
  chips.innerHTML = chipHtml;
  chips.querySelectorAll('.chip').forEach((c) => {
    c.onclick = () => { S.noteFilter = c.dataset.t; renderNotes(); };
  });

  const filtered = S.noteFilter === 'all' ? S.notes : S.notes.filter((n) => n.type === S.noteFilter);
  if (!filtered.length) { list.innerHTML = '<div class="empty">还没有这类记忆</div>'; return; }
  list.innerHTML = filtered.map((n) => `
    <div class="note" data-type="${esc(n.type)}">
      <div class="note-head">
        <span class="note-type">${esc(n.type)}</span>
        <span class="note-imp">${(n.importance || 0).toFixed(2)}</span>
      </div>${esc(n.content)}
    </div>`).join('');
}

/* ---------- 历史跳转 ---------- */

function updateHistoryBar() {
  const bar = $('history-bar');
  if (S.viewMode === 'live') { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  $('history-label').textContent = S.viewLabel || '正在查看历史聊天';
}

async function jumpToDate(date) {
  try {
    const msgs = await api(`${base()}/history?date=${date}&limit=500`);
    S.messages = msgs;
    S.viewMode = 'date';
    S.viewLabel = `正在查看 ${dayLabel(date)} 的对话`;
    S.hasMore = false;
    updateHistoryBar();
    renderChat();
    scrollBottom(true);
    closeDrawer();
    toast(S.viewLabel);
  } catch (e) { toast('加载失败'); }
}

async function jumpToMessage(id) {
  try {
    const msgs = await api(`${base()}/history?after_id=${id}&limit=500`);
    S.messages = msgs;
    S.viewMode = 'jump';
    S.viewLabel = '正在查看历史聊天';
    S.hasMore = false;
    updateHistoryBar();
    renderChat();
    closeSearch();
    // 滚到目标消息
    setTimeout(() => {
      const el = $('chat').querySelector(`.msg[data-id="${id}"]`);
      if (el) el.scrollIntoView({ block: 'center' });
    }, 60);
    toast('已跳转到那条消息');
  } catch (e) { toast('跳转失败'); }
}

async function backToLive() {
  await loadHistory(true);
  toast('已回到最新');
}

/* ---------- 搜索 ---------- */

function openSearch() {
  $('search-panel').classList.remove('hidden');
  $('search-input').focus();
}
function closeSearch() {
  $('search-panel').classList.add('hidden');
}

async function doSearch(q) {
  const box = $('search-results');
  if (!q.trim()) {
    box.innerHTML = '<div class="empty">输入关键词，搜索过去的聊天和她记得的事…</div>';
    return;
  }
  try {
    const hits = await api(`${base()}/search?q=${encodeURIComponent(q)}&scope=${S.searchScope}&limit=50`);
    if (!hits.length) {
      box.innerHTML = '<div class="empty">没有找到相关内容</div>';
      return;
    }
    const re = new RegExp(escRe(q), 'gi');
    const groups = { message: [], memory: [] };
    hits.forEach((h) => { (groups[h.kind] || (groups[h.kind] = [])).push(h); });

    let html = '';
    const label = { message: '聊天记录', memory: '她记得的' };
    Object.keys(groups).forEach((kind) => {
      if (!groups[kind].length) return;
      html += `<div class="drawer-label" style="margin:12px 0 6px">${label[kind] || kind}（${groups[kind].length}）</div>`;
      groups[kind].forEach((h) => {
        const text = esc(h.content).replace(re, (m) => `<mark>${m}</mark>`);
        const meta = h.kind === 'memory'
          ? `${esc(h.type || '')}`
          : `${esc(h.role === 'user' ? '我' : '她')} · ${fmtTime(h.created_at)}`;
        const jump = h.kind === 'message' ? ` data-jump="${h.id}"` : '';
        html += `<div class="hit" data-kind="${h.kind}"${jump}>
          <div class="hit-head"><span class="hit-meta">${meta}</span></div>
          <div class="hit-text">${text.slice(0, 400)}</div>
        </div>`;
      });
    });
    box.innerHTML = html;
    box.querySelectorAll('.hit[data-jump]').forEach((el) => {
      el.onclick = () => jumpToMessage(el.dataset.jump);
    });
  } catch (e) {
    box.innerHTML = `<div class="empty">搜索失败：${esc(e.message || '')}</div>`;
  }
}

/* ---------- WebSocket ---------- */

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const p = new URLSearchParams({ user: S.user, persona: S.persona });
  if (S.pin) p.set('pin', S.pin);
  return `${proto}//${location.host}/ws/chat?${p.toString()}`;
}

function setStatus(text, cls) {
  const el = $('persona-sub');
  el.textContent = text;
  el.className = 'topbar-sub ' + (cls || '');
}

function connect() {
  if (!S.user || !S.persona) return;
  setStatus('连接中…', '');
  try { if (S.ws) S.ws.close(); } catch (e) { /* ignore */ }
  const ws = new WebSocket(wsUrl());
  S.ws = ws;

  ws.onopen = () => { S.connected = true; setStatus('在线', 'online'); };
  ws.onclose = () => {
    S.connected = false;
    setStatus('已断开 · 重连中…', 'offline');
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = setTimeout(connect, 2500);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    handleWs(m);
  };
}

function handleWs(m) {
  switch (m.type) {
    case 'ready':
      setStatus('在线', 'online');
      break;
    case 'start': {
      const box = $('chat');
      const empty = box.querySelector('.empty');
      if (empty) empty.remove();
      const el = renderMsg('assistant', '', null, null, null);
      const cursor = document.createElement('span');
      cursor.className = 'cursor';
      el.firstChild.appendChild(cursor);
      box.appendChild(el);
      S.streamEl = el;
      scrollBottom();
      break;
    }
    case 'delta': {
      if (!S.streamEl) break;
      const text = S.streamEl.firstChild;
      const cur = text.querySelector('.cursor');
      if (cur) cur.remove();
      text.textContent += m.content || '';
      scrollBottom();
      break;
    }
    case 'done': {
      if (S.streamEl) {
        const cur = S.streamEl.querySelector('.cursor');
        if (cur) cur.remove();
        const st = document.createElement('span');
        st.className = 'stamp';
        st.textContent = fmtTime(new Date().toISOString());
        S.streamEl.appendChild(st);
        // 同步进 S.messages（保持内存态一致）
        S.messages.push({
          id: Date.now(), role: 'assistant',
          content: S.streamEl.firstChild.textContent,
          created_at: new Date().toISOString(),
        });
      }
      S.streamEl = null;
      S.sending = false;
      $('btn-send').disabled = false;
      break;
    }
    case 'error': {
      if (S.streamEl) { S.streamEl.remove(); S.streamEl = null; }
      S.sending = false;
      $('btn-send').disabled = false;
      toast('出错了：' + (m.message || '未知错误'), 4000);
      break;
    }
    case 'session_closed': {
      const bits = [];
      if (m.facts_created) bits.push(`记住了 ${m.facts_created} 件事`);
      if (m.blocks_updated) bits.push(`更新了 ${m.blocks_updated} 项状态`);
      toast(bits.length ? bits.join('，') : '这次聊天已结束', 3200);
      loadPersona(); loadNotes(); loadDates();
      if (S.viewMode === 'live') loadHistory(true);
      break;
    }
  }
}

/* ---------- 发送 ---------- */

function send() {
  const input = $('input');
  const text = input.value.trim();
  if (!text) return;
  if (!S.connected || !S.ws || S.ws.readyState !== WebSocket.OPEN) { toast('还没连上，等一下'); return; }
  if (S.sending) return;

  const box = $('chat');
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();
  const now = new Date().toISOString();
  const el = renderMsg('user', text, null, now, null);
  box.appendChild(el);
  S.messages.push({ id: Date.now(), role: 'user', content: text, created_at: now });
  input.value = '';
  input.style.height = 'auto';

  S.ws.send(JSON.stringify({ type: 'chat', content: text }));
  S.sending = true;
  $('btn-send').disabled = true;
  scrollBottom();
  // 键盘弹起时视口会变小，待底部重新滚动，避免新消息被掩住
  setTimeout(() => {
    syncViewport();
    if (wasAtBottom) scrollBottom();
  }, 320);
}

async function closeSession() {
  if (!S.connected) { toast('还没连上'); return; }
  const btn = $('btn-close-session');
  btn.disabled = true;
  btn.textContent = '正在整理…';
  try {
    await api('/api/session/close', {
      method: 'POST',
      body: JSON.stringify({ user_id: S.user, persona_id: S.persona }),
    });
    toast('这次聊天已结束', 2000);
    closeDrawer();
  } catch (e) {
    toast('结束失败：' + (e.message || ''), 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = '结束这次对话 · 让她记住';
  }
}

/* ---------- 抽屉 ---------- */

function openDrawer() { $('drawer').classList.add('open'); $('drawer').setAttribute('aria-hidden', 'false'); $('scrim').classList.remove('hidden'); }
function closeDrawer() { $('drawer').classList.remove('open'); $('drawer').setAttribute('aria-hidden', 'true'); $('scrim').classList.add('hidden'); }

/* ---------- 事件绑定 ---------- */

$('btn-send').onclick = send;
$('btn-close-session').onclick = closeSession;
$('btn-drawer').onclick = openDrawer;
$('btn-drawer-close').onclick = closeDrawer;
$('scrim').onclick = closeDrawer;
$('btn-search').onclick = openSearch;
$('btn-search-close').onclick = closeSearch;
$('btn-back-live').onclick = backToLive;

$('input').addEventListener('input', (e) => {
  const el = e.target;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
});
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});

/* iOS 焦点输入框时会滚动文档把输入框“露出来”。
   我们的 app 是 position:fixed，文档一滚它就被顶出屏幕。
   常量屏幕时把滚动量拉回来。 */
$('input').addEventListener('focus', () => {
  [0, 60, 180].forEach((d) => setTimeout(() => {
    if (window.scrollY !== 0) window.scrollTo(0, 0);
    syncViewport();
  }, d));
});

$('search-input').addEventListener('input', (e) => {
  clearTimeout(S.searchTimer);
  const q = e.target.value;
  S.searchTimer = setTimeout(() => doSearch(q), 300);
});
$('search-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { clearTimeout(S.searchTimer); doSearch(e.target.value); }
});
document.querySelectorAll('.search-tabs .chip').forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll('.search-tabs .chip').forEach((x) => x.classList.remove('active'));
    c.classList.add('active');
    S.searchScope = c.dataset.scope;
    doSearch($('search-input').value);
  };
});

$('toggle-thinking').addEventListener('change', async (e) => {
  S.showThinking = e.target.checked;
  try {
    await api(`/api/settings/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}`, {
      method: 'PUT',
      body: JSON.stringify({ show_thinking: S.showThinking }),
    });
    toast(S.showThinking ? '会显示她的思考过程' : '不显示思考过程');
    loadHistory(true);
  } catch (err) { toast('设置失败'); }
});

$('pin-submit').onclick = async () => {
  S.pin = $('pin-input').value.trim();
  try {
    await api('/api/health');
    localStorage.setItem('ce_pin', S.pin);
    $('pin-input').value = '';
    $('gate-error').textContent = '';
    boot();
  } catch (e) {
    $('gate-error').textContent = 'PIN 不对';
    S.pin = localStorage.getItem('ce_pin') || '';
  }
};
$('pin-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('pin-submit').click(); });

/* 往上滑到顶 → 加载更早的 */
$('chat').addEventListener('scroll', () => {
  const box = $('chat');
  if (box.scrollTop < 80) loadOlder();
  // 记住是否贴在底部（发送时不要把用户拉走）
  wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
});

/* 点聊天区 → 收起键盘
   手机用户的习惯：点聊天区就把键盘收起来。
   这样保护下的空间能够多展示几条聊天记录。 */
$('chat').addEventListener('click', (e) => {
  if (e.target.closest('details, summary, a, button')) return;
  const input = $('input');
  if (document.activeElement === input) {
    input.blur();
    setTimeout(syncViewport, 80);
  }
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (!S.connected) connect();
    else if (S.viewMode === 'live') loadHistory(true);
  }
});

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* 非 HTTPS 会失败 */ });
  });
}

syncViewport();
boot();
