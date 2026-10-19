/* munnamobile :: sw.js — service worker (offline shell + safe live sync)
   Strategy: NETWORK-FIRST for every same-origin request with a cache
   fallback, so customers always get fresh Sheet/catalog data online and a
   working app offline. Cross-origin calls (Google Sheet webhook, JioMart
   proxies, CDNs) are never intercepted. Bump VERSION when deploying a
   change so old clients drop their cached copy. */
const VERSION = 'munna-v1';
const CACHE = 'munna-shell-' + VERSION;

const PAGES = ['home', 'product', 'cart', 'orders', 'address', 'profile',
  'modal-admin-login', 'modal-order-alert', 'admin', 'scanner', 'modal-login'];
const SCRIPTS = ['core', 'catalog', 'checkout', 'product-detail', 'navigation',
  'admin', 'admin-settings', 'image-search', 'admin-catalog', 'data-helpers',
  'sheet-post', 'sheet-sync', 'address', 'profile', 'scanner', 'auth', 'init'];

const CORE = ['./', 'index.html', 'manifest.json', 'css/styles.css',
  'icon-192.png', 'icon-512.png', 'apple-touch-icon.png']
  .concat(PAGES.map(function (p) { return 'pages/' + p + '.html'; }))
  .concat(SCRIPTS.map(function (s) { return 'js/' + s + '.js'; }));

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function (c) { return c.addAll(CORE); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        return k === CACHE ? null : caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (event) {
  const req = event.request;
  if (req.method !== 'GET') return;                    // POST (sheet writes) untouched
  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;     // API/CDN never intercepted

  event.respondWith(
    fetch(req).then(function (res) {
      if (res && res.ok && res.type === 'basic') {
        const copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); }).catch(function () {});
      }
      return res;
    }).catch(function () {
      return caches.match(req, { ignoreSearch: false }).then(function (hit) {
        if (hit) return hit;
        if (req.mode === 'navigate') return caches.match('index.html');
        return new Response('', { status: 504, statusText: 'offline' });
      });
    })
  );
});
