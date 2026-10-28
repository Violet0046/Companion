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
};

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

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
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

/* ---------- 启动 ---------- */

async function boot() {
  try {
    const health = await api('/api/health');
    if (health.auth_required && !S.pin) {
      showGate();
      return;
    }
  } catch (e) {
    showGate(true);
    return;
  }
  $('gate').classList.add('hidden');
  $('app').classList.remove('hidden');
  await resolveTarget();
  await Promise.all([loadPersona(), loadSettings(), loadHistory()]);
  connect();
  loadNotes();
}

function showGate(networkError) {
  $('gate').classList.remove('hidden');
  $('app').classList.add('hidden');
  if (networkError) {
    $('gate-error').textContent = '连不上服务器，确认 companion-cli serve 还在跑';
  }
}

async function resolveTarget() {
  const params = new URLSearchParams(location.search);
  let users = [];
  try { users = await api('/api/users'); } catch (e) { /* ignore */ }
  if (!users.length) {
    $('persona-sub').textContent = '库里还没有用户，先跑 setup';
    return;
  }
  const u = params.get('user') || localStorage.getItem('ce_user') || users[0].id;
  const person = users.find((x) => x.id === u) || users[0];
  const p = params.get('persona') || localStorage.getItem('ce_persona') || (person.personas[0] || '');
  S.user = u;
  S.persona = p;
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
    S.info = await api(`/api/persona/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}`);
    const basics = S.info.persona_basics || {};
    if (basics.name) $('persona-name').textContent = basics.name;
    renderRelation();
    renderStats();
  } catch (e) { /* ignore */ }
}

async function loadSettings() {
  try {
    const s = await api(`/api/settings/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}`);
    S.showThinking = s.show_thinking !== false;
    $('toggle-thinking').checked = S.showThinking;
  } catch (e) { /* ignore */ }
}

async function loadHistory() {
  const box = $('chat');
  try {
    const msgs = await api(
      `/api/persona/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}/history?limit=50`
    );
    box.innerHTML = '';
    if (!msgs.length) {
      const hint = document.createElement('div');
      hint.className = 'empty';
      hint.textContent = '还没有对话过。说点什么吧。';
      box.appendChild(hint);
      return;
    }
    msgs.forEach((m) => box.appendChild(renderMsg(m.role, m.content, m.thinking, m.created_at)));
    scrollBottom(true);
  } catch (e) { /* ignore */ }
}

async function loadNotes() {
  try {
    S.notes = await api(
      `/api/persona/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}/notes?limit=200`
    );
    renderNotes();
  } catch (e) { /* ignore */ }
}

/* ---------- 渲染 ---------- */

function renderMsg(role, content, thinking, createdAt) {
  const el = document.createElement('div');
  el.className = 'msg ' + (role === 'user' ? 'me' : 'them');
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
    det.appendChild(sum);
    det.appendChild(body);
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
    const stageMap = { stranger: '初识', familiar: '熟悉', close: '亲近', deep_bond: ' deeply ' };
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
  const el = $('stat-line');
  el.innerHTML = `记忆 ${S.info.note_count} 条 · 消息 ${S.info.message_count} 条 · 共同走过 ${S.info.session_count} 段对话`;
}

function renderNotes() {
  const list = $('note-list');
  const chips = $('note-filters');
  const counts = {};
  S.notes.forEach((n) => { counts[n.type] = (counts[n.type] || 0) + 1; });
  $('note-count').textContent = `（${S.notes.length}）`;

  const types = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  let chipHtml = '<button class="chip' + (S.noteFilter === 'all' ? ' active' : '') + '" data-t="all">全部</button>';
  types.slice(0, 6).forEach((t) => {
    chipHtml += `<button class="chip${S.noteFilter === t ? ' active' : ''}" data-t="${esc(t)}">${esc(t)} ${counts[t]}</button>`;
  });
  chips.innerHTML = chipHtml;
  chips.querySelectorAll('.chip').forEach((c) => {
    c.onclick = () => { S.noteFilter = c.dataset.t; renderNotes(); };
  });

  const filtered = S.noteFilter === 'all' ? S.notes : S.notes.filter((n) => n.type === S.noteFilter);
  if (!filtered.length) {
    list.innerHTML = '<div class="empty">还没有这类记忆</div>';
    return;
  }
  list.innerHTML = filtered.map((n) => `
    <div class="note" data-type="${esc(n.type)}">
      <div class="note-head">
        <span class="note-type">${esc(n.type)}</span>
        <span class="note-imp">${(n.importance || 0).toFixed(2)}</span>
      </div>
      ${esc(n.content)}
    </div>`).join('');
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

  ws.onopen = () => {
    S.connected = true;
    setStatus('在线', 'online');
  };

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
      const el = renderMsg('assistant', '', null, null);
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
      loadPersona(); loadNotes(); loadHistory();
      break;
    }
  }
}

/* ---------- 发送 ---------- */

function send() {
  const input = $('input');
  const text = input.value.trim();
  if (!text) return;
  if (!S.connected || !S.ws || S.ws.readyState !== WebSocket.OPEN) {
    toast('还没连上，等一下');
    return;
  }
  if (S.sending) return;

  const box = $('chat');
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();
  box.appendChild(renderMsg('user', text, null, new Date().toISOString()));
  input.value = '';
  input.style.height = 'auto';

  S.ws.send(JSON.stringify({ type: 'chat', content: text }));
  S.sending = true;
  $('btn-send').disabled = true;
  scrollBottom();
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
  } catch (e) {
    toast('结束失败：' + (e.message || ''), 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = '结束这次对话 · 让她记住';
  }
}

/* ---------- 抽屉 ---------- */

function openDrawer() {
  $('drawer').classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
  $('scrim').classList.remove('hidden');
}
function closeDrawer() {
  $('drawer').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
  $('scrim').classList.add('hidden');
}

/* ---------- 事件绑定 ---------- */

$('btn-send').onclick = send;
$('btn-close-session').onclick = closeSession;
$('btn-drawer').onclick = openDrawer;
$('btn-drawer-close').onclick = closeDrawer;
$('scrim').onclick = closeDrawer;

$('input').addEventListener('input', (e) => {
  const el = e.target;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
});
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});

$('toggle-thinking').addEventListener('change', async (e) => {
  S.showThinking = e.target.checked;
  try {
    await api(`/api/settings/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}`, {
      method: 'PUT',
      body: JSON.stringify({ show_thinking: S.showThinking }),
    });
    toast(S.showThinking ? '会显示她的思考过程' : '不显示思考过程');
    loadHistory();
  } catch (err) {
    toast('设置失败');
  }
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

/* 页面回到前台时刷新一下（手机上切回来很常见） */
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (!S.connected) connect();
    else loadHistory();
  }
});

/* Service Worker（离线壳 + 推送） */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* 非 HTTPS 时会失败，忽略 */ });
  });
}

boot();
