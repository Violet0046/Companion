/* ============================================================
   Service Worker —— PWA 外壳缓存 + 推送接收
   注意：Service Worker 只在安全上下文（HTTPS / localhost）下注册。
   局域网 http://192.168.x.x:8000 下这里会注册失败，属于预期。
   ============================================================ */

const CACHE = 'ce-shell-v17';
const SHELL = [
  '/',
  '/index.html',
  '/app.js',
  '/notifications.js',
  '/styles.css',
  '/manifest.webmanifest',
  '/icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL).catch(() => { /* 某个文件 404 也不该阻塞安装 */ }))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('ce-shell-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* 网络优先，失败回落缓存（聊天接口不缓存） */
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) return;

  e.respondWith(
    fetch(req, {cache: 'no-cache'})
      .then((res) => {
        if (!res.ok) return res;
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(async () => {
        const saved = await caches.match(req);
        if (saved) return saved;
        if (req.mode === 'navigate') return await caches.match('/index.html') || Response.error();
        return Response.error();
      })
  );
});

/* 推送通知（主动触达功能启用后由服务端推送） */
self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch (err) { data = {}; }
  if (!data || typeof data !== 'object') data = {};
  const title = data.title || '同行';
  const options = {
    body: data.body || '',
    icon: '/icon.png',
    badge: '/icon.png',
    tag: data.tag || 'ce',
    renotify: false,
    data: { url: notificationURL(data.url) },
  };
  e.waitUntil(self.registration.showNotification(title, options));
});

function notificationURL(value) {
  try {
    const url = new URL(value || '/', self.location.origin);
    if (url.origin === self.location.origin && url.pathname === '/') return url.href;
  } catch (error) { /* Invalid payload falls back to the app home page. */ }
  return self.location.origin + '/';
}

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const destination = notificationURL(e.notification.data?.url);
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(async (list) => {
        const c = list.find((x) => 'focus' in x && 'navigate' in x && new URL(x.url).origin === self.location.origin);
        if (c) {
          await c.focus();
          const handled=await wakeConversation(c,destination);
          if (handled) return;
          return c.navigate(destination).then(client => client?.focus());
        }
        if (self.clients.openWindow) return self.clients.openWindow(destination);
      })
  );
});

function wakeConversation(client,url) {
  if (typeof MessageChannel==='undefined' || !client.postMessage) return Promise.resolve(false);
  return new Promise(resolve=>{
    const channel=new MessageChannel();
    const finish=value=>{clearTimeout(timer);channel.port1.close();channel.port2.close();resolve(value);};
    const timer=setTimeout(()=>finish(false),1500);
    channel.port1.onmessage=event=>finish(event.data?.handled===true);
    try {client.postMessage({type:'notification_open',url},[channel.port2]);}
    catch(error) {finish(false);}
  });
}
