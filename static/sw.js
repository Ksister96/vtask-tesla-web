// car-web PWA Service Worker — 外壳缓存（网络优先，核心静态文件离线可用）
const CACHE = 'carweb-multi-v75-nofrag';
const CORE = ['/', '/index.html', '/manifest.json', '/icons/favicon-32.png', '/icons/apple-touch-icon.png', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/icon-maskable-192.png', '/icons/icon-maskable-512.png', '/icons/og-image.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});

// Web Push 消息推送处理
self.addEventListener('push', (e) => {
  let data = { title: '⚡ V-TASK 智能车控', body: '收到新提醒' };
  if (e.data) {
    try {
      data = Object.assign(data, e.data.json());
    } catch (err) {
      data.body = e.data.text();
    }
  }

  const options = {
    body: data.body || '',
    icon: '/icons/icon-192.png',
    badge: '/icons/favicon-32.png',
    data: data.url || '/',
    vibrate: [100, 50, 100],
    renotify: true,
    tag: data.id || 'vtask-notification'
  };

  e.waitUntil(self.registration.showNotification(data.title || '⚡ V-TASK 智能车控', options));
});

// 点击通知唤起并激活页面
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const targetUrl = e.notification.data || '/';

  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (let i = 0; i < windowClients.length; i++) {
        const client = windowClients[i];
        if (client.url.indexOf(self.location.origin) !== -1 && 'focus' in client) {
          client.focus();
          // 通知已打开的页面：拉取最新公告抽屉
          try { client.postMessage({ type: 'OPEN_ANNOUNCEMENT' }); } catch (err) {}
          return;
        }
      }
      if (clients.openWindow) {
        // 无窗口时新开：带时间戳参数强制走网络拿最新页面
        const sep = targetUrl.indexOf('?') >= 0 ? '&' : '?';
        return clients.openWindow(targetUrl + sep + 'ts=' + Date.now());
      }
    })
  );
});

// v51: 缓存优先（秒开）+ 后台静默更新缓存（stale-while-revalidate for HTML）
// 核心静态文件（HTML/CSS/JS/图标）走缓存优先：命中即返回，不等网络；
// 同时后台更新缓存，下次打开拿到新版本。
// API 请求不拦截，走原生 fetch。
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;
  if (url.origin !== location.origin) return;
  // 页面导航网络优先：在线必拿最新（防 PWA 重开回到旧功能），离线才回退缓存
  if (e.request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    // HTML 永不进运行时缓存：半截响应会被缓存成毒页面（2026-10-08 事故）。
    // 离线回退用安装期 addAll 的完整副本。
    e.respondWith(
      fetch(e.request).catch(() => caches.match('/index.html') || caches.match('/'))
    );
    return;
  }
  e.respondWith(
    caches.match(e.request).then((cached) => {
      // 后台静默更新：不阻塞页面加载
      fetch(e.request).then((res) => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone)).catch(() => {});
        }
      }).catch(() => {});
      // 缓存命中 → 立即返回（秒开）；未命中 → 等网络
      return cached || fetch(e.request);
    })
  );
});
