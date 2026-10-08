// share-it service worker.
//
// Two jobs only. It makes the page installable as an app, and it catches the
// Android share sheet ("Share to share-it"). Everything else goes straight to
// the network: share-it is useless without its server, so there is nothing
// worth caching for offline use.
//
// A share arrives as a POST to /share-target. We park the shared files and
// text in a cache, then send the page to /?share-target=1. The page picks them
// up from the cache and runs them through the normal upload flow, so a shared
// file gets the same progress bar, history entry and "share" checkbox as one
// dropped on the page.

const INBOX = 'share-inbox';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === 'POST' && url.pathname === '/share-target') {
    event.respondWith(parkShare(event.request));
  }
});

async function parkShare(request) {
  // Keep an unread copy: if parking fails, the server gets the share instead.
  const spare = request.clone();
  try {
    const form = await request.formData();
    const cache = await caches.open(INBOX);
    // A new share replaces anything an earlier one left behind.
    for (const key of await cache.keys()) await cache.delete(key);

    const files = form.getAll('files').filter((f) => f && typeof f !== 'string');
    const meta = {
      title: String(form.get('title') || ''),
      text: String(form.get('text') || ''),
      url: String(form.get('url') || ''),
      files: [],
    };
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      const key = `/share-inbox/${i}`;
      await cache.put(key, new Response(f, { headers: { 'Content-Type': f.type || 'application/octet-stream' } }));
      meta.files.push({ key, name: f.name || `shared-${i}`, type: f.type || '' });
    }
    await cache.put('/share-inbox/meta', new Response(JSON.stringify(meta), {
      headers: { 'Content-Type': 'application/json' },
    }));
    return Response.redirect('/?share-target=1', 303);
  } catch (e) {
    // The server can save the files itself. See /share-target in main.py.
    return fetch(spare);
  }
}
