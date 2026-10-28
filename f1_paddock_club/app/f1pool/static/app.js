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
        board.innerHTML = html;
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
