const C='sage-v4',A=['/login','/manifest.json','/icon-192.png','/icon-512.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(A)));self.skipWaiting()});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==C).map(x=>caches.delete(x)))));self.clients.claim()});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.pathname==='/api')return;
 e.respondWith(fetch(e.request).catch(()=>caches.match(e.request).then(r=>r||new Response('<body style="background:#0a0f0d;color:#39ff88;font-family:monospace;padding:2em">Sage Terminal offline. Reconnect to see live paper trades.</body>',{headers:{'Content-Type':'text/html'}}))))});
