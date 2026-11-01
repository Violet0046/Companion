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
  showThinking: true,
  info: null,
  streamEl: null,
  replyState: null,
  replyStatusEl: null,
  historyRequest: 0,
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
  /* 新消息角标：用户往上翻时，新到的消息数 */
  newCount: 0,
  /* 当前视图：messages / contacts / me / chat / profile / memory */
  view: 'messages',
  /* 所有用户及其角色（/api/users） */
  users: [],
  /* 聊天列表数据 */
  convs: [],
  /* 各角色的汇总信息（note_count 等） */
  summaries: {},
  /* 正在看资料 / 记忆 / 关系状态的角色 */
  profilePersona: null,
  memoryPersona: null,
  relationPersona: null,
  /* 正在记录中的角色：key -> true（「记一次」按钮变「记录中…」） */
  recording: {},
  /* 本次手动记录还没回来的标记（WS 推送 record_done 时清掉） */
  recordPending: null,
  memoryNotes: [],
  memoryFilter: 'all',
  memoryAction: null,
  convQuery: '',
  contactQuery: '',
  automaticMemory: true,
  /* 日历 */
  calUser: null,
  calPersona: null,
  calCurMonth: '',
  calMonths: [],      // 已加载的月份（从早到晚）
  calLoading: false,
  calDates: {},
  /* 本轮流式输出里收到的思考过程 */
  streamThinking: '',
  /* 距离上一条时间分隔条已经过了几条消息 */
  sinceLastSep: 0,
  /* 顶栏显示的名字（等回复时会临时变成"对方正在输入…"） */
  personaTitle: '',
  /* 正在等 TA 回复 */
  typing: false,
  /* 发送看门狗的定时器 */
  sendTimer: null,
  debug: new URLSearchParams(location.search).has('debug'),
};

const PAGE = 50;
let bootRequest = 0;
let wasAtBottom = true;

/* ---------- 工具 ---------- */

function toast(msg, ms = 2600) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  // 对话框开着时提示挪到顶部——否则被面板挡在下面，根本看不见
  const dlg = $('dialog-persona');
  el.classList.toggle('toast-top', !!(dlg && !dlg.classList.contains('hidden')));
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
  if (S.pin && headers['X-Pin'] === undefined) headers['X-Pin'] = S.pin;
  if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const controller = new AbortController();
  const timer = setTimeout(()=>controller.abort(),15000);
  try {
    const res = await fetch(path, Object.assign({cache:'no-store'}, opts, { headers, signal:opts.signal || controller.signal }));
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
      const err = new Error(detail);
      err.status = res.status;
      if (res.status === 401 && path !== '/api/auth/check' && (headers['X-Pin'] || '') === S.pin) {
        invalidateAccess();
      }
      throw err;
    }
    return await res.json();
  } finally { clearTimeout(timer); }
}

const base = () => `/api/persona/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}`;

/* ============================================================
   页面层：消息 / 通讯录 / 我 / 会话 / 角色资料 / TA 的记忆
   页面入口与渲染
   ============================================================ */

const TAB_VIEWS = ['messages', 'contacts', 'me'];
const enc = (s) => encodeURIComponent(s);
const escAttr = (s) => esc(s).replace(/"/g, '&quot;');
const pkey = (user, persona) => `${user}/${persona}`;

/** 切换视图。子页面（会话/资料/记忆）不显示底部 tab */
function showView(name) {
  S.view = name;
  document.querySelectorAll('.view').forEach((v) => {
    v.classList.toggle('active', v.id === 'view-' + name);
  });
  $('tabbar').classList.toggle('hidden', !TAB_VIEWS.includes(name));
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.view === name);
  });
  if (name === 'chat') anchorBottom();
  if (name === 'me') window.CompanionNotifications?.refresh();
}

/* ---------- 角色清单 ---------- */

async function loadUsers() {
  try {
    S.users = await api('/api/users');
  } catch (e) {
    S.users = [];
  }
  S.archived = [];
  if (S.user) {
    try {
      S.archived = await api(`/api/archived-personas/${enc(S.user)}`);
    } catch (e) { /* 已删除清单拿不到就先不显示 */ }
  }
}

function allPersonas() {
  const out = [];
  (S.users || []).forEach((u) => {
    (u.personas || []).forEach((p) => out.push({ user: u.id, persona: p }));
  });
  return out;
}

/** 已读标记：记住每个角色读到哪条，用来算未读数 */
function readMark(user, persona) {
  const v = localStorage.getItem('ce_read_' + pkey(user, persona));
  return v == null ? null : Number(v);
}
function setReadMark(user, persona, id) {
  if (Number.isSafeInteger(id) && id >= 0 && id > (readMark(user,persona) ?? -1)) {
    localStorage.setItem('ce_read_' + pkey(user, persona), String(id));
  }
}

function markVisibleChatRead() {
  if (S.view !== 'chat' || document.visibilityState !== 'visible' || S.viewMode !== 'live' || !wasAtBottom) return;
  const replies = S.messages.filter(m => m.role === 'assistant' && Number.isSafeInteger(m.id) && !m.localOnly);
  if (!replies.length) return;
  setReadMark(S.user,S.persona,Math.max(...replies.map(m => m.id)));
  const mark = readMark(S.user,S.persona);
  reportRead(S.user,S.persona,mark);
  const conv = S.convs.find(c => c.user === S.user && c.persona === S.persona);
  if (conv && (conv.unreadThrough ?? conv.last?.id ?? 0) <= mark) {
    conv.unread = 0;
    renderConvs();
  }
}

const readReceipts = new Map();
function reportRead(user,persona,id) {
  const key=pkey(user,persona), state=readReceipts.get(key)||{sent:0,busy:false};
  readReceipts.set(key,state);
  if (state.busy || id<=state.sent) return;
  state.busy=true;
  api(`/api/persona/${enc(user)}/${enc(persona)}/read`,{method:'POST',body:JSON.stringify({message_id:id})})
    .then(()=>{state.sent=Math.max(state.sent,id);})
    .catch(()=>{})
    .finally(()=>{state.busy=false;if (state.sent===id && S.user===user && S.persona===persona) markVisibleChatRead();});
}

async function fetchUnread(user,persona) {
  // Read position can advance while the request is in flight. Recheck against
  // the current cursor so a late count cannot resurrect already read badges.
  for (;;) {
    const after = readMark(user,persona) ?? 0;
    const result = await api(`/api/persona/${enc(user)}/${enc(persona)}/unread-count?after_id=${after}`);
    const current = readMark(user,persona) ?? 0;
    if (current >= result.latest_id) return {count:0,latest_id:result.latest_id};
    if (current === after) return result;
  }
}

/** 按名字散列出一个稳定的头像配色 */
function avaClass(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return ['ava-s1', 'ava-s2', 'ava-s3'][h % 3];
}

/** 列表上的时间：今天显示时刻，昨天显示"昨天 21:30"，一周内显示星期 */
function fmtConvTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const now = new Date();
  const p = (n) => String(n).padStart(2, '0');
  if (d.toDateString() === now.toDateString()) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  const y = new Date(now); y.setDate(y.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `昨天 ${p(d.getHours())}:${p(d.getMinutes())}`;
  if (now - d < 7 * 86400000) return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()];
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

/** "多久之前" */
function fmtAgo(iso) {
  if (!iso) return '还没有记录过';
  const d = new Date(String(iso).replace(' ', 'T'));
  if (isNaN(d)) return '还没有记录过';
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return '刚刚';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

/** 只刷新一个角色的会话摘要（收到新消息时用） */
async function refreshConv(user, persona) {
  const idx = S.convs.findIndex((x) => x.user === user && x.persona === persona);
  if (idx < 0) return;
  S.convs[idx] = await fetchConv(S.convs[idx]);
  renderConvs();
}

/* ---------- "对方正在输入" + 发送看门狗 ---------- */

/**
 * 顶栏中间的提示。
 *
 * 以前是往聊天区里插一个空气泡 + 闪烁光标（微信不是这么干的，
 * 那个框一直跳很吵），现在改成微信那种：顶栏名字变成"对方正在输入…"。
 */
function setTyping(on) {
  S.typing = !!on;
  const el = $('persona-name');
  if (!el) return;
  if (on) {
    el.innerHTML = '对方正在输入<span class="dots"><i></i><i></i><i></i></span>';
  } else {
    el.textContent = S.personaTitle || S.persona || '';
  }
}

/**
 * 发送看门狗。
 *
 * 以前发完消息要把发送按钮置灰（等回复），但如果这期间
 * **WebSocket 断了**，就永远等不到 'done' → 按钮永久变灰，
 * 能打字但发不出去，只能关掉页面重进。
 * 现在：连接断开 / 服务端不回 / 超时，三条路都会把按钮恢复。
 */
const SEND_WATCHDOG_MS = 120000;

function startSendWatchdog() {
  stopSendWatchdog();
  S.sendTimer = setTimeout(() => {
    S.sendTimer = null;
    if (S.sending) {
      S.sending = false;
      $('btn-send').disabled = false;
      setTyping(false);
      recoverReply(true);
    }
  }, SEND_WATCHDOG_MS);
}

function stopSendWatchdog() {
  if (S.sendTimer) { clearTimeout(S.sendTimer); S.sendTimer = null; }
}

/** 恢复"可以发送"的状态（连接断了 / 出错了 / 超时了都走这里） */
function resetSending() {
  S.sending = false;
  const btn = $('btn-send');
  if (btn) btn.disabled = false;
  stopSendWatchdog();
  setTyping(false);
}

/* ---------- 消息（Tab 1） ---------- */

/** 取一个角色的会话摘要：最后一条消息 + 汇总 + 未读数 */
async function fetchConv(p) {
  const item = {
    user: p.user, persona: p.persona, last: null, unread: 0, summary: null, error: null,
  };
  // 两个请求**各自独立**失败 —— 以前用 Promise.all，一个挂了另一个也白费，
  // 结果错误被吞掉，界面上显示成"还没有聊过"（其实是有记录的）。
  const [msgs, sum] = await Promise.allSettled([
    api(`/api/persona/${enc(p.user)}/${enc(p.persona)}/history?limit=20&include_thinking=0`),
    api(`/api/persona/${enc(p.user)}/${enc(p.persona)}`),
  ]);
  if (msgs.status === 'fulfilled') {
    const list = msgs.value;
    item.last = list.length ? list[list.length - 1] : null;
    if (readMark(p.user,p.persona) == null) setReadMark(p.user,p.persona,item.last?.id ?? 0);
    try {
      const unread = await fetchUnread(p.user,p.persona);
      item.unread = unread.count;
      item.unreadThrough = unread.latest_id;
    } catch (_) {
      // Preserve the last known count on a failed count request.
      item.unread = p.unread || 0;
      item.unreadThrough = p.unreadThrough;
    }
  } else {
    item.error = (msgs.reason && msgs.reason.message) || '加载失败';
    console.error('会话加载失败', p, msgs.reason);
  }
  if (sum.status === 'fulfilled') {
    item.summary = sum.value;
    S.summaries[pkey(p.user, p.persona)] = sum.value;
  } else {
    console.error('角色汇总加载失败', p, sum.reason);
  }
  return item;
}

async function loadConvs() {
  const ps = allPersonas();
  S.convs = await Promise.all(ps.map(fetchConv));
  renderConvs();
  renderMe();
  // 有加载失败的（比如服务器刚重启时请求打不通）→ 自动重试
  if (S.convs.some((c) => c.error)) {
    S.convRetry = (S.convRetry || 0) + 1;
    if (S.convRetry <= 5) {
      setTimeout(() => loadConvs(), 2000);
    } else {
      S.convRetry = 0;
    }
  } else {
    S.convRetry = 0;
  }
}

function renderConvs() {
  const box = $('conv-list');
  if (!box) return;
  for (const c of S.convs) {
    if (Number.isSafeInteger(c.unreadThrough) && (readMark(c.user,c.persona) ?? -1) >= c.unreadThrough) c.unread = 0;
  }
  const q = (S.convQuery || '').trim().toLowerCase();
  const items = S.convs.filter((c) => {
    if (!q) return true;
    const last = c.last ? (c.last.content || '') : '';
    return c.persona.toLowerCase().includes(q) || last.toLowerCase().includes(q);
  });
  if (!items.length) {
    box.innerHTML = `<div class="empty">${q ? '没有匹配的会话' : '还没有角色'}</div>`;
    return;
  }
  box.innerHTML = items.map((c) => {
    const last = c.last;
    const preview = c.error
      ? '加载失败，正在重试…'
      : (last ? (last.role === 'user' ? '我：' : '') + last.content : '还没有聊过');
    const badge = c.unread
      ? `<span class="badge">${c.unread > 99 ? '99+' : c.unread}</span>`
      : '<span style="height:18px"></span>';
    return `<div class="cell" data-user="${escAttr(c.user)}" data-persona="${escAttr(c.persona)}">
      <div class="ava ${avaClass(c.persona)}">${esc(c.persona.slice(0, 1))}</div>
      <div class="cell-body">
        <div class="cell-title">${esc(c.persona)}</div>
        <div class="cell-sub">${esc(preview)}</div>
      </div>
      <div class="cell-meta"><span class="cell-time">${fmtConvTime(last && last.created_at)}</span>${badge}</div>
    </div>`;
  }).join('');
  box.querySelectorAll('.cell').forEach((el) => {
    el.onclick = () => openChat(el.dataset.user, el.dataset.persona);
  });

  // 底部 Tab 的角标 = 所有角色未读回复的总数
  const total = S.convs.reduce((a, c) => a + (c.unread || 0), 0);
  const tb = $('tab-badge-msg');
  if (tb) {
    tb.textContent = total > 99 ? '99+' : String(total);
    tb.classList.toggle('hidden', total <= 0);
  }
}

/* ---------- 通讯录（Tab 2） ---------- */

/**
 * 通讯录里显示的角色简介。
 *
 * 注意：**不显示最后一条消息** —— 那是「消息」页的事。
 * 通讯录要的就是一份干净的角色清单（微信的通讯录也是这个路子），
 * 这里显示角色自己的设定（身份 / 性格 / 关系）。
 */
function personaTag(p) {
  const sum = S.summaries[pkey(p.user, p.persona)];
  const b = (sum && sum.persona_basics) || {};
  if (b.identity) return String(b.identity);
  if (Array.isArray(b.personality) && b.personality.length) return b.personality.join('、');
  if (b.relationship) return String(b.relationship);
  if (typeof b.tag === 'string' && b.tag.trim()) return b.tag.trim();
  return 'AI 伴侣';
}

function renderContacts() {
  const box = $('contact-list');
  if (!box) return;
  const q = (S.contactQuery || '').trim().toLowerCase();
  const ps = allPersonas().filter((p) => !q || p.persona.toLowerCase().includes(q));
  let html = '';
  if (ps.length) {
    html += '<div class="group-label">角色</div>';
    html += ps.map((p) => `<div class="cell" data-user="${escAttr(p.user)}" data-persona="${escAttr(p.persona)}">
      <div class="ava ${avaClass(p.persona)}">${esc(p.persona.slice(0, 1))}</div>
      <div class="cell-body"><div class="cell-title">${esc(p.persona)}</div>
      <div class="cell-sub">${esc(personaTag(p))}</div></div>
    </div>`).join('');
  }
  const archived = (S.archived || []).filter((p) => !q || p.persona_id.toLowerCase().includes(q));
  if (archived.length) {
    html += '<div class="group-label">已删除的角色</div>';
    html += archived.map((p) => `<div class="cell" data-archived="${escAttr(p.persona_id)}">
      <div class="ava" style="background:var(--surface-2);color:var(--text-muted)">${esc(p.name.slice(0, 1))}</div>
      <div class="cell-body"><div class="cell-title">${esc(p.name)}</div><div class="cell-sub">${esc(p.identity || '已删除')}</div></div>
      <button class="btn-mini persona-restore" data-archived="${escAttr(p.persona_id)}">恢复</button>
    </div>`).join('');
  }
  html += '<div class="group-label">功能</div>';
  html += `<div class="cell" id="cell-new-persona"><div class="ava" style="background:var(--surface-2);color:var(--text-muted)">+</div>
    <div class="cell-body"><div class="cell-title">新建角色</div><div class="cell-sub">创建一个新的 AI 伴侣</div></div></div>`;
  html += `<div class="cell" id="cell-group-chat"><div class="ava" style="background:var(--surface-2);color:var(--text-muted)">群</div>
    <div class="cell-body"><div class="cell-title">群聊</div><div class="cell-sub">多个角色在同一个房间里聊天</div></div></div>`;
  box.innerHTML = html;
  box.querySelectorAll('.cell[data-persona]').forEach((el) => {
    el.onclick = () => openProfile(el.dataset.user, el.dataset.persona);
  });
  const nb = $('cell-new-persona');
  if (nb) nb.onclick = () => openCreatePersona();
  const gb = $('cell-group-chat');
  if (gb) gb.onclick = () => toast('群聊：还没做', 2500);
  box.querySelectorAll('.persona-restore').forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      restorePersona(S.user, el.dataset.archived);
    };
  });
}

/* ---------- 新建 / 删除角色 ---------- */

const PF = { fields: [], templates: [], activeTemplate: '' };

function openCreatePersona() {
  const dlg = $('dialog-persona');
  if (!dlg) return;
  dlg.classList.remove('hidden');
  PF.activeTemplate = '';
  api('/api/persona-templates').then((data) => {
    PF.fields = data.fields || [];
    PF.templates = data.templates || [];
    renderTemplateCards();
    renderPersonaFields();
  }).catch((e) => { toast('拿不到角色模板：' + e.message); });
}

function renderTemplateCards() {
  const box = $('tpl-list');
  if (!box) return;
  box.innerHTML = PF.templates.map((t) => `
    <button type="button" class="tpl-card" data-tpl="${escAttr(t.id)}">
      <div class="tpl-name">${esc(t.name)}</div>
      <div class="tpl-id">${esc(t.identity || t.id)}</div>
    </button>`).join('');
  box.querySelectorAll('.tpl-card').forEach((el) => {
    el.onclick = () => pickTemplate(el.dataset.tpl);
  });
}

/** 模板内容 → 表单值。列表字段给数组，文本字段给字符串。纯函数，不碰 DOM。 */
function templateValues(fields, template) {
  const values = {};
  fields.forEach((f) => {
    const v = template ? template[f.key] : null;
    values[f.key] = f.type === 'list'
      ? (Array.isArray(v) ? v.slice() : [])
      : (v == null ? '' : String(v));
  });
  return values;
}

/** 表单值 → persona_basics。空值不写进去。纯函数，不碰 DOM。 */
function buildDefinition(fields, values) {
  const out = {};
  fields.forEach((f) => {
    const raw = values[f.key];
    if (f.type === 'list') {
      const items = (Array.isArray(raw) ? raw : []).map((s) => String(s).trim()).filter(Boolean);
      if (items.length) out[f.key] = items;
      return;
    }
    const text = (raw == null ? '' : String(raw)).trim();
    if (text) out[f.key] = text;
  });
  return out;
}

/** 选模板：把模板内容填进表单；没填过的字段清空，避免残留上一个模板。 */
function pickTemplate(id) {
  const tpl = PF.templates.find((t) => t.id === id);
  if (!tpl) return;
  PF.activeTemplate = id;
  document.querySelectorAll('#tpl-list .tpl-card').forEach((el) => {
    el.classList.toggle('is-active', el.dataset.tpl === id);
  });
  const values = templateValues(PF.fields, tpl.fields || {});
  PF.fields.forEach((f) => {
    const el = $('pf-' + f.key);
    if (!el) return;
    if (f.type === 'list') setChips(el, values[f.key]);
    else { el.value = values[f.key]; autoGrow(el); }
  });
}

/* --- 列表字段：一条一个标签，输入回车成标签 --- */

/** 一行里粘了好几条：按、分隔拆开。纯函数。 */
function splitEntries(raw) {
  return (raw || '').split(/[、,，]/).map((s) => s.trim()).filter(Boolean);
}

function addChip(box, value) {
  const chip = document.createElement('span');
  chip.className = 'chip-item';
  chip.dataset.value = value;
  const text = document.createElement('span');
  text.textContent = value;
  const x = document.createElement('button');
  x.type = 'button';
  x.className = 'chip-x';
  x.textContent = '×';
  x.setAttribute('aria-label', '删除 ' + value);
  chip.appendChild(text);
  chip.appendChild(x);
  box.insertBefore(chip, box.querySelector('.chip-entry'));
}

function setChips(box, values) {
  box.querySelectorAll('.chip-item').forEach((c) => c.remove());
  (values || []).forEach((v) => addChip(box, v));
}

function chipValues(box) {
  return Array.from(box.querySelectorAll('.chip-item')).map((c) => c.dataset.value);
}

function initChipInput(box) {
  const entry = box.querySelector('.chip-entry');
  if (!entry) return;
  const commit = () => {
    const items = splitEntries(entry.value);
    if (!items.length) return;
    items.forEach((v) => addChip(box, v));
    entry.value = '';
  };
  entry.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    else if (e.key === 'Backspace' && !entry.value) {
      const chips = box.querySelectorAll('.chip-item');
      if (chips.length) chips[chips.length - 1].remove();
    }
  });
  entry.addEventListener('blur', commit);
  box.addEventListener('click', (e) => {
    if (e.target.classList.contains('chip-x')) {
      e.target.closest('.chip-item').remove();
    } else if (e.target === box) {
      entry.focus();
    }
  });
}

/** 长文本随内容长高（CSS 的 max-height 兜底，超了内部滚动）。 */
function autoGrow(el) {
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = el.scrollHeight + 'px';
}

function renderPersonaFields() {
  const box = $('pf-fields');
  if (!box) return;
  box.innerHTML = PF.fields.map((f) => {
    const label = `<label class="dlg-field-label" for="pf-${escAttr(f.key)}">${esc(f.label)}${f.required ? ' *' : ''}</label>`;
    if (f.type === 'list') {
      return label + `<div class="chip-input" id="pf-${escAttr(f.key)}">`
        + `<input class="chip-entry" placeholder="输入一条，回车添加" autocomplete="off"></div>`;
    }
    return label
      + `<textarea id="pf-${escAttr(f.key)}" class="dlg-textarea" rows="1" autocomplete="off"></textarea>`;
  }).join('');
  PF.fields.forEach((f) => {
    const el = $('pf-' + f.key);
    if (!el) return;
    if (f.type === 'list') initChipInput(el);
    else el.addEventListener('input', () => autoGrow(el));
  });
}

/** 表单 → persona_basics。 */
function collectPersonaDefinition() {
  const values = {};
  PF.fields.forEach((f) => {
    const el = $('pf-' + f.key);
    if (!el) return;
    values[f.key] = f.type === 'list' ? chipValues(el) : (el.value || '');
  });
  return buildDefinition(PF.fields, values);
}

async function savePersona() {
  const definition = collectPersonaDefinition();
  if (!definition.name) { toast('给角色起个名字'); return; }
  if (!definition.identity) { toast('写一句这个角色是谁'); return; }
  // 角色标识就是角色名——不再单开一个字段
  const personaId = String(definition.name).trim();
  if (personaId.includes(',')) { toast('名字里不能有逗号'); return; }
  const user = S.user;
  try {
    await api(`/api/persona/${enc(user)}`, {
      method: 'POST',
      body: JSON.stringify({ persona_id: personaId, definition: JSON.stringify(definition) }),
    });
    $('dialog-persona').classList.add('hidden');
    toast('角色已创建');
    await loadUsers();
    await loadConvs();
    renderContacts();
    await openChat(user, personaId);
  } catch (e) {
    toast('创建失败：' + e.message);
  }
}

/** 删除角色 = 停用：撤销访问，记忆和聊天记录都留着，可以恢复。 */
async function deletePersona(user, persona) {
  if (!confirm(`删除角色「${persona}」？\n\n记忆和聊天记录都会保留，之后可以在「已删除的角色」里恢复。`)) return;
  try {
    await api(`/api/persona/${enc(user)}/${enc(persona)}`, { method: 'DELETE' });
    toast('角色已删除');
    await loadUsers();
    // 删掉的可能就是当前聊天角色——换到还剩下的一个，别留着失效的上下文
    if (S.user === user && S.persona === persona) {
      const left = allPersonas();
      if (left.length) await switchTarget(left[0].user, left[0].persona);
    }
    await loadConvs();
    renderContacts();
    // 回原处（通讯录点开的就回通讯录）
    showView(S.profileFrom || 'messages');
  } catch (e) {
    toast('删除失败：' + e.message);
  }
}

async function restorePersona(user, persona) {
  try {
    await api(`/api/persona/${enc(user)}/${enc(persona)}/restore`, { method: 'POST' });
    toast('已恢复');
    await loadUsers();
    await loadConvs();
    renderContacts();
  } catch (e) {
    toast('恢复失败：' + e.message);
  }
}

/* ---------- 我（Tab 3） ---------- */

function renderMe() {
  if (!$('me-name')) return;
  let notes = 0, msgs = 0, sessions = 0;
  S.convs.forEach((c) => {
    if (!c.summary) return;
    notes += c.summary.note_count || 0;
    msgs += c.summary.message_count || 0;
    sessions += c.summary.session_count || 0;
  });
  $('me-avatar').textContent = (S.user || '·').slice(0, 1);
  $('me-name').textContent = S.user || '我';
  $('me-sub').textContent = `${S.convs.length} 个角色 · 共 ${notes} 条记忆 · ${msgs} 条消息`;
  $('me-server').textContent = location.host;
  $('me-interval').textContent = S.automaticMemory ? '已开启' : '已关闭';
  $('me-version').textContent = 'v0.3.0';
  $('me-hint').textContent = `共 ${sessions} 次记录。切换角色请到「通讯录」。`;
}

/* ---------- 切换角色 / 打开会话 ---------- */

async function switchTarget(user, persona) {
  const changed = (user !== S.user) || (persona !== S.persona);
  S.user = user; S.persona = persona;
  localStorage.setItem('ce_user', user);
  localStorage.setItem('ce_persona', persona);
  S.personaTitle = persona;
  $('persona-name').textContent = persona;
  $('btn-avatar').textContent = persona.slice(0, 1);
  if (changed) {
    stopReplyRecovery();
    S.latestClientMessageId = null; S.generationId = null; S.replyState = null;
    S.messages = [];
    if (S.streamEl) { S.streamEl.remove(); S.streamEl = null; }
    connect();               // WS 的 URL 里带 user/persona，换角色必须重连
    await loadPersona();
    await loadHistory(true);
  }
}

async function openChat(user, persona) {
  await switchTarget(user, persona);
  if (S.user !== user || S.persona !== persona) return;
  showView('chat');
}

/* ---------- 日历（按日期回溯） ---------- */

/** 一次加载几个月的日历 */
const CAL_SPAN = 3;

async function openCalendar(user, persona) {
  S.calUser = user; S.calPersona = persona;
  const now = new Date();
  const cur = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  S.calDates = {};
  S.calMonths = [];
  // 先给前面几个月，这样一打开就能往上滑
  for (let i = CAL_SPAN; i >= 1; i--) S.calMonths.push(monthShift(cur, -i));
  S.calMonths.push(cur);
  S.calCurMonth = cur;
  S.calLoading = false;
  $('calendar-panel').classList.remove('hidden');
  const scroll = $('cal-scroll');
  scroll.innerHTML = '';
  try {
    const dates = await api(`/api/persona/${enc(user)}/${enc(persona)}/dates`);
    dates.forEach((d) => { S.calDates[d.date] = d; });
  } catch (e) { /* ignore */ }
  renderCalendar();
  // 打开时停在本月（而不是最底下，本月是最后一块，停最底会把它切一半）
  const curBlock = scroll.querySelector(`.cal-month[data-month="${S.calCurMonth}"]`);
  if (curBlock) scroll.scrollTop = curBlock.offsetTop;
  updateCalTitle();
}

function closeCalendar() {
  $('calendar-panel').classList.add('hidden');
}

/** 月份偏移：'2026-10' 加 -1 -> '2026-09' */
function monthShift(key, delta) {
  const [y, m] = key.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

/** 渲染所有已加载的月份（越往上越早，可以一直往上滑） */
function renderCalendar() {
  const scroll = $('cal-scroll');
  scroll.innerHTML = S.calMonths.map(monthHtml).join('');
  scroll.querySelectorAll('.cal-day.has').forEach((el) => {
    el.onclick = async () => {
      const info = S.calDates[el.dataset.date];
      const date = el.dataset.date;
      closeCalendar();
      await switchTarget(S.calUser, S.calPersona);
      await jumpToDate(date, info && info.first_id);
    };
  });
}

/** 某个月的格子 */
function monthHtml(key) {
  const [y, m] = key.split('-').map(Number);
  const days = new Date(y, m, 0).getDate();
  const lead = new Date(y, m - 1, 1).getDay();
  const now = new Date();
  const todayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  let html = `<div class="cal-month" data-month="${key}">`
    + `<div class="cal-month-title">${y}年${m}月</div>`
    + '<div class="cal-week"><span>日</span><span>一</span><span>二</span><span>三</span>'
    + '<span>四</span><span>五</span><span>六</span></div><div class="cal-days">';
  for (let i = 0; i < lead; i++) html += '<div class="cal-day empty"></div>';
  for (let d = 1; d <= days; d++) {
    const dk = `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    const has = !!S.calDates[dk];
    const cls = ['cal-day', has ? 'has' : 'empty'];
    const isToday = dk === todayKey;
    if (isToday) cls.push('today');
    // 今天的数字包一层 span（圆形用 span 画，才不会被网格拉变形）
    const inner = isToday ? `<span>${d}</span>` : String(d);
    html += `<div class="${cls.join(' ')}"${has ? ` data-date="${dk}"` : ''}>${inner}</div>`;
  }
  return html + '</div></div>';
}

/** 滑到顶就加载更早的月份 */
function loadOlderMonths() {
  if (S.calLoading || !S.calMonths.length) return;
  S.calLoading = true;
  const scroll = $('cal-scroll');
  const prevH = scroll.scrollHeight;
  const prevTop = scroll.scrollTop;
  const first = S.calMonths[0];
  const added = [];
  for (let i = CAL_SPAN; i >= 1; i--) added.push(monthShift(first, -i));
  S.calMonths = added.concat(S.calMonths);
  renderCalendar();
  // 顶部插了内容，滚动位置要补偿，不然会跳
  scroll.scrollTop = prevTop + (scroll.scrollHeight - prevH);
  S.calLoading = false;
}

/** 标题跟着当前看到的月份走 */
function updateCalTitle() {
  const scroll = $('cal-scroll');
  const box = scroll.getBoundingClientRect();
  const mid = box.top + box.height / 2;   // 用视口中间判断，比"最上面那个"更符合直觉
  let cur = null;
  scroll.querySelectorAll('.cal-month').forEach((m) => {
    if (cur === null && m.getBoundingClientRect().bottom > mid) cur = m.dataset.month;
  });
  if (!cur) cur = S.calMonths[0] || '';
  const [y, m] = cur.split('-').map(Number);
  $('cal-title').textContent = `${y}年${m}月`;
  $('btn-cal-today').classList.toggle('hidden', cur === S.calCurMonth);
  const inMonth = Object.keys(S.calDates).filter((k) => k.startsWith(cur));
  const msgs = inMonth.reduce((a, k) => a + (S.calDates[k].count || 0), 0);
  $('cal-foot').textContent = inMonth.length
    ? `${m}月有 ${inMonth.length} 天聊过天，共 ${msgs} 条消息`
    : `${m}月还没有聊天记录`;
}

/**
 * 定位到某条消息：加载「它之前的一页」+「它之后的全部」。
 *
 * 这样看到的是**普通聊天界面**（能一直往上/往下翻全部历史），
 * 而不是把视图锁在那一段里。
 */
async function loadAround(firstId) {
  // 注意：before_id 是**排他**的（id < X），所以要传 firstId + 1
  // 才能把目标消息本身也包含进来（否则定位不到它）
  const older = await api(`${base()}/history?before_id=${firstId + 1}&limit=${PAGE}`);
  let newer = [];
  let after = firstId;
  for (;;) {
    const part = await api(`${base()}/history?after_id=${after}&limit=500`);
    if (!part.length) break;
    newer = newer.concat(part);
    if (part.length < 500) break;
    after = part[part.length - 1].id;
  }
  return { older, newer };
}

/* ---------- 角色资料 ---------- */

async function openProfile(user, persona) {
  S.profilePersona = { user, persona };
  // 记住来路：返回时回原处。通讯录点开的就回通讯录，聊天页点开的就回聊天页——
  // 打开资料页不改变当前聊天角色（那是 openChat 的事），所以固定回聊天页会串台。
  S.profileFrom = S.view;
  showView('profile');
  const body = $('profile-body');
  body.innerHTML = '<div class="empty">载入中…</div>';
  let info = null;
  try {
    info = await api(`/api/persona/${enc(user)}/${enc(persona)}`);
    S.summaries[pkey(user, persona)] = info;
  } catch (e) { /* ignore */ }
  renderProfile(user, persona, info);
}

function renderProfile(user, persona, info) {
  const body = $('profile-body');
  if (!body) return;
  const basics = (info && info.persona_basics) || {};
  const name = basics.name || persona;
  const tag = basics.tag || basics.description || '';
  body.innerHTML = `
    <div class="profile-hero">
      <div class="ava ava-xl ${avaClass(persona)}">${esc(name.slice(0, 1))}</div>
      <div class="profile-name">${esc(name)}</div>
      ${tag ? `<div class="profile-sub">${esc(tag)}</div>` : ''}
    </div>
    <div class="stats">
      <div><div class="stat-num">${(info && info.message_count) || 0}</div><div class="stat-label">条消息</div></div>
      <div><div class="stat-num">${(info && info.note_count) || 0}</div><div class="stat-label">条记忆</div></div>
      <div><div class="stat-num">${(info && info.session_count) || 0}</div><div class="stat-label">次记录</div></div>
    </div>
    <div class="group">
      <div class="row" id="row-chat"><span>继续聊天</span><span class="row-val">现在就开始 <span class="chev">›</span></span></div>
      <div class="row" id="row-memory"><span>TA 的记忆</span><span class="row-val">${(info && info.note_count) || 0} 条 <span class="chev">›</span></span></div>
      <div class="row" id="row-dates"><span>聊天记录</span><span class="row-val">按日期回溯 <span class="chev">›</span></span></div>
      <div class="row"><span>主动联系</span><button type="button" class="notification-action" id="btn-proactive" disabled>载入中…</button></div>
      ${S.debug ? '<div class="row" id="row-relation"><span>后台记忆</span><span class="row-val">任务状态 <span class="chev">›</span></span></div>' : ''}
    </div>
    <div class="dlg-danger">
      <button class="dlg-danger-btn" id="btn-persona-delete">删除角色</button>
    </div>
  `;
  const q = (id) => body.querySelector('#' + id);
  q('row-chat').onclick = () => openChat(user, persona);
  q('row-memory').onclick = () => openMemory(user, persona);
  q('row-dates').onclick = () => openCalendar(user, persona);
  const maintenance = q('row-relation');
  if (S.debug && maintenance) maintenance.onclick = () => openRelation(user, persona);
  q('btn-persona-delete').onclick = () => deletePersona(user, persona);
  const initiative=q('btn-proactive');
  if (initiative) {
    const path=`/api/persona/${enc(user)}/${enc(persona)}/proactive`;
    api(path).then(state=>{
      let enabled=state.enabled;
      initiative.textContent=enabled?'关闭':'开启';initiative.disabled=false;
      initiative.onclick=async()=>{
        initiative.disabled=true;
        try {
          const next=await api(path,{method:'PUT',body:JSON.stringify({enabled:!enabled})});
          enabled=next.enabled;initiative.textContent=enabled?'关闭':'开启';
          toast(enabled?'已允许 TA 在白天主动联系，每天最多一次':'已关闭主动联系');
        } catch(error) {toast(error.message||'设置失败');}
        finally {initiative.disabled=false;}
      };
    }).catch(()=>{initiative.textContent='暂不可用';});
  }
}

/* ---------- 后台记忆任务（仅调试入口） ---------- */
async function openRelation(user, persona) {
  if (!S.debug) return;
  S.relationPersona = { user, persona };
  showView('relation');
  const body = $('relation-body');
  if (!body) return;
  body.innerHTML = '<div class="empty">载入中…</div>';
  let info = null;
  try {
    info = await api(`/api/persona/${enc(user)}/${enc(persona)}`);
    S.summaries[pkey(user, persona)] = info;
  } catch (e) { /* ignore */ }
  renderRelationPage(user, persona, info);
}

/** 只显示后台任务状态，不展示画像、关系摘要或事实引文。 */
function renderRelationPage(user, persona, info) {
  if (!S.debug) return;
  const body = $('relation-body');
  if (!body) return;
  const basics = (info && info.persona_basics) || {};
  const name = basics.name || persona;
  const key = pkey(user, persona);
  // 「记录中」两个来源：本地刚点的 / 服务器说正在记
  const recording = !!S.recording[key] || !!(info && info.recording);
  const lastRec = info && info.last_record_at;
  const meta = (info && info.summary_meta) || {};
  const summaryBusy = ['queued','running'].includes(meta.job_status);
  body.innerHTML = `
    <div class="profile-hero">
      <div class="ava ava-xl ${avaClass(persona)}">${esc(name.slice(0, 1))}</div>
      <div class="profile-name">${esc(name)}</div>
      <div class="profile-sub">后台记忆任务</div>
    </div>
    <div class="hint">${meta.current ? '摘要已与事实操作同步。' : meta.document ? '摘要更新期间，对话继续沿用上次成功摘要。' : '尚未生成独立摘要，后台将从事实建立。'}${meta.pending_changes ? ` 尚有 ${meta.pending_changes} 条事实操作待处理。` : ''}</div>
    <div class="group">
      <div class="row"><span>摘要更新</span><span class="row-val"><button class="btn-mini" id="btn-summary-refresh" ${summaryBusy ? 'disabled' : ''}>${summaryBusy ? '更新中…' : meta.job_status === 'failed' ? '重试更新' : meta.legacy ? '从事实生成' : '更新摘要'}</button></span></div>
      ${meta.generated_at ? `<div class="row"><span>摘要更新时间</span><span class="row-val">${esc(fmtTime(meta.generated_at))}</span></div>` : ''}
    </div>
    ${meta.last_error ? `<div class="hint">摘要暂未完成：${esc(taskFailureText(meta.last_error))}${meta.retry_scheduled ? '（已安排重试）' : '（当前快速重试已暂停，可查看后台执行记录）'}</div>` : ''}
    <div class="group">
      <div class="row"><span>上次记录</span>
        <span class="row-val col">
          <span id="last-record-val">${esc(fmtAgo(lastRec))}</span>
          ${lastRec ? `<span class="dim-sm">${esc(fmtTime(lastRec))}</span>` : ''}
        </span></div>
      <div class="row"><span>手动记一次</span>
        <span class="row-val"><span class="btn-mini${recording ? ' is-busy' : ''}" id="btn-record-now">${recording ? '记录中…' : '记一次'}</span></span></div>
    </div>
    ${info && info.memory_meta && info.memory_meta.last_error ? `<div class="hint">记一次未完成：${esc(taskFailureText(info.memory_meta.last_error))}${info.memory_meta.retry_at ? '（已安排重试）' : '（当前快速重试已暂停）'}</div>` : ''}
    <div class="hint">${S.automaticMemory ? '对话积累一定内容或满足空闲条件后，会自动记下值得保留的事。' : '自动记录已关闭。'}<br/>想现在记也可以手动触发，记录完这里会自动刷新。</div>
    <button class="btn-mini" id="btn-task-attempts">查看后台执行记录</button><pre id="task-attempts"></pre>
  `;
  const btn = body.querySelector('#btn-record-now');
  if (btn && !recording) btn.onclick = (e) => recordNow(user, persona, e.target);
  const summaryBtn = body.querySelector('#btn-summary-refresh');
  if (summaryBtn && !summaryBusy) summaryBtn.onclick = () => refreshFactSummaries(user,persona);
  const attemptsBtn = body.querySelector('#btn-task-attempts');
  if (S.debug && attemptsBtn) attemptsBtn.onclick = () => loadTaskAttempts(user,persona);
  if (summaryBusy || (meta.stale && (!meta.job_status || meta.retry_scheduled))) watchFactSummaries(user,persona);
}

async function loadTaskAttempts(user,persona) {
  try {
    const records=await api(`/api/persona/${enc(user)}/${enc(persona)}/task-attempts`);
    const target=S.relationPersona;
    if (!S.debug || S.view!=='relation' || !target || target.user!==user || target.persona!==persona) return;
    const box=$('task-attempts');
    if (box) box.textContent=JSON.stringify(records,null,2);
  } catch (e) { toast('读取执行记录失败：'+e.message); }
}

const summaryWatchers = new Set();
async function refreshFactSummaries(user,persona) {
  const key = pkey(user,persona);
  try {
    const result = await api(`/api/persona/${enc(user)}/${enc(persona)}/summaries/refresh`, {method:'POST'});
    toast(result.status === 'unchanged' ? '事实没有变化，摘要已是最新' : '开始更新摘要，你可以继续聊天',2500);
    const info = await api(`/api/persona/${enc(user)}/${enc(persona)}`);
    S.summaries[key] = info; renderRelationIfOpen(user,persona); renderProfileIfOpen(user,persona);
  } catch (e) { toast('摘要更新失败：' + e.message,4000); }
}

async function watchFactSummaries(user,persona) {
  const key = pkey(user,persona);
  if (summaryWatchers.has(key)) return;
  summaryWatchers.add(key);
  try {
    for (let i=0;i<160;i++) {
      await new Promise(resolve => setTimeout(resolve,3000));
      const target = S.relationPersona;
      if (S.view !== 'relation' || !target || target.user !== user || target.persona !== persona || document.visibilityState !== 'visible') return;
      let info;
      try { info = await api(`/api/persona/${enc(user)}/${enc(persona)}`); } catch (_) { continue; }
      if (S.view !== 'relation' || S.relationPersona !== target) return;
      S.summaries[key] = info; renderRelationIfOpen(user,persona); renderProfileIfOpen(user,persona);
      const meta = info.summary_meta || {};
      if (meta.current || (meta.job_status === 'failed' && !meta.retry_scheduled)) return;
    }
  } finally { summaryWatchers.delete(key); }
}

/** 关系状态页当前是开着这个角色的话，就地重画（不跳页、不打断） */
function renderRelationIfOpen(user, persona) {
  const rp = S.relationPersona;
  if (!rp || rp.user !== user || rp.persona !== persona) return;
  if (S.view !== 'relation') return;
  renderRelationPage(user, persona, S.summaries[pkey(user, persona)]);
}

/** 资料页开着这个角色的话，也顺手重画（关系阶段可能变了） */
function renderProfileIfOpen(user, persona) {
  const pp = S.profilePersona;
  if (!pp || pp.user !== user || pp.persona !== persona) return;
  if (S.view !== 'profile') return;
  renderProfile(user, persona, S.summaries[pkey(user, persona)]);
}

/**
 * 手动触发一次记录。
 *
 * **不等**：抽取是一次完整 LLM 调用（几十秒），
 * 所以后端丢到后台跑，这里立刻返回，用户可以继续聊天。
 *
 * 记完之后怎么知道？**服务端会主动推 record_done**（WS）——
 * 所以关系状态页会自己刷新，不用退出重进。
 * 万一 WS 断了，还有 pollRecordDone 兜底。
 */
async function recordNow(user, persona, btn) {
  const key = pkey(user, persona);
  const before = (S.summaries[key] || {}).last_record_at || null;
  if (btn) { btn.textContent = '记录中…'; btn.classList.add('is-busy'); btn.style.pointerEvents = 'none'; }
  S.recording[key] = true;
  S.recordPending = { user, persona, before, key };
  try {
    const res = await api('/api/session/close', {
      method: 'POST',
      body: JSON.stringify({ user_id: user, persona_id: persona }),
    });
    if (res && res.started === false) {
      toast('已经在记录中了', 2500);
      S.recording[key] = false;
      S.recordPending = null;
      restoreRecordBtn(btn);
      return;
    }
    toast('开始记录，你可以继续聊', 2200);
    pollRecordDone(user, persona, before);   // 兜底（WS 正常时它会自己退出）
  } catch (e) {
    toast('记录失败：' + (e.message || ''), 4000);
    S.recording[key] = false;
    S.recordPending = null;
    restoreRecordBtn(btn);
  }
}

function restoreRecordBtn(btn) {
  if (!btn) return;
  btn.textContent = '记一次';
  btn.classList.remove('is-busy');
  btn.style.pointerEvents = '';
}

/**
 * 兜底轮询：WS 没推 record_done（比如断线了）时，靠它发现记录完成。
 *
 * 成功时间变化和 worker 结束之外，还必须确认没有失败/未完成的批次。
 * 判定完成的两个条件：
 *   1. last_record_at 变了 —— 至少一个持久任务已成功提交
 *   2. recording 变回 false —— 抽取真的跑完了
 * 只看 1 会**提前误判**成「记好了」（那时候记忆还没抽出来）。
 */
async function pollRecordDone(user, persona, before, tries = 40) {
  const key = pkey(user, persona);
  for (let i = 0; i < tries; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    // 已经被 WS 推送处理过了 → 不用管
    if (!S.recordPending || S.recordPending.key !== key) return;
    let info = null;
    let jobs = [];
    try {
      const scope = `/api/persona/${enc(user)}/${enc(persona)}`;
      [info, jobs] = await Promise.all([api(scope), api(`${scope}/memory-jobs`)]);
    } catch (e) { continue; }
    if (!S.recordPending || S.recordPending.key !== key) return;
    const failed = jobs.find(j => j.status === 'failed');
    if (failed && !info.recording) {
      finishRecord(user, persona, info, false);
      toast('记忆整理未完成，可点「记一次」重试：' + (failed.last_error || ''), 5000);
      return;
    }
    if (jobs.some(j => j.status === 'queued' || j.status === 'running')) continue;
    if (info && info.last_record_at && info.last_record_at !== before && !info.recording) {
      finishRecord(user, persona, info, true);
      return;
    }
  }
  // 超时：记录可能还在跑，把按钮恢复
  S.recording[key] = false;
  if (S.recordPending && S.recordPending.key === key) S.recordPending = null;
  renderRelationIfOpen(user, persona);
  restoreRecordBtn($('btn-record-now'));
  toast('记录还没完成，过一会儿再看', 3000);
}

/** 记录完成后的收尾：更新缓存 + 重画关系状态页 + 刷新会话列表 */
function finishRecord(user, persona, info, toastIt) {
  const key = pkey(user, persona);
  S.recording[key] = false;
  if (S.recordPending && S.recordPending.key === key) S.recordPending = null;
  if (info) S.summaries[key] = info;
  renderRelationIfOpen(user, persona);
  renderProfileIfOpen(user, persona);
  restoreRecordBtn($('btn-record-now'));
  if (toastIt) toast('记好了', 2000);
  loadConvs();
}

/* ---------- TA 的记忆 ---------- */

async function openMemory(user, persona) {
  const target = { user, persona };
  S.memoryPersona = target;
  S.memoryAction = null;
  S.memoryNotes = [];
  showView('memory');
  $('memory-title').textContent = persona + '的记忆';
  const box = $('memory-list');
  box.innerHTML = '<div class="empty">载入中…</div>';
  try {
    const notes = await api(`/api/persona/${enc(user)}/${enc(persona)}/notes?limit=200`);
    if (S.memoryPersona !== target) return;
    S.memoryNotes = notes;
  } catch (e) {
    if (S.memoryPersona !== target) return;
    S.memoryNotes = [];
  }
  renderMemory();
}

function renderMemory() {
  const notes = S.memoryNotes || [];
  const chips = $('memory-filters');
  const box = $('memory-list');
  if (!chips || !box) return;
  const counts = {};
  notes.forEach((n) => { counts[n.kind] = (counts[n.kind] || 0) + 1; });
  const types = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  chips.innerHTML = '<button class="chip' + (S.memoryFilter === 'all' ? ' active' : '') + '" data-t="all">全部</button>'
    + types.map((t) => `<button class="chip${S.memoryFilter === t ? ' active' : ''}" data-t="${escAttr(t)}">${t === 'episode' ? '经历' : '事实'} ${counts[t]}</button>`).join('');
  chips.querySelectorAll('.chip').forEach((c) => {
    c.onclick = () => { S.memoryFilter = c.dataset.t; renderMemory(); };
  });
  const filtered = S.memoryFilter === 'all' ? notes : notes.filter((n) => n.kind === S.memoryFilter);
  if (!filtered.length) { box.innerHTML = '<div class="empty">还没有这类记忆</div>'; return; }
  const action = S.memoryAction;
  box.innerHTML = filtered.map((n) => {
    const active = action && action.id === n.id;
    let controls = `<div class="note-actions"><button class="note-edit" data-id="${escAttr(n.id)}" ${action ? 'disabled' : ''}>编辑</button><button class="note-delete" data-id="${escAttr(n.id)}" ${action ? 'disabled' : ''}>删除</button></div>`;
    if (active) {
      const disabled = action.busy ? 'disabled' : '';
      controls = action.kind === 'edit'
        ? `<textarea class="note-editor" data-note-editor aria-label="编辑记忆内容" ${disabled}>${esc(action.draft)}</textarea><div class="note-actions"><button class="note-cancel" ${disabled}>取消</button><button class="note-save" ${disabled}>${action.busy ? '保存中…' : '保存'}</button></div>`
        : `<div class="note-confirm">删除这条记忆？聊天记录会保留。</div><div class="note-actions"><button class="note-cancel" ${disabled}>取消</button><button class="note-save danger" ${disabled}>${action.busy ? '删除中…' : '确认删除'}</button></div>`;
      if (action.error) controls += `<div class="note-error" role="alert">${esc(action.error)}</div>`;
    }
    return `
    <div class="note" data-kind="${escAttr(n.kind)}">
      <div class="note-head"><span class="note-type">${n.kind === 'episode' ? '经历' : '事实'}</span>
      <span class="note-imp">重要度 ${(n.importance || 0).toFixed(2)}</span></div>
      ${active && action.kind === 'edit' ? '' : `<div class="note-content">${esc(n.content)}</div>`}
      ${n.reality === 'shared_fiction' ? '<div class="dim-sm">共同想象</div>' : ''}
      <div class="note-bar"><i style="width:${Math.round((n.importance || 0) * 100)}%"></i></div>
      ${controls}
    </div>`;
  }).join('');
  for (const kind of ['edit', 'delete']) box.querySelectorAll('.note-' + kind).forEach(button => {
    button.onclick = () => startMemoryAction(button.dataset.id, kind);
  });
  box.querySelectorAll('.note-cancel').forEach(button => button.onclick = () => { S.memoryAction = null; renderMemory(); });
  box.querySelectorAll('.note-save').forEach(button => button.onclick = saveMemoryAction);
  const editor = box.querySelector('[data-note-editor]');
  if (editor && action) editor.oninput = () => {
    action.draft = editor.value;
    const save = box.querySelector('.note-save');
    if (save) save.disabled = action.busy || !action.draft.trim();
  };
}

function startMemoryAction(id, kind) {
  if (S.memoryAction) return;
  const note = S.memoryNotes.find(note => note.id === id);
  if (!note || !S.memoryPersona) return;
  S.memoryAction = {id, kind, scope:S.memoryPersona, revision:note.revision, draft:note.content, busy:false, error:''};
  renderMemory();
}

async function saveMemoryAction() {
  const action = S.memoryAction;
  if (!action || action.busy) return;
  if (action.kind === 'edit' && !action.draft.trim()) {
    action.error = '记忆内容不能为空'; renderMemory(); return;
  }
  const {user,persona} = action.scope;
  action.busy = true; action.error = ''; renderMemory();
  try {
    const path = `/api/persona/${enc(user)}/${enc(persona)}/notes/${enc(action.id)}`;
    const result = action.kind === 'edit'
      ? await api(path, {method:'PATCH',body:JSON.stringify({content:action.draft.trim(),revision:action.revision})})
      : await api(`${path}?revision=${action.revision}`, {method:'DELETE'});
    if (S.memoryPersona !== action.scope || S.memoryAction !== action) return;
    S.memoryNotes = action.kind === 'edit'
      ? S.memoryNotes.map(note => note.id === action.id ? result : note)
      : S.memoryNotes.filter(note => note.id !== action.id);
    S.memoryAction = null; renderMemory();
    toast(action.kind === 'edit' ? '已保存修改' : '已删除记忆');
    try {
      S.summaries[pkey(user,persona)] = await api(`/api/persona/${enc(user)}/${enc(persona)}`);
      renderProfileIfOpen(user,persona);
    } catch (_) { /* 稍后重新打开资料时同步计数 */ }
  } catch (error) {
    if (S.memoryPersona !== action.scope || S.memoryAction !== action) return;
    action.busy = false; action.error = error.message; renderMemory();
  }
}

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
  const standalone = navigator.standalone === true || !!window.matchMedia?.('(display-mode: standalone)').matches;
  const dynamicHeight = standalone && !!window.CSS?.supports('height', '100dvh');

  if (!vv) {
    // 不支持 visualViewport：退回到布局视口
    root.style.setProperty('--app-h', dynamicHeight ? '100dvh' : window.innerHeight + 'px');
    root.style.setProperty('--vv-top', '0px');
    updateDebug();
    return;
  }

  // 可见视口的底边（client 坐标）
  const vvBottom = Math.round(vv.offsetTop + vv.height);
  // 键盘占了多少
  const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
  const active = document.activeElement;
  const editing = active && (['INPUT','TEXTAREA'].includes(active.tagName) || active.isContentEditable);
  const kbOpen = kb > 120 && !!editing;

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

  root.style.setProperty('--app-h', dynamicHeight && !kbOpen ? '100dvh' : h + 'px');
  root.style.setProperty('--vv-top', dynamicHeight && !kbOpen ? '0px' : top + 'px');
  S.appH = h;
  S.kbOpen = kbOpen;

  // 键盘弹起导致可见区域变小 → 滚到底部（新消息不能被遮住）
  // 高度变了（键盘弹出/收起、app 高度重算）：若原本贴着底部就重新贴底；
  // 键盘弹起导致高度变小时强制贴底，避免最后一条被键盘挡住
  if (prev && prev !== h) {
    const box = $('chat');
    const atBottom = !box || (box.scrollHeight - box.scrollTop - box.clientHeight < 80);
    if (atBottom || h < prev) anchorBottom();
  }

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

async function verifyAccess(pin) {
  const result = await api('/api/auth/check', {method: 'POST', headers: {'X-Pin': pin}});
  if (result.ok !== true) throw new Error('访问校验未完成');
}

function invalidateAccess() {
  ++bootRequest;
  const hadPin = Boolean(S.pin);
  S.pin = '';
  localStorage.removeItem('ce_pin');
  clearTimeout(S.reconnectTimer);
  stopReplyRecovery();
  const ws = S.ws;
  S.ws = null;
  S.connected = false;
  S.user = null;
  S.persona = null;
  if (ws) { try { ws.close(); } catch (_) { /* 已断开 */ } }
  resetSending();
  $('dialog-persona').classList.add('hidden');
  $('gate-error').textContent = hadPin ? 'PIN 已失效，请重新输入' : '请输入访问 PIN';
  showGate();
}

async function boot() {
  const request = ++bootRequest;
  const pin = S.pin;
  try {
    await verifyAccess(pin);
    if (request !== bootRequest || S.pin !== pin) return;
    const health = await api('/api/health');
    if (request !== bootRequest || S.pin !== pin) return;
    S.automaticMemory=health.auto_memory !== false;
  } catch (e) {
    if (request !== bootRequest || S.pin !== pin) return;
    if (e.status === 401) {
      invalidateAccess();
    } else {
      showGate(true);
    }
    return;
  }
  $('gate').classList.add('hidden');
  $('app').classList.remove('hidden');
  await resolveTarget();
  if (S.debug) {
    $('call-log-link').classList.remove('hidden');
    $('call-log-link').href = `/calls.html?user=${enc(S.user)}&persona=${enc(S.persona)}`;
    $('call-log-link').onclick = () => {
      $('call-log-link').href = `/calls.html?user=${enc(S.user)}&persona=${enc(S.persona)}`;
    };
  }
  // 先读设置（show_thinking）再拉历史 —— 并行的话不知道该不该带 think
  await loadSettings();
  await loadUsers();
  await Promise.all([loadPersona(), loadHistory(true)]);
  const initialView = new URLSearchParams(location.search).get('open') === 'chat'
    && S.users.some(u => u.id === S.user && (u.personas || []).includes(S.persona)) ? 'chat' : 'messages';
  showView(initialView);
  window.CompanionNotifications?.init({api, toast, debug: S.debug,
    target: () => ({user_id: S.user, persona_id: S.persona}),
    active: () => S.view === 'me' && !$('app').classList.contains('hidden')});
  connect();
  loadConvs();          // 聊天列表比较慢，放后台拉
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
  $('btn-avatar').textContent = initial;
  $('gate-avatar').textContent = initial;
  $('persona-name').textContent = p || '未设置角色';
}

/* ---------- 数据加载 ---------- */

async function loadPersona() {
  try {
    S.info = await api(base());
    const basics = S.info.persona_basics || {};
    if (basics.name) {
      S.personaTitle = basics.name;
      $('persona-name').textContent = basics.name;
    }
    $('btn-avatar').textContent = (basics.name || S.persona || '·').slice(0, 1);
  } catch (e) { /* ignore */ }
}

async function loadSettings() {
  try {
    const s = await api(`/api/settings/${encodeURIComponent(S.user)}/${encodeURIComponent(S.persona)}`);
    S.showThinking = s.show_thinking !== false;
    $('toggle-thinking').checked = S.showThinking;
  } catch (e) { /* ignore */ }
}

async function loadHistory(reset, opts) {
  opts = opts || {};
  const scope = base(), clientId = S.latestClientMessageId;
  const request = ++S.historyRequest;
  const box = $('chat');
  const prevH = box.scrollHeight;
  const prevTop = box.scrollTop;
  try {
    // 显式告诉服务端要不要带 think，别依赖服务端自己的设置（两边可能不一致）
    const wantThinking = opts.withThinking !== undefined ? opts.withThinking : S.showThinking;
    const q = new URLSearchParams({ limit: String(PAGE) });
    q.set('include_thinking', wantThinking ? '1' : '0');
    const msgs = await api(`${scope}/history?${q}`);
    if (scope !== base() || clientId !== S.latestClientMessageId || request !== S.historyRequest) return;
    // A history snapshot may predate a websocket message received while fetch
    // was in flight. Preserve the confirmed live tail and unacknowledged input.
    const ids = new Set(msgs.map(m => m.id));
    const clients = new Set(msgs.map(m => m.client_message_id).filter(Boolean));
    const latest = Math.max(0, ...msgs.map(m => m.id));
    const tail = S.messages.filter(m => !ids.has(m.id) && !clients.has(m.client_message_id)
      && (m.pending || (Number.isSafeInteger(m.id) && !m.localOnly && m.id > latest)));
    S.messages = msgs.concat(tail);
    const pending = readOutbox();
    for (const id of clients) delete pending[id];
    writeOutbox(pending);
    S.hasMore = msgs.length >= PAGE;
    S.viewMode = 'live';
    S.viewLabel = '';
    S.newCount = 0;
    updateHistoryBar();
    renderChat();
    if (opts.keepScroll) {
      // 保持用户正在看的位置（重绘前后内容高度变了，按差值补偿）
      box.scrollTop = prevTop + (box.scrollHeight - prevH);
    } else {
      anchorBottom();
    }
    updateJumpButton();
  } catch (e) { /* ignore */ }
}

/** 往上翻：加载更早的一页 */
async function loadOlder() {
  if (S.loadingOlder || !S.hasMore) return;
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

/* ---------- 时间分隔条 ---------- */

/**
 * 什么时候插一条时间分隔条 —— **两个条件满足其一，谁先到算谁**：
 *
 * 1. 时间间隔 > 10 分钟
 *    不用微信那套 5 分钟：AI 陪聊时正常间隔就几秒，
 *    按分钟切会把一段连贯的聊天切得很碎。
 *
 * 2. 连续超过 10 条消息
 *    光看时间不够 —— 一段密集的长对话（比如一天聊了 150 条）
 *    中间可能一条锚点都没有，往上滚完全不知道自己在哪。
 *
 * 两个阈值是配套的：10 条消息大概就是 10 分钟的来回，
 * 这样"聊得密"和"停了一阵"会被切成同样大小的块。
 */
const TIME_SEP_GAP_MIN = 10;
const TIME_SEP_GAP_MS = TIME_SEP_GAP_MIN * 60 * 1000;
const TIME_SEP_MAX_MSGS = 10;

function needTimeSep(prevIso, curIso, sinceLastSep) {
  // 条件 2：这一段已经太长
  if (sinceLastSep >= TIME_SEP_MAX_MSGS) return true;
  if (!prevIso || !curIso) return false;
  const a = new Date(prevIso), b = new Date(curIso);
  if (isNaN(a) || isNaN(b)) return false;
  // 条件 1：隔了很久
  return Math.abs(b.getTime() - a.getTime()) > TIME_SEP_GAP_MS;
}

/**
 * 分隔条上的文字，按远近自适应：
 *   今天   -> 22:25
 *   昨天   -> 昨天 22:25
 *   更早   -> 10月28日 22:25
 */
function fmtTimeSep(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const now = new Date();
  const p = (n) => String(n).padStart(2, '0');
  const time = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const dayStart = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((dayStart(now) - dayStart(d)) / 86400000);
  if (days <= 0) return time;
  if (days === 1) return `昨天 ${time}`;
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日 ${time}`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${time}`;
}

/**
 * 往消息流里追加一条消息。
 * 实时消息和历史消息走同一套规则（否则刷新前后显示不一致）。
 */
function appendChatMsg(el, createdAt) {
  const box = $('chat');
  const prev = S.messages.length ? S.messages[S.messages.length - 1] : null;
  if (needTimeSep(prev && prev.created_at, createdAt, S.sinceLastSep || 0)) {
    const sep = document.createElement('div');
    sep.className = 'time-sep';
    sep.textContent = fmtTimeSep(createdAt);
    box.appendChild(sep);
    S.sinceLastSep = 0;
  } else {
    S.sinceLastSep = (S.sinceLastSep || 0) + 1;
  }
  box.appendChild(el);
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
    renderReplyStatus();
    return;
  }
  let prevAt = null;
  let since = 0;
  S.messages.forEach((m) => {
    if (needTimeSep(prevAt, m.created_at, since)) {
      const sep = document.createElement('div');
      sep.className = 'time-sep';
      sep.textContent = fmtTimeSep(m.created_at);
      box.appendChild(sep);
      since = 0;
    } else {
      since += 1;
    }
    prevAt = m.created_at;
    box.appendChild(renderMsg(m.role, m.content, m.thinking, m.id));
  });
  S.sinceLastSep = since;
  if (S.streamEl && S.viewMode === 'live') box.appendChild(S.streamEl);
  renderReplyStatus();
  markVisibleChatRead();
}

function taskFailureText(error) {
  const text = String(error || '未知错误');
  if (/Timeout|timed out/i.test(text)) return '模型响应超时';
  if (/Connection|ConnectError|NetworkError/i.test(text)) return '暂时无法连接模型服务';
  if (/HTTP 40[13]/.test(text)) return '模型服务拒绝访问，请检查服务端配置';
  if (/ValidationError|JSONDecodeError/.test(text)) return '模型输出未通过格式校验';
  return text;
}

function renderReplyStatus() {
  if (S.replyStatusEl) { S.replyStatusEl.remove(); S.replyStatusEl = null; }
  const state = S.replyState;
  if (!state || state.scope !== base() || S.viewMode !== 'live' || state.status !== 'failed') return;
  const el = document.createElement('div');
  el.className = 'reply-status';
  const label = document.createElement('span');
  label.textContent = '回复未完成：' + taskFailureText(state.last_error);
  el.appendChild(label);
  if (state.status === 'failed' && state.can_retry) {
    const button = document.createElement('button');
    button.className = 'btn-mini'; button.textContent = '重试回复'; button.disabled = !!state.retrying;
    button.onclick = () => retryReply(state);
    el.appendChild(button);
  }
  $('chat').appendChild(el); S.replyStatusEl = el;
}

async function retryReply(state) {
  if (state.scope !== base() || state.retrying) return;
  state.retrying = true; renderReplyStatus();
  try {
    const g = await api(`${state.scope}/generations/${enc(state.id)}/retry`, {method:'POST'});
    if (state.scope !== base()) return;
    S.generationId = g.id; S.replyState = {...g, scope:state.scope};
    await recoverReply(true);
  } catch (e) { toast('重试失败：' + taskFailureText(e.message),4000); }
  finally { state.retrying = false; renderReplyStatus(); }
}

function renderMsg(role, content, thinking, id) {
  const el = document.createElement('div');
  el.className = 'msg ' + (role === 'user' ? 'me' : 'them');
  if (id) el.dataset.id = id;
  const text = document.createElement('div');
  text.className = 'body';
  text.innerHTML = renderMarkdown(content);
  el.appendChild(text);

  if (thinking && S.showThinking) {
    el.appendChild(buildThinkBlock(thinking));
  }
  return el;
}

/* ---------- Markdown 渲染（零依赖 / 离线可用 / XSS 安全） ---------- */

/**
 * 为什么自己写而不用 marked.js：
 * - 零构建、零 CDN —— 局域网、离线都要能打开，不能依赖外网
 * - LLM 输出是不可信输入，必须自己保证不注入 HTML
 *
 * 支持：粗体、*斜体*、~~删除线~~、`行内代码`、无序/有序列表、换行
 * 不支持：标题、代码块、链接、图片（伴侣聊天用不上，少一个 XSS 面）
 */
function renderInlineMd(text) {
  // 行内代码先摘出来，里面不做任何替换
  const codes = [];
  let s = text.replace(/`([^`\n]+)`/g, (m, c) => {
    codes.push(c);
    return '\u0000C' + (codes.length - 1) + '\u0000';
  });
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  // 注意：中文不属于 \w，所以边界不能用 \w 判断（否则中文里的 *斜体* 匹配不上）
  s = s.replace(/(^|[^*\s])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  // 注意：codes[i] 在摘出来时就已经转义过了，这里不能再 esc 一次（否则 &lt; 变 &amp;lt;）
  s = s.replace(/\u0000C(\d+)\u0000/g, (m, i) => '<code>' + codes[+i] + '</code>');
  return s;
}

function renderMarkdown(src) {
  const lines = esc(src).replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let para = [];
  let list = null;
  let items = [];

  const flushPara = () => {
    if (para.length) { blocks.push(para.map(renderInlineMd).join('<br>')); para = []; }
  };
  const flushList = () => {
    if (list) {
      blocks.push('<' + list + '>' + items.map((t) => '<li>' + renderInlineMd(t) + '</li>').join('') + '</' + list + '>');
      list = null; items = [];
    }
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul) { flushPara(); if (list !== 'ul') { flushList(); list = 'ul'; } items.push(ul[1]); continue; }
    if (ol) { flushPara(); if (list !== 'ol') { flushList(); list = 'ol'; } items.push(ol[1]); continue; }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  // 段落之间用 \n 分隔（父元素是 pre-wrap，会渲染成换行）
  return blocks.join('\n');
}

/* ---------- 思考过程 ---------- */

/** 生成"TA 当时在想什么"折叠块 */
function buildThinkBlock(thinking) {
  const det = document.createElement('details');
  det.className = 'think';
  const sum = document.createElement('summary');
  sum.textContent = 'TA 当时在想什么';
  const body = document.createElement('div');
  body.className = 'think-body';
  body.textContent = thinking;
  det.appendChild(sum); det.appendChild(body);
  return det;
}

/* ---------- 回到最新（微信式） ---------- */

/**
 * 用户往上翻历史时，右下角出现一个悬浮按钮：
 * - 没有新消息：显示"回到最新"
 * - 有新消息：显示"N 条新消息"
 * 点一下直接跳到底部。
 */
function updateJumpButton() {
  const btn = $('btn-jump-latest');
  if (!btn) return;
  const show = S.viewMode === 'live' && !wasAtBottom;
  btn.classList.toggle('hidden', !show);
  const label = $('jump-latest-label');
  if (label) label.textContent = S.newCount > 0 ? `${S.newCount} 条新消息` : '回到最新';
}

/** 重新渲染当前这屏，但保持用户正在看的位置 */
function rerenderKeepScroll() {
  const box = $('chat');
  const prevH = box.scrollHeight;
  const prevTop = box.scrollTop;
  renderChat();
  box.scrollTop = prevTop + (box.scrollHeight - prevH);
  updateJumpButton();
}

function scrollBottom(instant) {
  const box = $('chat');
  if (!box) return;
  if (instant) {
    // 直接赋值 scrollTop：一步到底，不受 CSS scroll-behavior 影响
    box.scrollTop = box.scrollHeight;
  } else {
    box.scrollTo({ top: box.scrollHeight, behavior: 'smooth' });
  }
}

/**
 * 锚定到底部（渲染完 / 高度变化后用）
 *
 * 为什么不能只调一次 scrollBottom：
 * - 渲染后布局可能还没稳定（字体、异步图片、app 高度重算），
 *   内容再撑高一点，滚动位置就又不贴底了；
 * - 所以在两帧之后再补两次，保证最终落点一定是底部。
 */
function anchorBottom() {
  scrollBottom(true);
  requestAnimationFrame(() => {
    scrollBottom(true);
    requestAnimationFrame(() => {
      scrollBottom(true);
      wasAtBottom = true;
      markVisibleChatRead();
    });
  });
}

/* ---------- 历史跳转 ---------- */

function updateHistoryBar() {
  const bar = $('history-bar');
  if (S.viewMode === 'live') { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  $('history-label').textContent = S.viewLabel || '正在查看历史聊天';
}

async function jumpToDate(date, firstId) {
  try {
    if (!firstId) { toast('这一天没有聊天记录'); return; }
    // 定位到那天**第一条**消息，但视图仍然是普通聊天界面
    const { older, newer } = await loadAround(firstId);
    S.messages = older.concat(newer);
    S.viewMode = 'jump';
    S.viewLabel = `正在查看 ${dayLabel(date)} 附近的聊天`;
    S.hasMore = older.length >= PAGE;
    updateHistoryBar();
    renderChat();
    updateJumpButton();
    showView('chat');
    setTimeout(() => {
      const el = $('chat').querySelector(`.msg[data-id="${firstId}"]`);
      // 跨日期定位要瞬移（不是平滑滚），不然要滚好久
      if (el) el.scrollIntoView({ block: 'start', behavior: 'instant' });
      else anchorBottom();
    }, 80);
    toast(S.viewLabel);
  } catch (e) { toast('加载失败'); }
}

async function jumpToMessage(id) {
  try {
    // 目标消息本身也要在列表里，所以取「它之前的一页」+「它之后的全部」
    const { older, newer } = await loadAround(Number(id));
    S.messages = older.concat(newer);
    S.viewMode = 'jump';
    S.viewLabel = '正在查看历史聊天';
    S.hasMore = older.length >= PAGE;
    updateHistoryBar();
    renderChat();
    updateJumpButton();
    closeSearch();
    showView('chat');
    // 滚到目标消息
    setTimeout(() => {
      const el = $('chat').querySelector(`.msg[data-id="${id}"]`);
      if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      else anchorBottom();
    }, 80);
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
    box.innerHTML = '<div class="empty">输入关键词，搜索过去的聊天和 TA 记得的事…</div>';
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
    const label = { message: '聊天记录', memory: 'TA 记得的' };
    Object.keys(groups).forEach((kind) => {
      if (!groups[kind].length) return;
      html += `<div class="drawer-label" style="margin:12px 0 6px">${label[kind] || kind}（${groups[kind].length}）</div>`;
      groups[kind].forEach((h) => {
        const text = esc(h.content).replace(re, (m) => `<mark>${m}</mark>`);
        const meta = h.kind === 'memory'
          ? `${esc(h.memory_kind || '')}`
          : `${esc(h.role === 'user' ? '我' : 'TA')} · ${fmtTime(h.created_at)}`;
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
  clearTimeout(S.reconnectTimer);
  setStatus('连接中…', '');
  try { if (S.ws) S.ws.close(); } catch (e) { /* ignore */ }
  const ws = new WebSocket(wsUrl());
  S.ws = ws;

  ws.onopen = () => {
    if (S.ws !== ws) return;
    S.connected = true;
    setStatus('', 'online');        // 不再显示"在线"（AI 一直都在）
    // 连接恢复了：可能有一轮卡在"等待回复"，这里清掉
    resetSending();
    loadConvs();
  };
  ws.onclose = () => {
    if (S.ws !== ws) return;
    S.connected = false;
    setStatus('已断开 · 重连中…', 'offline');
    // 只清理临时显示；服务器继续生成，重连后恢复正式消息。
    resetSending();
    if (S.streamEl) { S.streamEl.remove(); S.streamEl = null; }
    recoverReply(true);
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = setTimeout(connect, 2500);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
  ws.onmessage = (ev) => {
    if (S.ws !== ws) return;
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    try {
      handleWs(m);
    } catch (e) {
      // 安全网：一条消息处理出错，绝不能让界面卡在坏状态
      // （以前就是这里抛了异常，发送按钮永久变灰、能打字但发不出去）
      console.error('处理服务端消息出错', e);
      resetSending();
      toast('出了点问题，已经恢复', 3000);
    }
  };
}

function outboxKey(user=S.user,persona=S.persona) { return `companion-outbox:${user}:${persona}`; }
function readOutbox(user=S.user,persona=S.persona) {
  try { return JSON.parse(localStorage.getItem(outboxKey(user,persona)) || '{}'); } catch (_) { return {}; }
}
function writeOutbox(value,user=S.user,persona=S.persona) { localStorage.setItem(outboxKey(user,persona), JSON.stringify(value)); }

const outboxFlushes = new Map();
function flushOutbox(user,persona) {
  const key=pkey(user,persona), scope=`/api/persona/${enc(user)}/${enc(persona)}`;
  if (outboxFlushes.has(key)) return outboxFlushes.get(key);
  const work=(async () => {
    for (const [id,content] of Object.entries(readOutbox(user,persona))) {
      const receipt=await api('/api/messages',{method:'POST',body:JSON.stringify({user_id:user,persona_id:persona,content,client_message_id:id})});
      const pending=readOutbox(user,persona); delete pending[id]; writeOutbox(pending,user,persona);
      if (scope===base()) {
        const saved=S.messages.find(m=>m.client_message_id===id);
        if (saved) { saved.id=receipt.message_id; saved.pending=false; }
        if (!S.latestClientMessageId || S.latestClientMessageId===id) S.generationId=receipt.generation_id;
      }
    }
  })();
  outboxFlushes.set(key,work);
  return work.finally(()=>{ if (outboxFlushes.get(key)===work) outboxFlushes.delete(key); });
}

let replyRecoveryTimer = null;
let replyRecoveryEpoch = 0;
function stopReplyRecovery() {
  clearTimeout(replyRecoveryTimer);
  replyRecoveryTimer = null;
  replyRecoveryEpoch += 1;
}

async function recoverReply(refreshHistory = false) {
  stopReplyRecovery();
  if (!S.user || !S.persona) return;
  const epoch = replyRecoveryEpoch, scope = base(), clientId = S.latestClientMessageId;
  const current = () => epoch === replyRecoveryEpoch && scope === base() && clientId === S.latestClientMessageId;
  try {
    while (Object.keys(readOutbox()).length) {
      await flushOutbox(S.user,S.persona);
      if (!current()) return;
    }
    const g = await api(`${scope}/generation`);
    if (!current()) return;
    if (refreshHistory && S.viewMode === 'live') {
      await loadHistory(true, { keepScroll: !wasAtBottom });
      if (!current()) return;
    }
    if (!g) return;
    S.generationId = g.id;
    S.replyState = {...g, scope};
    if (S.viewMode === 'live') {
      for (const message of g.messages) {
        if (!S.messages.some(m => m.id === message.id)) handleWs({type: 'message_done', generation_id: g.id, message});
      }
    }
    if (g.status === 'queued' || g.status === 'running') {
      setTyping(true);
      replyRecoveryTimer = setTimeout(() => recoverReply(), 1500);
    } else {
      resetSending();
    }
    renderReplyStatus();
  } catch (_) {
    if (current()) replyRecoveryTimer = setTimeout(() => recoverReply(refreshHistory), 2500);
  }
}

function handleWs(m) {
  if (m.generation_id && ['start', 'delta', 'thinking', 'message_done', 'done', 'cancelled', 'error'].includes(m.type)
      && m.generation_id !== S.generationId) return;
  switch (m.type) {
    case 'received': {
      const saved = S.messages.find(x => x.client_message_id === m.client_message_id);
      if (saved) { saved.id = m.message_id; saved.pending = false; }
      const pending = readOutbox();
      delete pending[m.client_message_id];
      writeOutbox(pending);
      if (!S.latestClientMessageId || m.client_message_id === S.latestClientMessageId) S.generationId = m.generation_id;
      recoverReply(true);
      break;
    }
    case 'message_done': {
      if (S.streamEl) { S.streamEl.remove(); S.streamEl = null; }
      const saved = m.message;
      if (!S.messages.some(x => x.id === saved.id)) {
        const el = renderMsg('assistant', saved.content, S.showThinking ? saved.thinking : null, saved.id);
        appendChatMsg(el, saved.created_at);
        S.messages.push(saved);
        if (wasAtBottom) scrollBottom();
      }
      S.streamThinking = '';
      renderReplyStatus();
      markVisibleChatRead();
      break;
    }
    case 'cancelled': {
      stopReplyRecovery();
      if (S.streamEl) { S.streamEl.remove(); S.streamEl = null; }
      resetSending();
      break;
    }
    case 'ready':
      // Retry only unacknowledged input, with its original idempotency key.
      for (const [id, content] of Object.entries(readOutbox())) {
        S.latestClientMessageId = id;
        S.ws.send(JSON.stringify({type: 'chat', content, client_message_id: id}));
      }
      setStatus('', 'online')  // 不显示"在线"（对 AI 来说它一直都在）;
      // 连上了说明服务器活着了 → 刷新聊天列表（服务器重启后自动恢复）
      loadConvs();
      recoverReply(true);
      break;
    case 'start': {
      // 不再往聊天区插空气泡了（那个框一直跳很吵），
      // 改成顶栏中间的"对方正在输入…"
      const box = $('chat');
      const empty = box.querySelector('.empty');
      if (empty) empty.remove();
      S.streamEl = null;
      S.streamThinking = '';
      setTyping(true);
      startSendWatchdog();
      break;
    }
    case 'delta': {
      if (!S.streamEl) {
        // 第一段文字到了才真正建气泡（等待期间只有顶栏的提示）
        const el = renderMsg('assistant', '', null, null);
        const cur = document.createElement('span');
        cur.className = 'cursor';
        el.firstChild.appendChild(cur);
        appendChatMsg(el, new Date().toISOString());
        S.streamEl = el;
        // 只有用户本来就在底部才自动跟到底，否则会把正在翻历史的人硬拽下去
        if (wasAtBottom) scrollBottom();
      }
      const text = S.streamEl.firstChild;
      const cur = text.querySelector('.cursor');
      if (cur) cur.remove();
      text.textContent += m.content || '';
      if (wasAtBottom) scrollBottom();
      break;
    }
    case 'thinking': {
      // 服务端单独推来的思考过程，先存着，done 时挂到消息上
      S.streamThinking = m.content || '';
      break;
    }
    case 'done': {
      stopReplyRecovery();
      S.replyState = null; renderReplyStatus();
      if (S.streamEl) {
        const cur = S.streamEl.querySelector('.cursor');
        if (cur) cur.remove();
        const think = S.streamThinking || null;
        // 思考过程：有就存进内存（开关打开时立刻能显示）
        if (think && S.showThinking) {
          S.streamEl.appendChild(buildThinkBlock(think));
        }
        // 流式期间是纯文本，收尾时升级成 markdown 渲染
        // 注意：入库/进内存的必须是**原始文本**（含 ** 标记），不能是渲染后的
        const rawText = S.streamEl.firstChild.textContent;
        S.streamEl.firstChild.innerHTML = renderMarkdown(rawText);
        // 同步进 S.messages（保持内存态一致）
        S.messages.push({
          id: Date.now(), role: 'assistant', localOnly:true,
          content: rawText,
          thinking: think,
          created_at: new Date().toISOString(),
        });
        // 用户往上翻着的时候来了新消息 → 记角标，别把人拽回底部
        if (wasAtBottom) scrollBottom();
        else S.newCount += 1;
      }
      S.streamEl = null;
      S.streamThinking = '';
      S.sending = false;
      $('btn-send').disabled = false;
      stopSendWatchdog();
      setTyping(false);
      updateJumpButton();
      refreshConv(S.user, S.persona);
      if (m.generation_id) loadHistory(true);
      break;
    }
    case 'error': {
      stopReplyRecovery();
      if (m.generation_id) S.replyState = {id:m.generation_id, scope:base(), status:'failed', last_error:m.message};
      renderReplyStatus();
      if (S.streamEl) { S.streamEl.remove(); S.streamEl = null; }
      S.sending = false;
      $('btn-send').disabled = false;
      stopSendWatchdog();
      setTyping(false);
      if (m.generation_id) recoverReply(true);
      else toast('出错了：' + taskFailureText(m.message), 4000);
      break;
    }
    case 'proactive_message': {
      refreshConv(m.user_id,m.persona_id);
      if (m.user_id===S.user && m.persona_id===S.persona && S.viewMode==='live') {
        loadHistory(true,{keepScroll:!wasAtBottom});
      }
      break;
    }
    case 'summary_updated': {
      const user = m.user_id || S.user, persona = m.persona_id || S.persona;
      api(`/api/persona/${enc(user)}/${enc(persona)}`).then(info => {
        S.summaries[pkey(user,persona)] = info;
        renderRelationIfOpen(user,persona); renderProfileIfOpen(user,persona);
      }).catch(() => {});
      break;
    }
    case 'record_done': {
      // 后台记录跑完了（手动点的 / 空闲自动的都有）
      // 服务端主动推的 —— 所以关系状态页不用退出重进就会自己刷新。
      //
      // 注意：通讯录里可以打开**任意角色**的资料页，
      // 而 WS 只连着当前在聊的那个角色 —— 所以服务端按 user 推，
      // 事件里带 persona_id，这里判断是不是当前角色再决定要不要打扰。
      const u = m.user_id || S.user;
      const p = m.persona_id || S.persona;
      const key = pkey(u, p);
      const isCurrent = (u === S.user && p === S.persona);
      if (m.more_pending) {
        S.recording[key] = true;
        renderRelationIfOpen(u, p);
        break;
      }
      const wasPending = !!(S.recordPending && S.recordPending.key === key);
      S.recording[key] = false;
      if (S.recordPending && S.recordPending.key === key) S.recordPending = null;
      // 重新拉一次汇总：关系状态、记忆条数、上次记录时间都变了
      api(`/api/persona/${enc(u)}/${enc(p)}`).then((info) => {
        S.summaries[key] = info;
        renderRelationIfOpen(u, p);
        renderProfileIfOpen(u, p);
        if (isCurrent) loadConvs();
      }).catch(() => { /* ignore */ });
      if (!isCurrent || (!S.debug && !wasPending)) break;
      if (m.errors && m.errors.length) { toast('记忆整理未完成，可在关系页点「记一次」重试：' + m.errors[0], 5000); break; }
      const bits = [];
      if (m.facts_created) bits.push(`记住了 ${m.facts_created} 件事`);
      if (m.notes_updated) bits.push(`更新了 ${m.notes_updated} 条记忆`);
      if (m.blocks_updated) bits.push(`关系状态更新了 ${m.blocks_updated} 项`);
      if (wasPending) {
        toast(bits.length ? `记好了，${bits.join('，')}` : '记好了', 3000);
      } else {
        toast(bits.length ? `刚才那轮已自动记下，${bits.join('，')}` : '刚才那轮已自动记下', 3000);
      }
      break;
    }
    case 'session_closed': {
      const bits = [];
      if (m.facts_created) bits.push(`记住了 ${m.facts_created} 件事`);
      if (m.blocks_updated) bits.push(`更新了 ${m.blocks_updated} 项状态`);
      toast(bits.length ? bits.join('，') : '这次聊天已结束', 3200);
      loadPersona(); loadConvs();
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
  if (!S.user || !S.persona) return;
  stopReplyRecovery();
  // A new user message interrupts only the unfinished assistant draft.
  if (S.streamEl) { S.streamEl.remove(); S.streamEl = null; }
  S.generationId = null;
  S.replyState = null; renderReplyStatus();
  const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  S.latestClientMessageId = requestId;

  const box = $('chat');
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();
  const now = new Date().toISOString();
  const el = renderMsg('user', text, null, null);
  appendChatMsg(el, now);
  S.messages.push({ id: Date.now(), role: 'user', content: text, created_at: now, client_message_id: requestId, pending:true });
  input.value = '';
  input.style.height = 'auto';

  const pending = readOutbox();
  pending[requestId] = text;
  writeOutbox(pending);
  if (S.connected && S.ws && S.ws.readyState === WebSocket.OPEN) {
    try { S.ws.send(JSON.stringify({ type: 'chat', content: text, client_message_id: requestId })); }
    catch (_) { recoverReply(true); }
  } else recoverReply(true);
  // An OPEN mobile socket may already be dead without emitting close/received.
  // Retry the durable receipt over HTTP with the same idempotency key.
  setTimeout(()=>{ if (S.latestClientMessageId===requestId && readOutbox()[requestId]) recoverReply(true); },1500);
  S.sending = true;
  $('btn-send').disabled = false;
  scrollBottom();
  // 键盘弹起时视口会变小，待底部重新滚动，避免新消息被掩住
  setTimeout(() => {
    syncViewport();
    if (wasAtBottom) scrollBottom();
  }, 320);
}

/* ---------- 事件绑定 ---------- */

$('btn-send').onclick = send;
$('btn-search').onclick = openSearch;
$('btn-search-close').onclick = closeSearch;
$('btn-back-live').onclick = backToLive;
$('btn-jump-latest').onclick = () => {
  S.newCount = 0;
  wasAtBottom = true;
  anchorBottom();
  updateJumpButton();
};

/* ---------- 新页面导航 ---------- */

$('btn-back-list').onclick = () => showView('messages');
$('btn-avatar').onclick = () => openProfile(S.user, S.persona);
$('btn-profile-back').onclick = () => showView(S.profileFrom || 'chat');
$('btn-memory-back').onclick = () => showView('profile');
$('btn-relation-back').onclick = () => showView('profile');
$('btn-new-conv').onclick = () => toast('新建会话：还没做', 2500);
$('btn-conv-search').onclick = () => {
  const bar = $('conv-searchbar');
  const open = bar.classList.toggle('hidden');
  if (!open) {
    const input = $('conv-search');
    if (input) { input.value = ''; S.convQuery = ''; renderConvs(); input.focus(); }
  }
};
$('btn-cal-close').onclick = closeCalendar;
$('cal-scroll').addEventListener('scroll', () => {
  const el = $('cal-scroll');
  if (el.scrollTop < 140) loadOlderMonths();
  updateCalTitle();
});
$('btn-cal-today').onclick = () => {
  const cur = $('cal-scroll').querySelector(`.cal-month[data-month="${S.calCurMonth}"]`);
  if (cur) $('cal-scroll').scrollTo({ top: cur.offsetTop, behavior: 'smooth' });
};

document.querySelectorAll('.tab').forEach((tab) => {
  tab.onclick = () => {
    showView(tab.dataset.view);
    if (tab.dataset.view === 'contacts') renderContacts();
  };
});

$('conv-search').addEventListener('input', (e) => {
  S.convQuery = e.target.value;
  renderConvs();
});
$('contact-search').addEventListener('input', (e) => {
  S.contactQuery = e.target.value;
  renderContacts();
});

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
    if (S.viewMode === 'live') {
      // 重拉这一页（带/不带 think），并保持用户正在看的位置
      await loadHistory(true, { withThinking: S.showThinking, keepScroll: true });
    } else {
      rerenderKeepScroll();
    }
    toast(S.showThinking ? '会显示 TA 的思考过程' : '不显示思考过程');
  } catch (err) { toast('设置失败'); }
});

$('pin-submit').onclick = async () => {
  const button = $('pin-submit');
  if (button.disabled) return;
  button.disabled = true;
  ++bootRequest;
  showGate();
  const pin = $('pin-input').value.trim();
  try {
    await verifyAccess(pin);
    S.pin = pin;
    localStorage.setItem('ce_pin', S.pin);
    $('pin-input').value = '';
    $('gate-error').textContent = '';
    await boot();
  } catch (e) {
    $('gate-error').textContent = e.status === 401 ? 'PIN 不对' : '暂时无法验证，请稍后重试';
  } finally {
    button.disabled = false;
  }
};
$('pin-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('pin-submit').click(); });

/* 新建角色对话框 */
$('btn-persona-cancel').onclick = () => $('dialog-persona').classList.add('hidden');
$('btn-persona-save').onclick = savePersona;
// 点遮罩关闭（右上角不放叉号——底部的"取消"已经够了）
$('dialog-persona').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) $('dialog-persona').classList.add('hidden');
});

/* 往上滑到顶 → 加载更早的 */
$('chat').addEventListener('scroll', () => {
  const box = $('chat');
  if (box.scrollTop < 80) loadOlder();
  // 记住是否贴在底部（发送时不要把用户拉走）
  wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  // 自己滑回底部了 → 角标清零
  if (wasAtBottom) { S.newCount = 0; markVisibleChatRead(); }
  updateJumpButton();
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
    // 手机后台的旧 socket 可能看似 OPEN；新连接的 ready 会恢复正式消息和任务状态。
    connect();
    recoverReply(true);
    loadConvs();
    if (S.view === 'relation' && S.relationPersona) {
      const target = S.relationPersona;
      api(`/api/persona/${enc(target.user)}/${enc(target.persona)}`).then(info => {
        if (S.view === 'relation' && S.relationPersona === target) {
          S.summaries[pkey(target.user,target.persona)] = info;
          renderRelationIfOpen(target.user,target.persona);
        }
      }).catch(() => {});
    }
  } else {
    stopReplyRecovery();
  }
});

// Safari-family browsers can restore a frozen page from the back/forward cache
// without a new websocket ready event. HTTP recovery does not depend on it.
window.addEventListener('pageshow', () => { if (S.user && S.persona) recoverReply(true); });
window.addEventListener('online', () => { if (S.user && S.persona) { connect(); recoverReply(true); } });

if ('serviceWorker' in navigator) {
  navigator.serviceWorker?.addEventListener?.('message', event => {
    if (event.data?.type!=='notification_open' || $('app').classList.contains('hidden') || !S.user) return;
    let url;
    try {url=new URL(event.data.url,location.href);} catch(error) {return;}
    if (url.origin!==new URL(location.href).origin || url.pathname!=='/') return;
    const user=url.searchParams.get('user'), persona=url.searchParams.get('persona');
    event.ports?.[0]?.postMessage({handled:true});
    if (!user || !persona) {showView('messages');return;}
    if (!S.users.some(item=>item.id===user && (item.personas||[]).includes(persona))) {toast('该会话暂不可用');return;}
    openChat(user,persona).catch(()=>toast('暂时无法打开会话'));
  });
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', {updateViaCache: 'none'}).catch(() => { /* 非 HTTPS 会失败 */ });
  });
}

syncViewport();
boot();
