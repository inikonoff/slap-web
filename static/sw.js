const CACHE = 'slap-v1';
const ASSETS = ['/', '/style.css', '/app.js', '/icon-192.png', '/icon-512.png'];

// Установка — кэшируем статику
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(ASSETS))
  );
  self.skipWaiting();
});

// Активация — удаляем старые кэши
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Запросы:
// - API (/api/*) — всегда сеть, никогда не кэшируем
// - Статика — сначала кэш, при промахе — сеть
// - Офлайн — показываем заглушку из кэша
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  if (url.pathname.startsWith('/api/')) {
    // API — только сеть
    e.respondWith(fetch(e.request));
    return;
  }

  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(response => {
        // Кэшируем только успешные GET-ответы
        if (e.request.method === 'GET' && response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return response;
      }).catch(() => {
        // Офлайн — возвращаем главную страницу из кэша
        return caches.match('/');
      });
    })
  );
});
