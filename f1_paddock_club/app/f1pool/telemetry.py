"""Lap comparison after a session: the pole lap against P2 (qualifying, sprint qualifying) and the
fastest lap against the second fastest (race), from F1's car telemetry in the timing archive.

Per lap: speed, throttle, brake and gear against distance, the time gap along the lap, and a track
map coloured by who was quicker in each part of the lap. Works the way FastF1 does it: lap times and
lap ends from TimingData, car samples (about 4 per second) from CarData.z, positions from Position.z,
distance by integrating speed. CarData.z and Position.z are big (10-20 MB a race), so each session is
built once, in the background, and stored gzipped in /data/telemetry."""
import gzip
import json
import logging
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, track
from . import livetiming as lt

log = logging.getLogger("telemetry")
VERSION = 1
POINTS = 500                     # samples per lap after resampling on distance
SECTORS = 25                     # parts of the lap on the track map
KINDS = {"Q": "Pole lap", "SQ": "Sprint pole lap", "R": "Fastest lap"}
SPEED, RPM, GEAR, THROTTLE, BRAKE, DRS = "2", "0", "3", "4", "5", "45"
_building = {}
_lock = threading.Lock()
_heavy = threading.Semaphore(1)  # one build at a time: the files are big for a Raspberry Pi


# ------------------------------------------------------------------ parsing ---
def _utc(s):
    """'2020-08-30T12:41:44.8129711Z' -> epoch seconds (7 decimals, which fromisoformat may refuse)."""
    s = str(s).rstrip("Z")
    main, _, frac = s.partition(".")
    dt = datetime.strptime(main, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp() + (float("0." + frac) if frac else 0.0)


def laps(timing_text):
    """(final TimingData state, {racing number: [{'end': session s, 'time': s, 'n': lap counter}]}).

    One entry per new LastLapTime value. Keyed by arrival, not by lap counter: an in-lap time can come
    in without the counter moving, and must not replace the lap before it."""
    st = lt.TimingState()
    nlaps, out = {}, {}
    for t, m in lt.parse_timed(timing_text):
        st.apply("TimingData", m)
        lines = m.get("Lines") if isinstance(m, dict) else None
        if not isinstance(lines, dict):
            continue
        for num, ln in lines.items():
            if not isinstance(ln, dict):
                continue
            if "NumberOfLaps" in ln:
                nlaps[num] = ln["NumberOfLaps"]
            llt = ln.get("LastLapTime")
            val = llt.get("Value") if isinstance(llt, dict) else None
            secs = lt._laptime(val) if val else None
            if not secs:
                continue
            mine = out.setdefault(num, [])
            n = int(nlaps.get(num) or 0)
            if mine and mine[-1]["time"] == secs and mine[-1]["n"] == n:
                continue                                  # the same value sent again
            mine.append({"end": t, "time": secs, "n": n})
    return st.data, out


def pick(kind, data, lapmap):
    """The two laps to compare: [(racing number, index in lapmap[num], lap time s), ...] (fastest first)."""
    lines = ((data.get("TimingData") or {}).get("Lines")) or {}

    def best(num, part=None):
        ln = lines.get(num) or {}
        b = ln.get("BestLapTime") or {}
        if part is not None:
            parts = ln.get("BestLapTimes") or {}
            got = [parts[k] for k in sorted(parts, key=lambda k: int(k) if str(k).isdigit() else -1)
                   if isinstance(parts[k], dict) and parts[k].get("Value")]
            if got:
                b = got[-1]
        secs = lt._laptime(b.get("Value")) if b.get("Value") else None
        if not secs:
            return None
        want = int(b.get("Lap") or 0)
        hits = [i for i, lp in enumerate(lapmap.get(num) or []) if abs(lp["time"] - secs) <= 0.002]
        if not hits:
            return None
        i = min(hits, key=lambda k: abs(lapmap[num][k]["n"] - want))
        return num, i, secs

    if kind in ("Q", "SQ"):
        order = lt.classification(data)[:4]
        got = [b for b in (best(n, part=True) for n in order) if b]
        return got[:2] if len(got) >= 2 else []
    cands = sorted((b for b in (best(n) for n in lines) if b), key=lambda b: b[2])
    return cands[:2] if len(cands) >= 2 else []


def _samples(text, nums, key, fields):
    """{racing number: [(utc, values...)]} for the wanted cars, plus the arrival delay of the stream
    (session time minus sample time, median over messages)."""
    out = {n: [] for n in nums}
    delays = []
    for t, raw in lt.parse_timed(text):
        try:
            msg = lt.decode(key + ".z", raw)
        except Exception:  # noqa: BLE001
            continue
        entries = msg.get("Entries") if key == "CarData" else msg.get("Position")
        if not isinstance(entries, list) or not entries:
            continue
        last = None
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                u = _utc(e.get("Utc") or e.get("Timestamp"))
            except ValueError:
                continue
            last = u
            cars = e.get("Cars") if key == "CarData" else e.get("Entries")
            for n in nums:
                c = (cars or {}).get(n)
                if not isinstance(c, dict):
                    continue
                if key == "CarData":
                    ch = c.get("Channels") or {}
                    out[n].append((u,) + tuple(float(ch.get(f) or 0) for f in fields))
                elif c.get("X") is not None:
                    out[n].append((u, float(c["X"]), float(c["Y"])))
        if last is not None:
            delays.append(t - last)
    return out, (statistics.median(delays) if delays else None)


# ------------------------------------------------------------------ maths -----
def _interp(xs, ys, x):
    """Linear interpolation in sorted xs (clamped at the ends)."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo] or 1e-9
    return ys[lo] + (ys[hi] - ys[lo]) * (x - xs[lo]) / span


def _nearest(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    return ys[lo] if x - xs[lo] <= xs[hi] - x else ys[hi]


def lap_trace(samples, t0, t1):
    """Samples of one lap (utc window t0..t1) as columns with time from the lap start and distance
    (speed integrated, trapezium rule). Points at the exact window ends are interpolated."""
    rows = [s for s in samples if t0 - 1.5 <= s[0] <= t1 + 1.5]
    if len(rows) < 20:
        return None
    us = [r[0] for r in rows]
    inside = [r for r in rows if t0 < r[0] < t1]
    edge = [tuple(_interp(us, [r[i] for r in rows], t) for i in range(len(rows[0]))) for t in (t0, t1)]
    rows = [edge[0]] + inside + [edge[1]]
    t = [r[0] - t0 for r in rows]
    d = [0.0]
    for i in range(1, len(rows)):
        d.append(d[-1] + (rows[i][1] + rows[i - 1][1]) / 2 / 3.6 * (t[i] - t[i - 1]))
    return {"t": t, "d": d, "speed": [r[1] for r in rows], "throttle": [r[2] for r in rows],
            "brake": [r[3] for r in rows], "gear": [r[4] for r in rows], "drs": [r[5] for r in rows]}


def compare(a, b, lap_a, lap_b, pos_a=None, frame=None, corners=None):
    """Both laps on one distance axis of POINTS samples; delta = how far B is behind A at each point."""
    length = (a["d"][-1] + b["d"][-1]) / 2
    sa, sb = length / a["d"][-1], length / b["d"][-1]
    da, db_ = [x * sa for x in a["d"]], [x * sb for x in b["d"]]
    grid = [length * i / (POINTS - 1) for i in range(POINTS)]
    ch = {}
    for name, fn in (("speed", _interp), ("throttle", _interp), ("brake", _nearest), ("gear", _nearest)):
        ch[name] = [[round(fn(da, a[name], g), 1) for g in grid], [round(fn(db_, b[name], g), 1) for g in grid]]
    ch["brake"] = [[1 if v else 0 for v in row] for row in ch["brake"]]
    ch["gear"] = [[int(v) for v in row] for row in ch["gear"]]
    ta = [_interp(da, a["t"], g) for g in grid]
    tb = [_interp(db_, b["t"], g) for g in grid]
    raw = [y - x for x, y in zip(ta, tb)]
    drift = (lap_b - lap_a) - raw[-1]            # make the gap end exactly at the lap time difference
    delta = [round(r + drift * i / (POINTS - 1), 3) for i, r in enumerate(raw)]
    out = {"length": round(length), "step": round(length / (POINTS - 1), 2), "ch": ch, "delta": delta}
    if pos_a and frame:
        pt, px, py = pos_a["t"], pos_a["x"], pos_a["y"]
        xy = [frame.tf(_interp(pt, px, t), _interp(pt, py, t)) for t in ta]
        per = POINTS // SECTORS
        segs = []
        for k in range(SECTORS):
            i0, i1 = k * per, (POINTS - 1 if k == SECTORS - 1 else (k + 1) * per)
            gain = (tb[i1] - tb[i0]) - (ta[i1] - ta[i0])           # > 0: A quicker here
            pts = xy[i0:i1 + 1]
            segs.append({"p": "M" + " L".join(f"{x:.0f} {y:.0f}" for x, y in pts), "w": 0 if gain >= 0 else 1,
                         "g": round(abs(gain), 3)})
        allx, ally = [p[0] for p in xy], [p[1] for p in xy]
        pad = 500
        out["map"] = {"viewBox": f"{min(allx) - pad:.0f} {min(ally) - pad:.0f} "
                                 f"{max(allx) - min(allx) + 2 * pad:.0f} {max(ally) - min(ally) + 2 * pad:.0f}",
                      "segs": segs, "xy": [[round(x), round(y)] for x, y in xy[::2]]}
        if corners:
            raw_xy = [(_interp(pt, px, t), _interp(pt, py, t)) for t in ta]
            cs = []
            for c in corners:
                cx, cy = c["x"], c["y"]
                i = min(range(POINTS), key=lambda j: (raw_xy[j][0] - cx) ** 2 + (raw_xy[j][1] - cy) ** 2)
                cs.append({"n": c["n"], "d": round(grid[i]), "x": round(xy[i][0]), "y": round(xy[i][1])})
            out["corners"] = sorted(cs, key=lambda c: c["d"])
    return out


def _fmt(secs):
    m = int(secs // 60)
    return f"{m}:{secs - m * 60:06.3f}"


# ------------------------------------------------------------------ build -----
def build(streams, kind, info_db=None, outline=None):
    """Lap comparison from archive files: {topic: text}. Raises ValueError when it can't be made."""
    data, lapmap = laps(streams.get("TimingData") or "")
    for _t, m in lt.parse_timed(streams.get("DriverList") or ""):
        data.setdefault("DriverList", {})
        lt.merge(data["DriverList"], m if isinstance(m, dict) else {})
    chosen = pick(kind, data, lapmap)
    if len(chosen) < 2:
        raise ValueError("no two complete laps to compare")
    nums = [c[0] for c in chosen]
    car, car_delay = _samples(streams.get("CarData.z") or "", nums, "CarData", (SPEED, THROTTLE, BRAKE, GEAR, DRS))
    pos, pos_delay = _samples(streams.get("Position.z") or "", nums[:1], "Position", ())
    if car_delay is None:
        raise ValueError("no car telemetry in the archive")
    traces = []
    for num, idx, secs in chosen:
        end_session = lapmap[num][idx]["end"]
        t1 = end_session - car_delay                   # lap end as a sample time (utc)
        tr = lap_trace(car[num], t1 - secs, t1)
        if not tr:
            raise ValueError(f"no telemetry for car {num} on its lap of {_fmt(secs)}")
        tr["t0"] = t1 - secs
        traces.append(tr)
    pos_a = None
    rows = pos.get(nums[0]) or []
    if rows and pos_delay is not None:
        t0 = traces[0]["t0"]
        win = [r for r in rows if t0 - 2 <= r[0] <= t0 + chosen[0][2] + 2]
        if len(win) > 20:
            pos_a = {"t": [r[0] - t0 for r in win], "x": [r[1] for r in win], "y": [r[2] for r in win]}
    frame = None
    corners = None
    if pos_a:
        rot = (outline or {}).get("rotation", 0.0)
        frame = track.Frame(pos_a["x"], pos_a["y"], rot)
        corners = (outline or {}).get("corners")
    out = compare(traces[0], traces[1], chosen[0][2], chosen[1][2], pos_a, frame, corners)
    drv = lt.driver_info(data)
    people = []
    for (num, idx, secs) in chosen:
        d = drv.get(num) or {}
        db_info = (info_db or {}).get(num) or {}
        people.append({"tla": d.get("tla") or db_info.get("tla") or f"#{num}", "name": d.get("name") or "",
                       "team": d.get("team") or "", "c": "#" + (d.get("colour") or db_info.get("colour") or "8d97aa"),
                       "lap": _fmt(secs), "secs": secs})
    if people[0]["c"].lower() == people[1]["c"].lower():
        people[1]["same"] = True                        # teammates: the browser draws B dashed
    out.update(v=VERSION, kind=kind, label=KINDS.get(kind, "Lap"), drivers=people,
               gap=round(chosen[1][2] - chosen[0][2], 3))
    return out


def sessions(event_id):
    """Sessions of the weekend that can have a lap comparison, in weekend order."""
    return db.q("SELECT id, kind, name, status, path, start_utc FROM sessions WHERE event_id=? AND kind IN "
                "('SQ','Q','R') AND path IS NOT NULL AND path<>'' AND start_utc<? ORDER BY start_utc",
                event_id, db.iso(db.now_utc() - timedelta(hours=2)))


def path_for(data_dir, sid):
    return Path(data_dir) / "telemetry" / f"{sid}-v{VERSION}.json.gz"


def get(http, data_dir, sid):
    """('ready', gzipped bytes) | ('building', None) | ('error', text); starts a build when needed."""
    p = path_for(data_dir, sid)
    if p.exists():
        return "ready", p.read_bytes()
    nope = Path(str(p) + ".none")
    if nope.exists():
        return "error", nope.read_text()
    with _lock:
        state = _building.get(sid)
        if state == "building":
            return "building", None
        if state:
            _building.pop(sid)
            return "error", state
        _building[sid] = "building"
    threading.Thread(target=_build, args=(http, data_dir, sid), daemon=True).start()
    return "building", None


def warm(http, data_dir, sid):
    """Build in the background if not done yet (called by the engine when a session is final)."""
    if not path_for(data_dir, sid).exists():
        get(http, data_dir, sid)


TOPICS = ["TimingData", "DriverList", "SessionInfo", "CarData.z", "Position.z"]


def _build(http, data_dir, sid):
    with _heavy:
        _build_one(http, data_dir, sid)


def _build_one(http, data_dir, sid):
    try:
        s = db.q1("SELECT s.*, e.name AS ev_name, e.year FROM sessions s JOIN events e ON e.id=s.event_id "
                  "WHERE s.id=?", sid)
        if not s or not s["path"]:
            raise RuntimeError("this session has no timing archive")
        streams = {}
        for topic in TOPICS:
            txt = http.get(f"{s['path']}{topic}.jsonStream", timeout=180)
            if txt:
                streams[topic] = txt
        if not streams.get("TimingData") or not streams.get("CarData.z"):
            raise RuntimeError(f"F1 telemetry not available yet ({http.last_status or 'no answer'})")
        info = {str(e["number"]): {"tla": e["tla"], "colour": e["colour"]}
                for e in db.q("SELECT number, tla, colour FROM entrants WHERE event_id=?", s["event_id"])}
        st = lt.TimingState()
        for m in lt.parse_stream(streams.get("SessionInfo") or ""):
            st.apply("SessionInfo", m)
        key = lt.circuit_key(st.data)
        outline = track.outline(key, s["year"]) if key else None
        try:
            rep = build(streams, s["kind"], info, outline)
        except ValueError as exc:
            # a day after the session the archive is complete: this lap can't be compared, don't retry
            if db.parse_iso(s["start_utc"]) < db.now_utc() - timedelta(hours=24):
                p = path_for(data_dir, sid)
                p.parent.mkdir(parents=True, exist_ok=True)
                Path(str(p) + ".none").write_text(f"No lap comparison for this session: {exc}.")
            raise RuntimeError(str(exc)) from None
        rep.update(sid=sid, name=f"{s['ev_name']}, {s['name']}")
        p = path_for(data_dir, sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(rep, separators=(",", ":")).encode(), 6))
        tmp.replace(p)
        db.logmsg("info", f"Lap comparison ready for {rep['name']}: {rep['drivers'][0]['tla']} vs "
                          f"{rep['drivers'][1]['tla']}")
        with _lock:
            _building.pop(sid, None)
    except Exception as exc:  # noqa: BLE001
        log.warning("lap comparison for session %s failed: %s", sid, exc)
        with _lock:
            _building[sid] = f"Lap comparison could not be made: {exc}"
