// Service worker mínimo del organizador: cachea el shell para que la página
// abierta en el móvil (PWA) arranque aunque la red tailnet taseje; los datos
// SIEMPRE van a la red (no se cachean: el plan vivo es lo único que importa).
const SHELL = ["", "/manifest.webmanifest", "/icon.svg"];
const CACHE = "plan-shell-v1";

self.addEventListener("install", (evento) => {
  evento.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(caches.keys().then((claves) => {
    return Promise.all(claves.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
  }));
  self.clients.claim();
});

self.addEventListener("fetch", (evento) => {
  const url = new URL(evento.request.url);
  if (evento.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  evento.respondWith(
    fetch(evento.request).then((respuesta) => respuesta).catch(() => caches.match(evento.request.url))
  );
});