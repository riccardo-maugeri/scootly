// Tiene l'app disponibile anche senza rete.
// - pagine e icone: prima prova la rete, se manca usa la copia salvata
// - mappa (data/strade.json, ~3 MB) e sfondo (data/sfondo.json): usa subito la copia salvata e intanto
//   scarica quella nuova, che verra' usata alla prossima apertura
const CACHE = 'scooter-v33';
const MAPS = ['data/strade.json', 'data/sfondo.json'];
const FILES = ['./', 'index.html', ...MAPS, 'manifest.webmanifest', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  // scarica e salva; la promessa finisce solo quando il file e' davvero salvato
  // (su iPhone il service worker puo' essere fermato appena finisce il lavoro "in attesa")
  const fresh = () => fetch(e.request).then(r => {
    if (!r.ok) return r;
    const copy = r.clone();
    return caches.open(CACHE).then(c => c.put(e.request, copy)).then(() => r, () => r);
  });
  if (MAPS.some(m => url.pathname.endsWith('/' + m))) {
    e.respondWith(caches.match(e.request).then(hit => { const net = fresh(); if (hit) { e.waitUntil(net.catch(() => {})); return hit; } return net; }));
    return;
  }
  e.respondWith(fresh().catch(() => caches.match(e.request).then(r => r || caches.match('index.html'))));
});
