/* Private Chat service worker
 * - Precaches the offline page and core static assets.
 * - Never caches HTML pages, API calls, sockets or user uploads (private content).
 * - If a page navigation fails because the network is down, shows /offline.
 * Bump VERSION to force clients to refresh their caches.
 */
const VERSION = "v1";
const CACHE = "private-chat-" + VERSION;
const OFFLINE_URL = "/offline";
const PRECACHE = [
  OFFLINE_URL,
  "/manifest.webmanifest",
  "/static/css/main.css",
  "/static/js/main.js",
  "/static/img/logo.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function bypass(url) {
  return (
    url.pathname.startsWith("/socket.io") ||
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/static/uploads/")
  );
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin || bypass(url)) return;

  // Page loads: always go to the network; only fall back to the offline page.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  // Own static assets: serve from cache instantly, refresh in the background.
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(req).then((cached) => {
          const network = fetch(req)
            .then((res) => {
              if (res && res.ok) cache.put(req, res.clone());
              return res;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
  }
});
