// Service worker MÍNIMO do painel AGRIVIA Admin.
// Existe apenas para o navegador considerar o painel "instalável" (PWA).
// NÃO guarda nada em cache de propósito: o painel é administrativo e os
// dados precisam vir sempre frescos do servidor.
self.addEventListener('install', function () {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

// Handler de fetch presente (exigência de instalação), mas sem respondWith:
// todas as requisições seguem direto para a rede, como sempre.
self.addEventListener('fetch', function () {});
