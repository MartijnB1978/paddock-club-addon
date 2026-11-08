/* Chat notifications: register the service worker, keep this device's subscription in sync,
   and drive the "Turn on notifications" control ([data-push] blocks on the Chat and Account pages). */
(function () {
  var csrf = document.body.dataset.csrf;
  var supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var installed = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  var reg = null, key = null;

  function b64(s) {
    var raw = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function post(url, data) {
    var fd = new FormData(); fd.append("csrf", csrf);
    Object.keys(data || {}).forEach(function (k) { fd.append(k, data[k]); });
    return fetch(url, {method: "POST", body: fd, credentials: "same-origin"}).then(function (r) { return r.json(); });
  }
  function getKey() {
    return key ? Promise.resolve(key) : fetch("/push/key", {credentials: "same-origin"})
      .then(function (r) { return r.json(); }).then(function (d) { key = d.key; return key; });
  }
  function current() { return reg ? reg.pushManager.getSubscription() : Promise.resolve(null); }

  function render() {
    var boxes = document.querySelectorAll("[data-push]");
    if (!boxes.length) return;
    current().then(function (sub) {
      var html;
      if (!supported) {
        html = ios && !installed
          ? '<span class="push-note">📲 For notifications on iPhone/iPad: tap <b>Share</b> → <b>Add to Home Screen</b>, open Paddock Club from your home screen and come back here.</span>'
          : '<span class="push-note">This browser can\'t show notifications.</span>';
      } else if (Notification.permission === "denied") {
        html = '<span class="push-note">🔕 Notifications are blocked for this site. Allow them in your browser or phone settings, then reload.</span>';
      } else if (sub && Notification.permission === "granted") {
        html = '<span class="push-on">🔔 Notifications on for this device</span>' +
               '<button type="button" class="linkbtn" data-act="test">Send test</button>' +
               '<button type="button" class="linkbtn" data-act="off">Turn off</button>';
      } else {
        html = '<button type="button" class="btn small" data-act="on">🔔 Turn on notifications</button>' +
               '<span class="push-note">Get a pop-up for new chat messages, like Teams.</span>';
      }
      boxes.forEach(function (b) { b.innerHTML = html + '<span class="push-msg" role="status"></span>'; b.hidden = false; });
    });
  }
  function say(msg) { document.querySelectorAll("[data-push] .push-msg").forEach(function (s) { s.textContent = msg || ""; }); }

  function turnOn() {
    return Notification.requestPermission().then(function (p) {
      if (p !== "granted") { render(); throw new Error("Permission not given."); }
      return getKey();
    }).then(function (k) {
      return reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64(k)});
    }).then(function (sub) {
      return post("/push/subscribe", {sub: JSON.stringify(sub)});
    }).then(function (d) { render(); if (!d.ok) say(d.msg); })
      .catch(function (e) { say(e.message || "Couldn't turn notifications on."); });
  }
  function turnOff() {
    return current().then(function (sub) {
      if (!sub) return;
      return post("/push/unsubscribe", {endpoint: sub.endpoint}).then(function () { return sub.unsubscribe(); });
    }).then(render);
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-push] [data-act]");
    if (!b) return;
    b.disabled = true;
    var act = b.dataset.act;
    (act === "on" ? turnOn() : act === "off" ? turnOff()
      : post("/push/test").then(function (d) { say(d.msg); })).finally(function () { b.disabled = false; });
  });

  if (!supported) { render(); return; }
  navigator.serviceWorker.register("/sw.js", {scope: "/"}).then(function (r) {
    reg = r;
    return navigator.serviceWorker.ready;
  }).then(function () {
    render();
    // re-send an existing subscription: repairs rotated keys and moves it to whoever is signed in now
    var done = false; try { done = sessionStorage.getItem("pcr-push-sync") === csrf; } catch (x) {}
    if (!done) current().then(function (sub) {
      if (sub && Notification.permission === "granted") post("/push/subscribe", {sub: JSON.stringify(sub)}).then(function () {
        try { sessionStorage.setItem("pcr-push-sync", csrf); } catch (x) {}
      }).catch(function () {});
    });
    if (location.pathname.indexOf("/chat") === 0) {           // reading the chat clears the pop-ups and app badge
      reg.getNotifications({tag: "pcr-chat"}).then(function (ns) { ns.forEach(function (n) { n.close(); }); });
      if ("clearAppBadge" in navigator) navigator.clearAppBadge().catch(function () {});
    }
  }).catch(function () { render(); });
})();
