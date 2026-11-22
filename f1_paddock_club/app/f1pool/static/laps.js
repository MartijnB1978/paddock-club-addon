// Lap comparison on the race page: pole lap / fastest lap of two drivers from F1 telemetry.
(function () {
  "use strict";
  var box = document.getElementById("laps");
  if (!box) return;
  var msg = box.querySelector(".lc-msg"), body = box.querySelector(".lc-body");
  var who = box.querySelector(".lc-who"), mapEl = box.querySelector(".lc-map");
  var svgEl = box.querySelector(".lc-svg"), read = box.querySelector(".lc-read");
  var cache = {}, cur = null, want = null, timer = null, hoverI = null;
  var NS = "http://www.w3.org/2000/svg";
  var PANELS = [
    {k: "delta", label: "Gap", h: 96},
    {k: "speed", label: "Speed km/h", h: 170},
    {k: "throttle", label: "Throttle %", h: 74},
    {k: "brake", label: "Brake", h: 36},
    {k: "gear", label: "Gear", h: 74}
  ];
  var ML = 40, MR = 10, GAP = 30, TOP = 34;

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function sign(v) { return (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(3); }

  function load(sid) {
    want = sid;
    clearTimeout(timer);
    if (cache[sid]) { show(cache[sid]); return; }
    body.hidden = true;
    msg.hidden = false;
    msg.textContent = "Loading telemetry…";
    fetch("/laps/" + sid, {credentials: "same-origin"}).then(function (r) { return r.json(); }).then(function (d) {
      if (want !== sid) return;
      if (d.status === "building") {
        msg.textContent = "Building the comparison from F1's telemetry, this takes up to a minute…";
        timer = setTimeout(function () { load(sid); }, 5000);
      } else if (d.status) {
        msg.textContent = d.msg || "No lap comparison for this session.";
      } else {
        cache[sid] = d;
        show(d);
      }
    }).catch(function () {
      if (want === sid) msg.textContent = "Telemetry could not be loaded. Try again later.";
    });
  }

  function show(d) {
    cur = d;
    msg.hidden = true;
    body.hidden = false;
    var a = d.drivers[0], b = d.drivers[1];
    who.innerHTML =
      '<span class="lc-drv"><i style="--tc:' + esc(a.c) + '"></i><b>' + esc(a.tla) + "</b> " + esc(a.lap) + "</span>" +
      '<span class="lc-drv' + (b.same ? " dash" : "") + '"><i style="--tc:' + esc(b.c) + '"></i><b>' + esc(b.tla) + "</b> " + esc(b.lap) +
      ' <span class="muted">(' + sign(d.gap) + " s)</span></span>" +
      '<span class="muted small">' + esc(d.label) + ", " + (d.length / 1000).toFixed(2) + " km lap</span>";
    drawMap();
    drawCharts();
  }

  function el(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  var mapDot = null;
  function drawMap() {
    mapEl.innerHTML = "";
    mapDot = null;
    var m = cur.map;
    if (!m) { mapEl.hidden = true; return; }
    mapEl.hidden = false;
    var vb = m.viewBox.split(" ").map(Number), unit = Math.max(vb[2], vb[3]);
    var svg = el("svg", {viewBox: m.viewBox, role: "img", "aria-label": "Track map: where each driver was quicker"}, mapEl);
    var cols = [cur.drivers[0].c, cur.drivers[1].c];
    m.segs.forEach(function (s) {
      el("path", {d: s.p, fill: "none", stroke: "#141821", "stroke-width": unit * 0.03, "stroke-linecap": "round"}, svg);
    });
    m.segs.forEach(function (s) {
      var p = el("path", {d: s.p, fill: "none", stroke: cols[s.w], "stroke-width": unit * 0.016, "stroke-linecap": "round"}, svg);
      if (s.w === 1 && cur.drivers[1].same) p.setAttribute("stroke-dasharray", unit * 0.02 + " " + unit * 0.012);
      var t = el("title", {}, p);
      t.textContent = cur.drivers[s.w].tla + " quicker here by " + s.g.toFixed(3) + " s";
    });
    (cur.corners || []).forEach(function (c) {
      var t = el("text", {x: c.x, y: c.y - unit * 0.025, "font-size": unit * 0.032, class: "lc-cn"}, svg);
      t.textContent = c.n;
    });
    var s0 = m.xy[0];
    el("circle", {cx: s0[0], cy: s0[1], r: unit * 0.012, class: "lc-start"}, svg);
    mapDot = el("circle", {r: unit * 0.018, class: "lc-dot", visibility: "hidden"}, svg);
    var foot = document.createElement("p");
    foot.className = "muted small lc-mapkey";
    foot.innerHTML = "Colour: who was quicker in each part of the lap. Dot: start/finish.";
    mapEl.appendChild(foot);
    svg.addEventListener("pointermove", function (e) {
      var pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
      var p = pt.matrixTransform(svg.getScreenCTM().inverse()), best = 0, bd = Infinity;
      m.xy.forEach(function (q, i) { var dd = (q[0] - p.x) * (q[0] - p.x) + (q[1] - p.y) * (q[1] - p.y); if (dd < bd) { bd = dd; best = i; } });
      if (bd < unit * unit * 0.004) hover(Math.min(best * 2, cur.delta.length - 1)); else hover(null);
    });
    svg.addEventListener("pointerleave", function () { hover(null); });
  }

  var layout = null, cursor = null;
  function drawCharts() {
    svgEl.innerHTML = "";
    var W = Math.max(300, svgEl.clientWidth || 600), N = cur.delta.length, plotW = W - ML - MR;
    var H = TOP + PANELS.reduce(function (s, p) { return s + p.h + GAP; }, 0) - GAP + 22;
    var svg = el("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, class: "lc-plot", role: "img",
      "aria-label": "Speed, throttle, brake, gear and time gap along the lap"}, svgEl);
    var X = function (i) { return ML + (i / (N - 1)) * plotW; };
    var a = cur.drivers[0], b = cur.drivers[1], cols = [a.c, b.c];
    var y0 = TOP, boxes = [];
    // corner guides across all panels
    var guides = el("g", {class: "lc-guides"}, svg);
    PANELS.forEach(function (p) {
      var top = y0, h = p.h;
      boxes.push({top: top, h: h});
      el("text", {x: ML, y: top - 7, class: "lc-lbl"}, svg).textContent = p.label;
      if (p.k === "delta") {
        var dv = cur.delta, lo = Math.min.apply(null, dv.concat([0])), hi = Math.max.apply(null, dv.concat([0]));
        var pad = (hi - lo) * 0.1 || 0.05; lo -= pad; hi += pad;
        var Y = function (v) { return top + h - ((v - lo) / (hi - lo)) * h; };
        el("line", {x1: ML, x2: W - MR, y1: Y(0), y2: Y(0), class: "lc-zero"}, svg);
        // area: above zero = A ahead (A colour), below zero = B ahead (B colour)
        var dpos = "M" + X(0) + " " + Y(0), dneg = dpos;
        for (var i = 0; i < N; i++) { dpos += " L" + X(i).toFixed(1) + " " + Y(Math.max(0, dv[i])).toFixed(1); dneg += " L" + X(i).toFixed(1) + " " + Y(Math.min(0, dv[i])).toFixed(1); }
        dpos += " L" + X(N - 1) + " " + Y(0) + " Z"; dneg += " L" + X(N - 1) + " " + Y(0) + " Z";
        el("path", {d: dpos, fill: a.c, "fill-opacity": 0.28}, svg);
        el("path", {d: dneg, fill: b.c, "fill-opacity": 0.28}, svg);
        el("path", {d: line(dv, X, Y), class: "lc-line", stroke: "#e9edf3"}, svg);
        el("text", {x: ML + 4, y: top + 11, class: "lc-tick"}, svg).textContent = "▲ " + a.tla + " ahead";
        el("text", {x: ML + 4, y: top + h - 3, class: "lc-tick"}, svg).textContent = "▼ " + b.tla + " ahead";
        el("text", {x: W - MR, y: Y(dv[N - 1]) - 4, class: "lc-tick end"}, svg).textContent = sign(dv[N - 1]) + " s";
      } else if (p.k === "brake") {
        [0, 1].forEach(function (s) {
          var v = cur.ch.brake[s], start = -1, yy = top + s * (h / 2) + 2, hh = h / 2 - 4;
          for (var i = 0; i <= N; i++) {
            if (i < N && v[i] && start < 0) start = i;
            if ((i === N || !v[i]) && start >= 0) {
              el("rect", {x: X(start), y: yy, width: Math.max(1.5, X(i - 1) - X(start)), height: hh, rx: 2, fill: cols[s],
                "fill-opacity": s && b.same ? 0.55 : 0.9}, svg);
              start = -1;
            }
          }
          el("text", {x: ML - 4, y: yy + hh - 3, class: "lc-tick end"}, svg).textContent = cur.drivers[s].tla;
        });
      } else {
        var arr = cur.ch[p.k], mn = p.k === "speed" ? Math.floor(Math.min.apply(null, arr[0].concat(arr[1])) / 50) * 50 : 0;
        var mx = p.k === "speed" ? Math.ceil(Math.max.apply(null, arr[0].concat(arr[1])) / 50) * 50 : p.k === "gear" ? 8 : 100;
        if (p.k === "gear") mn = 1;
        var Ys = function (v) { return top + h - ((v - mn) / (mx - mn)) * h; };
        var ticks = p.k === "speed" ? [] : p.k === "gear" ? [2, 4, 6, 8] : [0, 50, 100];
        if (p.k === "speed") for (var t = mn; t <= mx; t += 50) ticks.push(t);
        ticks.forEach(function (t) {
          el("line", {x1: ML, x2: W - MR, y1: Ys(t), y2: Ys(t), class: "lc-gl"}, svg);
          el("text", {x: ML - 4, y: Ys(t) + 4, class: "lc-tick end"}, svg).textContent = t;
        });
        [1, 0].forEach(function (s) {
          var pth = el("path", {d: line(arr[s], X, Ys, p.k === "gear"), class: "lc-line", stroke: cols[s]}, svg);
          if (s === 1 && b.same) pth.setAttribute("stroke-dasharray", "6 4");
        });
      }
      y0 += h + GAP;
    });
    // corner markers
    var bottom = y0 - GAP, lastX = -99;
    (cur.corners || []).forEach(function (c) {
      var x = ML + (c.d / cur.length) * plotW;
      el("line", {x1: x, x2: x, y1: 16, y2: bottom, class: "lc-corner"}, guides);
      if (x - lastX < 14) return;            // too close to the previous number to read
      el("text", {x: x, y: 12, class: "lc-cn2"}, guides).textContent = c.n;
      lastX = x;
    });
    // distance axis
    for (var km = 0; km * 1000 <= cur.length; km++) {
      var xk = ML + (km * 1000 / cur.length) * plotW;
      el("text", {x: xk, y: bottom + 14, class: "lc-tick mid"}, svg).textContent = km + " km";
    }
    cursor = el("line", {y1: 16, y2: bottom, class: "lc-cursor", visibility: "hidden"}, svg);
    var hit = el("rect", {x: ML, y: 0, width: plotW, height: H, fill: "transparent"}, svg);
    hit.addEventListener("pointermove", function (e) {
      var r = svg.getBoundingClientRect(), x = (e.clientX - r.left) * (W / r.width);
      hover(Math.round(Math.max(0, Math.min(1, (x - ML) / plotW)) * (N - 1)));
    });
    hit.addEventListener("pointerleave", function () { hover(null); });
    hit.addEventListener("pointerdown", function (e) { if (e.pointerType !== "mouse") hit.dispatchEvent(new PointerEvent("pointermove", e)); });
    layout = {X: X};
    hover(hoverI);
  }

  function line(v, X, Y, step) {
    var d = "";
    for (var i = 0; i < v.length; i++) {
      if (step && i) d += " L" + X(i).toFixed(1) + " " + Y(v[i - 1]).toFixed(1);
      d += (i ? " L" : "M") + X(i).toFixed(1) + " " + Y(v[i]).toFixed(1);
    }
    return d;
  }

  function hover(i) {
    hoverI = i;
    if (!cur || !layout) return;
    if (i === null || i === undefined) {
      if (cursor) cursor.setAttribute("visibility", "hidden");
      if (mapDot) mapDot.setAttribute("visibility", "hidden");
      read.innerHTML = '<span class="muted">Move over the charts to read both laps at one point.</span>';
      return;
    }
    var x = layout.X(i);
    cursor.setAttribute("x1", x); cursor.setAttribute("x2", x); cursor.setAttribute("visibility", "visible");
    if (mapDot && cur.map) {
      var q = cur.map.xy[Math.min(cur.map.xy.length - 1, Math.round(i / 2))];
      mapDot.setAttribute("cx", q[0]); mapDot.setAttribute("cy", q[1]); mapDot.setAttribute("visibility", "visible");
    }
    var dist = i * cur.step, corner = "";
    (cur.corners || []).forEach(function (c) { if (Math.abs(c.d - dist) < 60) corner = " · turn " + c.n; });
    var ch = cur.ch, out = '<span class="muted">' + (dist / 1000).toFixed(2) + " km" + corner + "</span>";
    [0, 1].forEach(function (s) {
      out += '<span class="lc-drv' + (s && cur.drivers[1].same ? " dash" : "") + '"><i style="--tc:' + esc(cur.drivers[s].c) + '"></i><b>' + esc(cur.drivers[s].tla) + "</b> " +
        Math.round(ch.speed[s][i]) + " km/h · gear " + ch.gear[s][i] + " · " + Math.round(ch.throttle[s][i]) + "%" + (ch.brake[s][i] ? " · brake" : "") + "</span>";
    });
    var g = cur.delta[i];
    out += "<span>" + (Math.abs(g) < 0.0005 ? "level" : (g > 0 ? cur.drivers[0].tla : cur.drivers[1].tla) + " ahead by " + Math.abs(g).toFixed(3) + " s") + "</span>";
    read.innerHTML = out;
  }

  box.querySelector(".lc-tabs").addEventListener("click", function (e) {
    var bt = e.target.closest("button[data-sid]");
    if (!bt) return;
    box.querySelectorAll(".lc-tabs button").forEach(function (x) {
      x.classList.toggle("on", x === bt); x.setAttribute("aria-pressed", x === bt ? "true" : "false");
    });
    load(bt.dataset.sid);
  });
  var rt = null;
  window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { if (cur && !body.hidden) drawCharts(); }, 150); });
  var first = box.querySelector(".lc-tabs button.on");
  if (first) load(first.dataset.sid);
})();
