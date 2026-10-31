/* Tacitus web chat: the service worker. It keeps a copy of the app's
 * static files so the installed app opens without a network, and fetches
 * them fresh whenever it can. API requests (every path with /api/ in it:
 * messages, files, the event stream, pairing) are never answered from or
 * written to the cache; the worker leaves them to the browser. */
'use strict';

const CACHE = 'tacitus-webchat-v1';
const SHELL = ['./', 'app.js', 'app.css', 'manifest.webmanifest', 'favicon.svg', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE)
    .then((cache) => cache.addAll(SHELL))
    .catch(() => {})
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  const scope = new URL(self.registration.scope);
  if (req.method !== 'GET' || url.origin !== scope.origin) return;
  if (url.pathname.includes('/api/')) return;
  if (!url.pathname.startsWith(scope.pathname)) return;
  event.respondWith(fetch(req)
    .then((res) => {
      if (res.ok && res.type === 'basic') {
        const copy = res.clone();
        caches.open(CACHE).then((cache) => cache.put(req, copy));
      }
      return res;
    })
    .catch(() => caches.match(req, { ignoreSearch: true })
      .then((hit) => hit || caches.match(new URL('./', scope).href))
      .then((hit) => hit || Response.error())));
});
