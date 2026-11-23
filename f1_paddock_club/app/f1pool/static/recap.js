// Race recap on the race page: tyre strategy per driver and lap times, from F1's timing archive.
(function () {
  "use strict";
  var box = document.getElementById("recap");
  if (!box) return;
  var msg = box.querySelector(".rc-msg"), body = box.querySelector(".rc-body");
  var tyreEl = box.querySelector(".rc-tyres"), chipsEl = box.querySelector(".rc-chips");
  var lapEl = box.querySelector(".rc-laps"), read = box.querySelector(".rc-read");
  var cache = {}, cur = null, want = null, timer = null, picked = {}, lapLayout = null;
  var NS = "http://www.w3.org/2000/svg";
  var COMP = {S: ["Soft", "#e8002d", "#fff"], M: ["Medium", "#ffd12e", "#141821"], H: ["Hard", "#eceae4", "#141821"],
              I: ["Intermediate", "#39b54a", "#fff"], W: ["Wet", "#2b7bd0", "#fff"], "?": ["Unknown", "#8d97aa", "#141821"]};
  var BAND = {sc: ["Safety Car", "rgba(255,190,60,.16)"], vsc: ["Virtual Safety Car", "rgba(255,190,60,.08)"],
              red: ["Red flag", "rgba(232,0,45,.18)"]};

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function el(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function fmt(s) {
    if (s === null || s === undefined) return "–";
    var m = Math.floor(s / 60);
    return m + ":" + (s - m * 60).toFixed(3).padStart(6, "0");
  }

  function load(sid) {
    want = sid;
    clearTimeout(timer);
    if (cache[sid]) { show(cache[sid]); return; }
    body.hidden = true; msg.hidden = false;
    msg.textContent = "Loading the race recap…";
    fetch("/recap/" + sid, {credentials: "same-origin"}).then(function (r) { return r.json(); }).then(function (d) {
      if (want !== sid) return;
      if (d.status === "building") {
        msg.textContent = "Building the recap from F1's timing archive, this takes up to a minute…";
        timer = setTimeout(function () { load(sid); }, 5000);
      } else if (d.status) {
        msg.textContent = d.msg || "No race recap for this session.";
      } else {
        cache[sid] = d;
        show(d);
      }
    }).catch(function () { if (want === sid) msg.textContent = "The recap could not be loaded. Try again later."; });
  }

  function show(d) {
    cur = d;
    msg.hidden = true; body.hidden = false;
    picked = {};
    d.drivers.filter(function (x) { return x.laps; }).slice(0, 3).forEach(function (x) { picked[x.num] = true; });
    drawTyres();
    drawChips();
    drawLaps();
  }

  // ------------------------------------------------------------ tyre strategy
  function bands(svg, X, top, bottom) {
    var s = cur.track, i = 0;
    while (i < s.length) {
      if (BAND[s[i]]) {
        var j = i;
        while (j + 1 < s.length && s[j + 1] === s[i]) j++;
        var r = el("rect", {x: X(i), y: top, width: X(j + 1) - X(i), height: bottom - top, fill: BAND[s[i]][1]}, svg);
        el("title", {}, r).textContent = BAND[s[i]][0] + ": lap " + (i + 1) + (j > i ? "–" + (j + 1) : "");
        i = j + 1;
      } else i++;
    }
  }

  function drawTyres() {
    tyreEl.innerHTML = "";
    var W = Math.max(320, tyreEl.clientWidth || 600), LW = 64, RW = 34, ROW = 22, TOP = 6;
    var rows = cur.drivers, H = TOP + rows.length * ROW + 24, plotW = W - LW - RW, T = cur.total;
    var svg = el("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, class: "rc-plot", role: "img",
      "aria-label": "Tyre strategy per driver"}, tyreEl);
    var defs = el("defs", {}, svg);
    var pat = el("pattern", {id: "rc-used", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)"}, defs);
    el("rect", {width: 3, height: 6, fill: "rgba(20,24,33,.38)"}, pat);
    var X = function (lap) { return LW + (lap / T) * plotW; };
    bands(svg, X, TOP - 2, TOP + rows.length * ROW);
    rows.forEach(function (d, r) {
      var y = TOP + r * ROW;
      var lbl = el("text", {x: LW - 8, y: y + ROW / 2 + 4, class: "rc-lbl end"}, svg);
      lbl.textContent = d.pos + "  " + d.tla;
      d.stints.forEach(function (s) {
        var c = COMP[s.c] || COMP["?"], x0 = X(s.from - 1) + 1, x1 = X(s.to) - 1, w = Math.max(2, x1 - x0);
        var g = el("g", {class: "rc-stint"}, svg);
        el("rect", {x: x0, y: y + 3, width: w, height: ROW - 6, rx: 4, fill: c[1]}, g);
        if (!s.new) el("rect", {x: x0, y: y + 3, width: w, height: ROW - 6, rx: 4, fill: "url(#rc-used)"}, g);
        if (w > 18) {
          var t = el("text", {x: x0 + w / 2, y: y + ROW / 2 + 4, class: "rc-in", style: "fill:" + c[2]}, g);
          t.textContent = s.c;
        }
        el("title", {}, g).textContent = d.tla + ": " + c[0] + (s.new ? " (new set)" : " (used set)") +
          ", laps " + s.from + "–" + s.to + " (" + (s.to - s.from + 1) + " laps)";
      });
      if (!d.laps || d.out) {   // 0 laps: out before finishing lap 1 (or did not start)
        el("text", {x: (d.laps ? X(d.laps) : LW) + 6, y: y + ROW / 2 + 4, class: "rc-lbl"}, svg).textContent = d.laps ? "DNF" : "Out";
      }
    });
    var step = T > 40 ? 10 : 5;
    for (var l = step; l <= T; l += step) {
      el("text", {x: X(l), y: H - 6, class: "rc-tick mid"}, svg).textContent = l;
    }
    el("text", {x: LW - 8, y: H - 6, class: "rc-tick end"}, svg).textContent = "Lap";
  }

  // ------------------------------------------------------------ lap times
  function drawChips() {
    chipsEl.innerHTML = "";
    cur.drivers.forEach(function (d) {
      if (!d.laps) return;
      var b = document.createElement("button");
      b.type = "button";
      b.className = "rc-chip" + (picked[d.num] ? " on" : "") + (d.dash ? " dash" : "");
      b.setAttribute("aria-pressed", picked[d.num] ? "true" : "false");
      b.style.setProperty("--tc", d.c);
      b.innerHTML = "<i></i>" + esc(d.tla);
      b.title = d.name || d.tla;
      b.addEventListener("click", function () {
        picked[d.num] = !picked[d.num];
        drawChips(); drawLaps();
      });
      chipsEl.appendChild(b);
    });
  }

  function slowLaps(d) {
    // in-laps, out-laps and the first lap: off the scale, left out of the line
    var skip = {1: true};
    (d.pits || []).forEach(function (p) { skip[p] = true; skip[p + 1] = true; });
    return skip;
  }

  function drawLaps() {
    lapEl.innerHTML = "";
    var sel = cur.drivers.filter(function (d) { return picked[d.num] && d.laps; });
    if (!sel.length) {
      lapEl.innerHTML = '<p class="muted small">Pick one or more drivers above.</p>';
      read.innerHTML = ""; lapLayout = null;
      return;
    }
    var W = Math.max(320, lapEl.clientWidth || 600), ML = 52, MR = 10, TOP = 10, PH = 240, T = cur.total;
    var H = TOP + PH + 26, plotW = W - ML - MR;
    var vals = [];
    sel.forEach(function (d) {
      var skip = slowLaps(d);
      d.times.forEach(function (v, i) { if (v && !skip[i + 1] && !BAND[cur.track[i]]) vals.push(v); });
    });
    if (!vals.length) sel.forEach(function (d) { d.times.forEach(function (v) { if (v) vals.push(v); }); });
    vals.sort(function (a, b) { return a - b; });
    var lo = vals[0], hi = vals[Math.floor(vals.length * 0.95)] || vals[vals.length - 1];
    var pad = Math.max(0.3, (hi - lo) * 0.12);
    lo = Math.floor((lo - pad) * 2) / 2; hi = Math.ceil((hi + pad) * 2) / 2;
    var svg = el("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, class: "rc-plot", role: "img",
      "aria-label": "Lap times of the chosen drivers"}, lapEl);
    var X = function (lap) { return ML + ((lap - 1) / Math.max(1, T - 1)) * plotW; };
    var Xb = function (i) { return ML + ((i - 0.5) / Math.max(1, T - 1)) * plotW; };
    var Y = function (v) { return TOP + PH - ((v - lo) / (hi - lo)) * PH; };
    bands(svg, function (i) { return Math.max(ML, Math.min(W - MR, Xb(i))); }, TOP, TOP + PH);
    var span = hi - lo, stepS = span > 8 ? 2 : span > 4 ? 1 : 0.5;
    for (var t = Math.ceil(lo / stepS) * stepS; t <= hi + 1e-9; t += stepS) {
      el("line", {x1: ML, x2: W - MR, y1: Y(t), y2: Y(t), class: "rc-gl"}, svg);
      el("text", {x: ML - 4, y: Y(t) + 4, class: "rc-tick end"}, svg).textContent = fmt(t).replace(/0+$/, "").replace(/\.$/, "");
    }
    var step = T > 40 ? 10 : 5;
    for (var l = step; l <= T; l += step) el("text", {x: X(l), y: H - 6, class: "rc-tick mid"}, svg).textContent = l;
    el("text", {x: ML - 4, y: H - 6, class: "rc-tick end"}, svg).textContent = "Lap";
    sel.forEach(function (d) {
      var skip = slowLaps(d), dPath = "", pen = false;
      d.times.forEach(function (v, i) {
        var ok = v && !skip[i + 1] && v <= hi && v >= lo;
        if (ok) { dPath += (pen ? " L" : " M") + X(i + 1).toFixed(1) + " " + Y(v).toFixed(1); pen = true; }
        else pen = false;
      });
      var p = el("path", {d: dPath, class: "rc-line", stroke: d.c}, svg);
      if (d.dash) p.setAttribute("stroke-dasharray", "6 4");
      (d.pits || []).forEach(function (lap) {
        var m = el("path", {d: "M" + X(lap) + " " + (TOP + PH) + " l-5 8 h10 z", fill: d.c, class: "rc-pit"}, svg);
        el("title", {}, m).textContent = d.tla + " pitted at the end of lap " + lap;
      });
    });
    var cursor = el("line", {y1: TOP, y2: TOP + PH, class: "rc-cursor", visibility: "hidden"}, svg);
    var dots = sel.map(function (d) { return el("circle", {r: 4, fill: d.c, class: "rc-dot", visibility: "hidden"}, svg); });
    var hit = el("rect", {x: ML, y: 0, width: plotW, height: H, fill: "transparent"}, svg);
    lapLayout = {X: X, Y: Y, sel: sel, cursor: cursor, dots: dots, lo: lo, hi: hi};
    function move(e) {
      var r = svg.getBoundingClientRect(), x = (e.clientX - r.left) * (W / r.width);
      hover(Math.round(Math.max(0, Math.min(1, (x - ML) / plotW)) * (T - 1)) + 1);
    }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", function () { hover(null); });
    hover(null);
  }

  function hover(lap) {
    var L = lapLayout;
    if (!L) return;
    if (!lap) {
      L.cursor.setAttribute("visibility", "hidden");
      L.dots.forEach(function (d) { d.setAttribute("visibility", "hidden"); });
      read.innerHTML = '<span class="muted">Move over the chart to read every lap. Triangles: pit stops.</span>';
      return;
    }
    var x = L.X(lap), st = cur.track[lap - 1];
    L.cursor.setAttribute("x1", x); L.cursor.setAttribute("x2", x); L.cursor.setAttribute("visibility", "visible");
    var out = '<span class="muted">Lap ' + lap + (BAND[st] ? " · " + BAND[st][0] : "") + "</span>";
    L.sel.forEach(function (d, k) {
      var v = d.times[lap - 1], pit = (d.pits || []).indexOf(lap) >= 0 ? " in-lap" : (d.pits || []).indexOf(lap - 1) >= 0 ? " out-lap" : "";
      out += '<span class="rc-drv' + (d.dash ? " dash" : "") + '"><i style="--tc:' + esc(d.c) + '"></i><b>' + esc(d.tla) + "</b> " + fmt(v) + pit + "</span>";
      var dot = L.dots[k];
      if (v && v >= L.lo && v <= L.hi) { dot.setAttribute("cx", x); dot.setAttribute("cy", L.Y(v)); dot.setAttribute("visibility", "visible"); }
      else dot.setAttribute("visibility", "hidden");
    });
    read.innerHTML = out;
  }

  box.querySelector(".rc-tabs").addEventListener("click", function (e) {
    var bt = e.target.closest("button[data-sid]");
    if (!bt) return;
    box.querySelectorAll(".rc-tabs button").forEach(function (x) {
      x.classList.toggle("on", x === bt); x.setAttribute("aria-pressed", x === bt ? "true" : "false");
    });
    load(bt.dataset.sid);
  });
  var rt = null;
  window.addEventListener("resize", function () {
    clearTimeout(rt);
    rt = setTimeout(function () { if (cur && !body.hidden) { drawTyres(); drawLaps(); } }, 150);
  });
  var first = box.querySelector(".rc-tabs button.on");
  if (first) load(first.dataset.sid);
})();
