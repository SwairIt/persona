/*
 * Persona service worker — v0.94 feature 1/3
 *
 * Strategy:
 *  - /api/*       network-first (fall back to cached response if offline)
 *  - /static/*    cache-first   (CDN-style: ship once, reuse forever)
 *  - everything   network-first with cache fallback (so /timeline still
 *                 renders the last known HTML when the network drops)
 *
 * The install step warms the cache with the Tailwind + htmx + Alpine CDN
 * URLs and a handful of /static/* essentials referenced by base.html, so
 * the shell is usable on the very first offline visit.
 */

const CACHE_VERSION = 'persona-v1.71';
const PRECACHE_URLS = [
  '/static/manifest.json',
  '/static/icon-512.png',
  '/static/css/app.css',
  '/static/compact_mode.css',
  '/static/grayscale.css',
  '/static/reduce_motion.css',
  '/static/command_palette.css',
  '/static/keyboard_shortcuts.css',
  '/static/query_help.css',
  '/static/context_menu.css',
  '/static/cal_nav.css',
  '/static/bulk_select.css',
  '/static/command_palette.js',
  '/static/keyboard_shortcuts.js',
  '/static/query_help.js',
  '/static/context_menu.js',
  '/static/cal_nav.js',
  '/static/live_status.js',
  '/static/drag_to_tag.js',
  '/static/bulk_select.js',
  '/static/js/keyboard.js',
  '/static/js/palette.js',
  '/static/js/notes.js',
  'https://cdn.tailwindcss.com',
  'https://unpkg.com/htmx.org@2.0.4',
  'https://unpkg.com/alpinejs@3.14.7/dist/cdn.min.js',
  'https://cdn.jsdelivr.net/npm/markdown-it@14.1.0/dist/markdown-it.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => {
      // Use addAll-with-individual-catch so a single 404 doesn't poison the
      // whole precache step. CDN URLs in particular may be opaque/cors-fail
      // and we still want the install to succeed.
      return Promise.all(
        PRECACHE_URLS.map((url) =>
          cache.add(new Request(url, { mode: 'no-cors' })).catch(() => null)
        )
      );
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

function isApiRequest(url) {
  return url.pathname.startsWith('/api/');
}

function isStaticRequest(url) {
  return url.pathname.startsWith('/static/');
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const response = await fetch(request);
    // Only cache successful, basic (same-origin) GETs to avoid filling the
    // cache with redirects/errors.
    if (request.method === 'GET' && response && response.status === 200) {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (request.method === 'GET' && response && (response.status === 200 || response.type === 'opaque')) {
    cache.put(request, response.clone()).catch(() => {});
  }
  return response;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  let url;
  try {
    url = new URL(request.url);
  } catch (e) {
    return;
  }

  // Don't intercept websocket/EventSource upgrades or chrome-extension://
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;

  if (isApiRequest(url)) {
    event.respondWith(networkFirst(request));
    return;
  }

  if (isStaticRequest(url) || url.origin !== self.location.origin) {
    // /static/* and cross-origin CDNs (tailwind/htmx/alpine/markdown-it):
    // cache-first is fine because they're versioned URLs.
    event.respondWith(cacheFirst(request));
    return;
  }

  // Page navigations and everything else: network-first with cache fallback
  // so the timeline stays viewable when the user is offline.
  event.respondWith(networkFirst(request));
});
