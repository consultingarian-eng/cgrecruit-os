/*
 * CGRecruit service worker — intentionally cache-free.
 *
 * Its only job is to make the app an installable PWA (standalone, home-screen).
 * It does NOT cache HTML/JS/CSS: every request goes straight to the network, so
 * a Railway deploy is picked up on the next load with zero stale-bundle risk.
 * If offline support is ever wanted, add a caching strategy here deliberately.
 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {
    // No respondWith() → browser performs its default network fetch. Pass-through.
});
