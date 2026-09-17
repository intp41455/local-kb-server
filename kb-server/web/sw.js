/* 知识库随行版 · Service Worker
   目标：电脑关机 / 断网时，应用壳仍可从缓存打开。
   数据本身由页面内的 IndexedDB 负责，这里只缓存静态外壳。 */
const VERSION = 'txkb-v1';
const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // API 请求：直连网络，绝不缓存（数据走 IndexedDB）
  if (url.pathname.startsWith('/api/')) return;

  // 页面导航：网络优先，失败回退缓存的应用壳（离线打开的关键）
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(VERSION).then((c) => c.put('/index.html', copy)).catch(() => {});
          return resp;
        })
        .catch(() =>
          caches.match('/index.html').then((r) => r || caches.match('/'))
        )
    );
    return;
  }

  // 静态资源：缓存优先，后台更新
  e.respondWith(
    caches.match(req).then((hit) => {
      const refresh = fetch(req)
        .then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
          }
          return resp;
        })
        .catch(() => hit);
      return hit || refresh;
    })
  );
});
