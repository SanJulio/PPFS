// Spendara Service Worker — basic cache-first for static assets only.
//
// Deliberately narrow scope: this exists to satisfy PWA installability/
// reliability requirements for app store packaging (PWABuilder), not to
// rebuild the app as offline-first. It never touches anything except a
// same-origin GET request under /static/ — every API call, auth route,
// Stripe webhook, and HTML page navigation goes straight to the network,
// completely untouched by this file.
//
// Served from the site root (/sw.js, via a dedicated Flask route — see
// app.py's service_worker()), not /static/sw.js, so its default scope is
// the whole site ('/') rather than being confined to /static/.

var CACHE_NAME = 'spendara-static-v1';

// Small, cheap assets only — logo, favicons, icons, the manifest itself.
// Deliberately excludes larger static files (the demo video, screenshots)
// so install doesn't get slow or wasteful; those can still be cached
// opportunistically by the fetch handler below if a page ever requests one.
var PRECACHE_URLS = [
  '/static/6000-logo.png',
  '/static/favicon.ico',
  '/static/favicon-96x96.png',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/manifest.json'
];

self.addEventListener('install', function (e) {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(PRECACHE_URLS);
    })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names
          .filter(function (name) { return name !== CACHE_NAME; })
          .map(function (name) { return caches.delete(name); })
      );
    })
  );
  clients.claim();
});

self.addEventListener('fetch', function (e) {
  var req = e.request;

  // Only ever intercept a same-origin GET under /static/. Everything else
  // (every page load, /api/*, /login, /settings/*, /stripe/webhook,
  // /admin/*, etc.) is left completely alone — no respondWith() at all,
  // so the browser handles it exactly as if this service worker didn't
  // exist. This is the explicit exclusion the caching strategy depends on:
  // a generic cache-first rule must never be allowed to catch a POST,
  // a webhook, or an API response.
  if (req.method !== 'GET') return;

  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf('/static/') !== 0) return;

  e.respondWith(
    caches.match(req).then(function (cached) {
      if (cached) return cached;
      return fetch(req).then(function (res) {
        if (res && res.status === 200) {
          var resClone = res.clone();
          caches.open(CACHE_NAME).then(function (cache) { cache.put(req, resClone); });
        }
        return res;
      });
    })
  );
});
