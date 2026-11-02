/* Device notification settings. The host supplies authenticated API and target access. */
window.CompanionNotifications = (() => {
  let host, registration, config, subscription, subscribed = false, busy = false, sequence = 0;
  const el = id => document.getElementById(id);
  const standalone = () => navigator.standalone === true || !!window.matchMedia?.('(display-mode: standalone)').matches;
  const ios = () => /iPhone|iPad|iPod/.test(navigator.userAgent || '') || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  function unsupported() {
    if (!window.isSecureContext) return '请使用 HTTPS 地址开启通知';
    if (ios() && !standalone()) return '请从主屏幕打开同行，再开启通知';
    if (!('Notification' in window) || !('PushManager' in window) || !navigator.serviceWorker) return '当前浏览器暂不支持消息通知';
    return '';
  }
  function render(message = '') {
    const toggle = el('notification-toggle');
    if (!toggle) return;
    const denied = window.Notification?.permission === 'denied';
    toggle.checked = subscribed;
    toggle.disabled = busy || !!unsupported() || (!subscribed && (!config?.configured || !registration || denied));
    toggle.setAttribute('aria-busy', String(busy));
    const hint = message || unsupported()
      || (denied ? '通知权限已关闭，可在系统设置中允许同行发送通知' : '')
      || (config && !config.configured ? '服务器尚未开启通知' : '');
    el('notification-hint').textContent = hint;
    el('notification-hint').classList.toggle('hidden', !hint);
    toggle.title = hint;
  }
  function keyBytes(value) {
    const raw = atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
  }
  async function ready() {
    let timer;
    try {
      return await Promise.race([navigator.serviceWorker.ready, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('通知初始化未完成，请稍后再打开此页')), 10000);
      })]);
    } finally { clearTimeout(timer); }
  }
  async function refresh() {
    if (!host || busy || unsupported()) { render(); return; }
    const current = ++sequence, user = host.target().user_id;
    try {
      const nextConfig = await host.api('/api/notifications/config');
      const nextRegistration = await ready();
      const nextSubscription = await nextRegistration.pushManager.getSubscription();
      let enabled = false;
      if (nextSubscription) {
        const state = await host.api('/api/notifications/status', {method: 'POST',
          body: JSON.stringify({user_id: user, endpoint: nextSubscription.endpoint})});
        enabled = state.subscribed;
      }
      if (current !== sequence || user !== host.target().user_id) return;
      config = nextConfig; registration = nextRegistration; subscription = nextSubscription; subscribed = enabled;
      render();
    } catch (error) {
      if (current === sequence) render(error.message || '暂时无法读取通知设置');
    }
  }
  async function enable(target) {
    if (!config?.configured || !registration) throw new Error('通知尚未就绪，请稍后再试');
    // This call must happen before any awaited network request: iOS requires a user gesture.
    const permission = window.Notification.permission === 'granted' ? 'granted' : await window.Notification.requestPermission();
    if (permission !== 'granted') throw new Error('未开启通知权限');
    subscription = await registration.pushManager.getSubscription();
    const expected = keyBytes(config.public_key);
    const actual = subscription?.options?.applicationServerKey;
    if (subscription && actual && (new Uint8Array(actual).length !== expected.length
      || new Uint8Array(actual).some((value, index) => value !== expected[index]))) {
      await host.api('/api/notifications/subscriptions', {method: 'DELETE',
        body: JSON.stringify({user_id: target.user_id, endpoint: subscription.endpoint})});
      if (!await subscription.unsubscribe()) throw new Error('旧通知订阅未能更新，请稍后再试');
      subscription = null;
    }
    if (!subscription) subscription = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: expected});
    await host.api('/api/notifications/subscriptions', {method: 'POST',
      body: JSON.stringify({user_id: target.user_id, subscription: subscription.toJSON()})});
    subscribed = true;
    host.toast('通知连接已开启');
  }
  async function disable(target) {
    if (!subscription) { subscribed = false; return; }
    await host.api('/api/notifications/subscriptions', {method: 'DELETE',
      body: JSON.stringify({user_id: target.user_id, endpoint: subscription.endpoint})});
    subscribed = false;
    if (!await subscription.unsubscribe()) throw new Error('服务器已关闭通知，设备订阅暂未移除');
    subscription = null;
    host.toast('通知已关闭');
  }
  async function perform(action) {
    if (busy) return;
    busy = true; ++sequence; render();
    let message = '';
    try { await action({...host.target()}); }
    catch (error) { message = error.message || '通知操作未完成'; host.toast(message); }
    finally { busy = false; render(message); }
    await refresh();
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && host?.active?.()) refresh();
  });
  return {
    init(adapter) {
      host = adapter;
      el('notification-toggle').onclick = () => perform(subscribed ? disable : enable);
      render();
      return refresh();
    }, refresh,
  };
})();
