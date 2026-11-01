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
