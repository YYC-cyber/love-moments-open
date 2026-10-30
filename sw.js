const CACHE = "love-moments-open-v58";
const ASSETS = [
  "/",
  "/static/index.html",
  "/static/app.css",
  "/static/app.js",
  "/static/assets/couple-lake-hero.png",
  "/static/assets/love-moments-icon.svg",
  "/static/assets/love-moments-icon-180.png",
  "/static/assets/love-moments-icon-192.png",
  "/static/assets/love-moments-icon-512.png",
  "/manifest.webmanifest"
];
const CACHEABLE_PATHS = new Set(ASSETS);

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
});

self.addEventListener("message", event => {
  if (event.data?.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key !== CACHE).map(key => caches.delete(key))
  )));
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !CACHEABLE_PATHS.has(url.pathname)) return;
  // Every public shell asset uses its canonical pathname. Cache-busting query
  // strings update the same entry and offline query variants hit that entry.
  const cacheKey = url.pathname;
  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (response.ok && !/no-store/i.test(response.headers.get("Cache-Control") || "")) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(cacheKey, copy));
        }
        return response;
      })
      .catch(() => caches.match(cacheKey))
  );
});

self.addEventListener("push", event => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = {};
  }
  const incomingCall = payload.type === "incoming_call";
  const show = self.registration.showNotification(
      String(payload.title || "恋屿有新提醒").slice(0, 80),
      {
        body: String(payload.body || "打开恋屿查看详情。").slice(0, 180),
        icon: "/static/assets/love-moments-icon-192.png",
        badge: "/static/assets/love-moments-icon-192.png",
        tag: String(payload.tag || "love-moments").slice(0, 120),
        renotify: incomingCall,
        requireInteraction: incomingCall,
        timestamp: Date.now(),
        data: {
          type: String(payload.type || "notice"),
          url: String(payload.url || "/"),
          call_id: String(payload.call_id || "")
        }
      }
    );
  const badge = typeof self.registration.setAppBadge === "function"
    ? self.registration.setAppBadge(1).catch(() => undefined)
    : Promise.resolve();
  event.waitUntil(Promise.all([show, badge]));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  let targetUrl;
  try {
    targetUrl = new URL(event.notification.data?.url || "/?screen=chat", self.location.origin);
    if (targetUrl.origin !== self.location.origin) targetUrl = new URL("/?screen=chat", self.location.origin);
  } catch (_) {
    targetUrl = new URL("/?screen=chat", self.location.origin);
  }
  const clearBadge = typeof self.registration.clearAppBadge === "function"
    ? self.registration.clearAppBadge().catch(() => undefined)
    : Promise.resolve();
  const openTarget = self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async windows => {
    const existing = windows.find(client => {
      try { return new URL(client.url).origin === self.location.origin; } catch (_) { return false; }
    });
    if (existing) {
      const navigated = await existing.navigate(targetUrl.href).catch(() => existing);
      return navigated.focus();
    }
    return self.clients.openWindow(targetUrl.href);
  });
  event.waitUntil(Promise.all([clearBadge, openTarget]));
});
