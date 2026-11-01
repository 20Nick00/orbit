/* Never cache financial data, responses to mutations, or HTMX fragments. */
const CACHE_NAME = 'orbit-shell-v1';
const SHELL_PATHS = [
  '/static/orbit.css',
  '/static/paycheck.css',
  '/static/charts.js',
  '/static/paycheck.js',
  '/static/trends.js',
  '/static/pwa.js',
  '/static/vendor/htmx.min.js',
  '/static/vendor/fonts/manrope-latin-wght-normal.woff2',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/offline.html'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_PATHS)));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(names => Promise.all(
    names.filter(name => name.startsWith('orbit-shell-') && name !== CACHE_NAME)
      .map(name => caches.delete(name))
  )).then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  // Even a navigation to a static URL stays network-only.
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() =>
      caches.open(CACHE_NAME).then(cache => cache.match('/static/offline.html'))
    ));
    return;
  }
  if (url.search || !SHELL_PATHS.includes(url.pathname)) return;
  // Network first so an update is never hidden; the installed copy is only for offline use.
  event.respondWith(fetch(request).catch(() =>
    caches.open(CACHE_NAME)
      .then(cache => cache.match(url.pathname))
      .then(cached => cached || Response.error())
  ));
});
