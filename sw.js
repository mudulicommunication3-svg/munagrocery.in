/* ==========================================================================
   MUNNA MOBILE & Grocery — Service Worker (offline shell + static caching)
   Registered from index.html. Network-first for pages (fresh shop always),
   cache-first background-refresh for same-origin static files.
   Bump CACHE when the shell changes so phones pick up the new version.
   ========================================================================== */
var CACHE = 'munna-shell-v3';
var CORE = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './apple-touch-icon.png'
];

// Precache the app shell on install.
self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return Promise.all(
        CORE.map(function (url) {
          // One missing file must never break the whole install.
          return cache.add(url).catch(function () {});
        })
      );
    })
  );
  self.skipWaiting();
});

// Drop caches from older versions on activate.
self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (k) { return k === CACHE ? null : caches.delete(k); })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') return;

  var url;
  try { url = new URL(req.url); } catch (e) { return; }

  // Cross-origin (Google Sheet webhook, CDN, images, Telegram) -> browser
  // default. The live catalog/order data must NEVER be served from cache.
  if (url.origin !== self.location.origin) return;

  // Pages / navigations: network-first, cached shell when offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); }).catch(function () {});
        return res;
      }).catch(function () {
        return caches.match(req).then(function (r) {
          return r || caches.match('./index.html');
        });
      })
    );
    return;
  }

  // Same-origin static (icons, manifest, scripts): cache-first, refresh in
  // the background so repeat visits are instant.
  event.respondWith(
    caches.match(req).then(function (cached) {
      var network = fetch(req).then(function (res) {
        if (res && res.status === 200) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); }).catch(function () {});
        }
        return res;
      }).catch(function () { return cached; });
      return cached || network;
    })
  );
});
