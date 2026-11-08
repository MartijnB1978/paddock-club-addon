/* Paddock Club service worker: shows chat notifications and opens the chat when one is tapped. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = {body: e.data ? e.data.text() : ""}; }
  var jobs = [self.registration.showNotification(d.title || "Paddock Club Reeuwijk", {
    body: d.body || "New chat message",
    icon: "/static/icon-192.png",
    badge: "/static/badge-96.png",
    tag: d.tag || "pcr-chat",          // one stacked notification per chat, like Teams
    renotify: true,                    // ...but still buzz for every new message
    timestamp: Date.now(),
    data: {url: d.url || "/chat"}
  })];
  if ("setAppBadge" in navigator && typeof d.unread === "number") {
    jobs.push((d.unread > 0 ? navigator.setAppBadge(d.unread) : navigator.clearAppBadge()).catch(function () {}));
  }
  e.waitUntil(Promise.all(jobs));
});

self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  var url = new URL((e.notification.data && e.notification.data.url) || "/chat", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (new URL(c.url).origin === self.location.origin && "focus" in c) {
        return (c.url === url ? Promise.resolve(c) : c.navigate(url)).then(function (w) { return (w || c).focus(); });
      }
    }
    return self.clients.openWindow(url);
  }));
});

/* the browser rotated the subscription: re-register it while the session cookie is still valid */
self.addEventListener("pushsubscriptionchange", function (e) {
  e.waitUntil(fetch("/push/key", {credentials: "same-origin"}).then(function (r) { return r.json(); })
    .then(function (k) {
      var raw = atob(k.key.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((k.key.length + 3) % 4));
      var key = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) key[i] = raw.charCodeAt(i);
      return self.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
    }).catch(function () {}));
});
