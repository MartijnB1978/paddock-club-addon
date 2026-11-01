(function () {
  // countdowns
  function fmt(ms) {
    if (ms <= 0) return "closed";
    var s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60), sec = s % 60;
    if (d > 0) return "in " + d + "d " + h + "h";
    if (h > 0) return "in " + h + "h " + m + "m";
    return "in " + m + "m " + String(sec).padStart(2, "0") + "s";
  }
  function tick() {
    document.querySelectorAll("time[data-countdown]").forEach(function (t) {
      if (!t.dataset.label) t.dataset.label = t.textContent.trim();
      var ms = new Date(t.dataset.countdown) - new Date();
      t.textContent = t.dataset.label + " (" + fmt(ms) + ")";
    });
  }
  tick(); setInterval(tick, 1000);

  // session clock (Q1/Q2/Q3 time remaining), ticks locally between server updates
  function clockTick() {
    document.querySelectorAll("[data-remaining-until]").forEach(function (el) {
      var s = Math.max(0, Math.round((new Date(el.dataset.remainingUntil) - new Date()) / 1000));
      var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
      el.textContent = (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
    });
  }
  window.pcClockTick = clockTick;
  clockTick(); setInterval(clockTick, 1000);

  // colour the driver selects with the team colour
  document.querySelectorAll("select.dsel").forEach(function (s) {
    function paint() {
      var o = s.options[s.selectedIndex];
      s.style.setProperty("--sel", o && o.dataset.colour ? "#" + o.dataset.colour : "");
    }
    s.addEventListener("change", paint); paint();
  });

  // a driver can be picked only once within a top 10 / top 3 / championship list
  var groups = {};
  document.querySelectorAll("select.dsel").forEach(function (s) {
    var m = s.name.match(/^(race_pos|sprint_pos|season)_\d+$/);
    if (!m) return;
    (groups[m[1]] = groups[m[1]] || []).push(s);
  });
  Object.keys(groups).forEach(function (g) {
    var sels = groups[g];
    function sync() {
      var taken = {};
      sels.forEach(function (s) { if (s.value) taken[s.value] = s; });
      sels.forEach(function (s) {
        Array.prototype.forEach.call(s.options, function (o) {
          var blocked = o.value && taken[o.value] && taken[o.value] !== s;
          o.disabled = !!blocked;
          o.hidden = !!blocked;
        });
      });
    }
    sels.forEach(function (s) { s.addEventListener("change", sync); });
    sync();
  });

  // predictions: save every pick as soon as it is chosen (the save buttons stay as no-JS fallback)
  var queue = Promise.resolve();
  function badge(box) {
    var b = box.querySelector(".savestate");
    if (!b) {
      b = document.createElement("span");
      b.className = "savestate";
      b.setAttribute("role", "status");
      var anchorEl = box.querySelector(".lock") || box.querySelector("h2, legend");
      if (anchorEl) anchorEl.insertAdjacentElement("afterend", b); else box.prepend(b);
    }
    return b;
  }
  function show(b, text, kind) {
    b.textContent = text; b.className = "savestate " + kind;
    clearTimeout(b._t);
    if (kind === "ok") b._t = setTimeout(function () { b.textContent = ""; b.className = "savestate"; }, 2500);
  }
  document.querySelectorAll("form.predict[method=post]").forEach(function (form) {
    form.querySelectorAll(".js-hide").forEach(function (el) { el.hidden = true; });
    form.addEventListener("change", function (ev) {
      var sel = ev.target;
      if (!sel.name || sel.tagName !== "SELECT") return;
      var field = sel.name.replace(/_\d+$/, "");
      var fd = new FormData();
      ["csrf", "back"].forEach(function (n) { if (form.elements[n]) fd.append(n, form.elements[n].value); });
      form.querySelectorAll("select:not([disabled])").forEach(function (s) {
        if (s.name.replace(/_\d+$/, "") === field) fd.append(s.name, s.value);
      });
      var b = badge(sel.closest("fieldset") || form);
      show(b, "Saving…", "busy");
      queue = queue.then(function () {
        return fetch(form.action, {method: "POST", body: fd, headers: {"X-Autosave": "1"}, credentials: "same-origin"})
          .then(function (r) { return r.json(); })
          .then(function (j) { show(b, j.ok ? "✓ Saved" : j.msg, j.ok ? "ok" : "err"); })
          .catch(function () { show(b, "Not saved. Reload the page and try again.", "err"); });
      });
    });
  });

  // live F1 standings page: refresh tables on every change
  var f1 = document.getElementById("f1live");
  if (f1 && window.EventSource) {
    var f1first = true, f1busy = false;
    var es1 = new EventSource("/stream");
    es1.onmessage = function () {
      if (f1first) { f1first = false; return; }
      if (f1busy) return; f1busy = true;
      var sx = Array.prototype.map.call(f1.querySelectorAll(".grid-wrap"), function (g) { return g.scrollLeft; });
      fetch(f1.dataset.url, {cache: "no-store"}).then(function (r) { return r.text(); }).then(function (html) {
        f1.innerHTML = html; if (window.pcClockTick) window.pcClockTick();
        f1.querySelectorAll(".grid-wrap").forEach(function (g, i) { g.scrollLeft = sx[i] || 0; });
      }).finally(function () { f1busy = false; });
    };
  }

  // chat: poll for new messages, send text/photo/video with upload progress
  var chatBox = document.getElementById("chat");
  if (chatBox) {
    var log = document.getElementById("chat-log"), list = document.getElementById("chat-msgs");
    var form = document.getElementById("chat-form"), text = document.getElementById("chat-text");
    var file = document.getElementById("chat-file"), prev = document.getElementById("chat-preview");
    var errBox = document.getElementById("chat-err"), bar = document.getElementById("chat-progress");
    var last = Number(chatBox.dataset.last) || 0, first = Number(chatBox.dataset.first) || 0, csrf = chatBox.dataset.csrf;
    var since = chatBox.dataset.now, skew = new Date(chatBox.dataset.now) - new Date();   // server clock minus ours
    function serverNow() { return new Date(Date.now() + skew); }
    function expireEdits() {
      list.querySelectorAll(".edit").forEach(function (b) {
        if (new Date(b.dataset.until) <= serverNow() && !b.closest(".msg").classList.contains("editing")) b.remove();
      });
    }
    function swap(m) {
      var li = list.querySelector('[data-id="' + m.id + '"]');
      if (!li || li.classList.contains("editing")) return;
      if (m.deleted) { li.remove(); return; }
      var tmp = document.createElement("ol"); tmp.innerHTML = m.html;
      li.replaceWith(tmp.firstElementChild);
    }
    function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 120; }
    function toBottom() { log.scrollTop = log.scrollHeight; }
    function add(msgs, atTop) {
      if (!msgs.length) return;
      var empty = list.querySelector(".empty"); if (empty) empty.remove();
      var stick = nearBottom(), h = log.scrollHeight;
      msgs.forEach(function (m) {
        if (list.querySelector('[data-id="' + m.id + '"]')) return;
        var tmp = document.createElement("ol"); tmp.innerHTML = m.html;
        var li = tmp.firstElementChild;
        if (atTop) list.insertBefore(li, list.firstChild); else list.appendChild(li);
        if (!atTop) last = Math.max(last, m.id); else first = Math.min(first || m.id, m.id);
      });
      if (atTop) log.scrollTop += log.scrollHeight - h; else if (stick) toBottom();
    }
    function poll() {
      if (document.hidden) return;
      var upto = last;
      fetch("/chat/messages?after=" + last + "&since=" + encodeURIComponent(since || ""), {cache: "no-store", credentials: "same-origin"})
        .then(function (r) { return r.json(); }).then(function (d) {
          (d.changed || []).forEach(function (m) { if (m.id <= upto) swap(m); });
          add(d.messages || []);
          if (d.now) { since = d.now; skew = new Date(d.now) - new Date(); }
          expireEdits();
        }).catch(function () {});
    }
    function showErr(msg) { errBox.textContent = msg || ""; errBox.hidden = !msg; }
    function grow() { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, 140) + "px"; }
    toBottom();
    setInterval(poll, 4000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
    text.addEventListener("input", grow);
    text.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    file.addEventListener("change", function () {
      var f = file.files[0];
      if (!f) { prev.hidden = true; return; }
      var isVid = /^video\//.test(f.type), max = (isVid ? 50 : 10) * 1024 * 1024;
      if (f.size > max) { showErr((isVid ? "Videos" : "Photos") + " can be at most " + (isVid ? 50 : 10) + " MB."); file.value = ""; prev.hidden = true; return; }
      showErr("");
      document.getElementById("chat-fname").textContent = (isVid ? "🎬 " : "🖼️ ") + f.name + " (" + (f.size / 1048576).toFixed(1) + " MB)";
      prev.hidden = false;
    });
    document.getElementById("chat-unfile").addEventListener("click", function () { file.value = ""; prev.hidden = true; });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (!text.value.trim() && !file.files.length) return;
      var fd = new FormData(form); fd.append("csrf", csrf);
      var btn = document.getElementById("chat-send"); btn.disabled = true; showErr("");
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/chat/send");
      if (file.files.length) {
        bar.hidden = false;
        xhr.upload.onprogress = function (ev) { if (ev.lengthComputable) bar.firstElementChild.style.width = (ev.loaded / ev.total * 100) + "%"; };
      }
      xhr.onload = function () {
        var d = {}; try { d = JSON.parse(xhr.responseText); } catch (x) {}
        btn.disabled = false; bar.hidden = true; bar.firstElementChild.style.width = "0";
        if (xhr.status === 200 && d.ok) { form.reset(); prev.hidden = true; grow(); poll(); setTimeout(toBottom, 300); }
        else showErr(d.msg || "Sending failed, try again.");
      };
      xhr.onerror = function () { btn.disabled = false; bar.hidden = true; showErr("No connection, try again."); };
      xhr.send(fd);
    });
    setInterval(expireEdits, 5000);
    list.addEventListener("click", function (e) {
      var eb = e.target.closest(".edit");
      if (eb) {
        var li = eb.closest(".msg");
        if (li.classList.contains("editing")) return;
        li.classList.add("editing");
        var txt = li.querySelector(".txt");
        var ed = document.createElement("div"); ed.className = "editbox";
        ed.innerHTML = '<textarea rows="2" maxlength="2000" aria-label="Edit message"></textarea>' +
          '<div class="editbar"><span class="left"></span><button type="button" class="cancel">Cancel</button><button type="button" class="save btn">Save</button></div>';
        var ta = ed.querySelector("textarea"); ta.value = txt ? txt.dataset.raw : "";
        if (txt) txt.hidden = true;
        li.querySelector(".meta").after(ed); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
        var left = ed.querySelector(".left");
        var tick = setInterval(function () {
          var s = Math.max(0, Math.round((new Date(eb.dataset.until) - serverNow()) / 1000));
          left.textContent = s + " s left to edit";
          if (!s) { left.textContent = "Edit time is over"; ed.querySelector(".save").disabled = true; }
        }, 500);
        function close() { clearInterval(tick); ed.remove(); if (txt) txt.hidden = false; li.classList.remove("editing"); expireEdits(); }
        ed.querySelector(".cancel").addEventListener("click", close);
        function save() {
          var fd = new FormData(); fd.append("csrf", csrf); fd.append("text", ta.value);
          fetch("/chat/edit/" + eb.dataset.id, {method: "POST", body: fd, credentials: "same-origin"})
            .then(function (r) { return r.json(); }).then(function (d) {
              if (d.ok) { clearInterval(tick); li.classList.remove("editing"); swap(d.message); }
              else { left.textContent = d.msg || "Saving failed."; }
            });
        }
        ed.querySelector(".save").addEventListener("click", save);
        ta.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); save(); }
          if (ev.key === "Escape") close();
        });
        return;
      }
      var b = e.target.closest(".del"); if (!b || !confirm("Delete this message?")) return;
      var fd = new FormData(); fd.append("csrf", csrf);
      fetch("/chat/delete/" + b.dataset.id, {method: "POST", body: fd, credentials: "same-origin"})
        .then(function (r) { return r.json(); }).then(function (d) { if (d.ok) b.closest(".msg").remove(); });
    });
    var older = document.getElementById("chat-older");
    if (older) older.addEventListener("click", function () {
      fetch("/chat/messages?before=" + first, {cache: "no-store", credentials: "same-origin"})
        .then(function (r) { return r.json(); }).then(function (d) {
          add((d.messages || []).slice().reverse(), true);
          if (!d.more) older.remove();
        });
    });
  }

  // live standings page: refresh on every change, keep the track map element alive
  var livebox = document.getElementById("live");
  if (livebox && window.EventSource) {
    var lfirst = true, lbusy = false;
    var esl = new EventSource("/stream");
    esl.onmessage = function () {
      if (lfirst) { lfirst = false; return; }
      if (lbusy) return; lbusy = true;
      fetch(livebox.dataset.url, {cache: "no-store"}).then(function (r) { return r.text(); }).then(function (html) {
        if (map && map.parentNode) map.parentNode.removeChild(map);
        livebox.innerHTML = html; if (window.pcClockTick) window.pcClockTick(); mountMap();
      }).finally(function () { lbusy = false; });
    };
  }

  // live track map: one persistent element, moved into the slot after every board refresh
  var map = null, mapKey = "", mapTimer = null, unit = 120, NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    var el = document.createElementNS(NS, tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }
  function mountMap() {
    var slot = document.getElementById("trackmap-slot");
    if (!slot) {
      if (mapTimer) { clearInterval(mapTimer); mapTimer = null; }
      return;
    }
    if (!map) {
      map = document.createElement("div");
      map.className = "trackmap";
      map.innerHTML = '<svg role="img" aria-label="Live track map with every car\'s position"><g class="trk"></g><g class="cars"></g></svg><p class="note" hidden>The track outline fills in as the cars drive.</p>';
    }
    slot.replaceWith(map);
    if (!mapTimer) { pollMap(); mapTimer = setInterval(pollMap, 1500); }
  }
  function pollMap() {
    if (document.hidden || !map) return;
    fetch("/live/map?have=" + encodeURIComponent(mapKey), {cache: "no-store"})
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.live) return;
        var svg = map.querySelector("svg");
        if (d.track) {
          mapKey = d.key;
          svg.setAttribute("viewBox", d.track.viewBox);
          unit = Math.max(60, Number(d.track.viewBox.split(" ")[2]) / 85);
          var g = svg.querySelector(".trk");
          while (g.firstChild) g.removeChild(g.firstChild);
          if (d.track.path) {
            g.appendChild(svgEl("path", {d: d.track.path, "class": "road", "stroke-width": unit * 2.4}));
            g.appendChild(svgEl("path", {d: d.track.path, "class": "edge", "stroke-width": unit * 0.3}));
          } else {
            d.track.dots.forEach(function (p) { g.appendChild(svgEl("circle", {cx: p[0], cy: p[1], r: unit * 0.35, "class": "dot"})); });
          }
          map.querySelector(".note").hidden = !!d.track.path;
        }
        var cg = svg.querySelector(".cars"), seen = {};
        d.cars.forEach(function (c) {
          var el = cg.querySelector('[data-t="' + c.tla + '"]');
          var fresh = !el;
          if (fresh) {
            el = svgEl("g", {"class": "car"});
            el.setAttribute("data-t", c.tla);
            el.appendChild(svgEl("circle", {r: unit, "stroke-width": unit * 0.25}));
            var t = svgEl("text", {x: unit * 1.35, y: unit * 0.55, "font-size": unit * 1.7, "stroke-width": unit * 0.35});
            t.textContent = c.tla;
            el.appendChild(t);
            el.style.transition = "none";
          }
          el.querySelector("circle").setAttribute("fill", c.c);
          el.style.transform = "translate(" + c.x + "px," + c.y + "px)";
          cg.appendChild(el);                       // server order: leader last, drawn on top
          if (fresh) { el.getBoundingClientRect(); el.style.transition = ""; }
          seen[c.tla] = 1;
        });
        Array.prototype.slice.call(cg.children).forEach(function (el) {
          if (!seen[el.getAttribute("data-t")]) cg.removeChild(el);
        });
      }).catch(function () {});
  }
  mountMap();

  // live standings: server-sent events -> refresh board with FLIP animation
  var board = document.getElementById("board");
  if (!board || !window.EventSource) return;
  var year = board.dataset.year, busy = false, first = true;
  function positions() {
    var m = {};
    board.querySelectorAll(".row").forEach(function (r) { m[r.dataset.id] = r.getBoundingClientRect().top; });
    return m;
  }
  function refresh() {
    if (busy) return; busy = true;
    var before = positions();
    fetch("/fragment/board?year=" + year, {cache: "no-store"}).then(function (r) { return r.text(); })
      .then(function (html) {
        board.innerHTML = html; if (window.pcClockTick) window.pcClockTick(); mountMap();
        var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        board.querySelectorAll(".row").forEach(function (r) {
          var old = before[r.dataset.id];
          if (old === undefined || reduce) return;
          var dy = old - r.getBoundingClientRect().top;
          if (Math.abs(dy) < 2) return;
          r.animate([{transform: "translateY(" + dy + "px)"}, {transform: "none"}],
                    {duration: 700, easing: "cubic-bezier(.2,.8,.2,1)"});
          if (dy > 0) r.classList.add("flash-up");
        });
      }).finally(function () { busy = false; });
  }
  var es = new EventSource("/stream");
  es.onmessage = function () { if (first) { first = false; return; } refresh(); };
})();
