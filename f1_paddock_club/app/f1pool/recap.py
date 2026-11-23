"""Race recap for the race page: tyre strategy per driver and every driver's lap times, with Safety Car,
VSC and red flag laps marked. Built once per race or sprint from F1's timing archive (TimingData,
TimingAppData, TrackStatus, DriverList: about 6 MB) and stored gzipped in /data/recap."""
import gzip
import json
import logging
import threading
from datetime import timedelta
from pathlib import Path

from . import db
from . import livetiming as lt

log = logging.getLogger("recap")
VERSION = 1
KINDS = {"S": "Sprint", "R": "Race"}
TOPICS = ["TimingData", "TimingAppData", "TrackStatus", "DriverList"]
# worst track status during a lap wins; codes from the TrackStatus feed
STATUS = {"5": ("red", 4), "4": ("sc", 3), "6": ("vsc", 2), "7": ("vsc", 2), "2": ("y", 1)}
_building = {}
_lock = threading.Lock()
_heavy = threading.Semaphore(1)


def _stints(raw, laps_done):
    """[{'c': compound letter, 'from': first lap, 'to': last lap, 'new': bool}] from a TimingAppData line,
    clipped to the laps the driver completed (the last stint runs to his final lap)."""
    if isinstance(raw, list):
        raw = {str(i): s for i, s in enumerate(raw)}
    if not isinstance(raw, dict) or not laps_done:
        return []
    out, lap = [], 1
    for k in sorted((k for k in raw if str(k).isdigit() and isinstance(raw[k], dict)), key=int):
        s = raw[k]
        try:
            n = int(s.get("TotalLaps") or 0) - int(s.get("StartLaps") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0 and out:
            continue                                  # an empty stint record (fitted, no lap done yet)
        comp = str(s.get("Compound") or "").upper()
        letter = {"INTERMEDIATE": "I", "WET": "W", "UNKNOWN": "?", "": "?"}.get(comp, comp[:1])
        if out and str(s.get("TyresNotChanged")) == "1" and letter == out[-1]["c"]:
            out[-1]["to"] += max(n, 1)                 # through the pit lane without new tyres: same stint
            lap += max(n, 1)
            continue
        out.append({"c": letter, "from": lap, "to": lap + max(n, 1) - 1, "new": str(s.get("New")).lower() == "true"})
        lap += max(n, 1)
    out = [s for s in out if s["from"] <= laps_done]
    if out:
        out[-1]["to"] = laps_done
    return out


def build(streams, info_db=None):
    """Recap from archive files ({topic: text}); raises ValueError when there's nothing to show."""
    st = lt.TimingState()
    events = {}                       # racing number -> [(session t, lap number, lap time s)]
    nlaps = {}
    lap_end = {}                      # race lap -> session time the first car completed it
    for t, m in lt.parse_timed(streams.get("TimingData") or ""):
        st.apply("TimingData", m)
        lines = m.get("Lines") if isinstance(m, dict) else None
        if not isinstance(lines, dict):
            continue
        for num, ln in lines.items():
            if not isinstance(ln, dict):
                continue
            if "NumberOfLaps" in ln:
                try:
                    n = int(ln["NumberOfLaps"])
                except (TypeError, ValueError):
                    n = 0
                nlaps[num] = n
                if n and n not in lap_end:
                    lap_end[n] = t
            llt = ln.get("LastLapTime")
            val = llt.get("Value") if isinstance(llt, dict) else None
            secs = lt._laptime(val) if val else None
            if secs and nlaps.get(num):
                events.setdefault(num, []).append((t, nlaps[num], secs))
    for m in lt.parse_stream(streams.get("TimingAppData") or ""):
        st.apply("TimingAppData", m)
    for m in lt.parse_stream(streams.get("DriverList") or ""):
        st.apply("DriverList", m)
    total = max(lap_end) if lap_end else 0
    if total < 2:
        raise ValueError("no laps in the timing data")

    track = []
    for t, m in lt.parse_timed(streams.get("TrackStatus") or ""):
        if isinstance(m, dict) and m.get("Status"):
            track.append((t, str(m["Status"])))
    status = []
    for lap in range(1, total + 1):
        t1 = lap_end[lap]
        t0 = lap_end.get(lap - 1, t1 - 600)
        worst, rank = "g", 0
        code = "1"
        for ts, c in track:
            if ts <= t0:
                code = c
            elif ts <= t1:
                s = STATUS.get(c)
                if s and s[1] > rank:
                    worst, rank = s
        s = STATUS.get(code)
        if s and s[1] > rank:
            worst = s[0]
        status.append(worst)

    data = st.data
    order = lt.classification(data)
    lines = ((data.get("TimingData") or {}).get("Lines")) or {}
    app = ((data.get("TimingAppData") or {}).get("Lines")) or {}
    drv = lt.driver_info(data)
    out_drivers = []
    for pos, num in enumerate(order, 1):
        ln = lines.get(num) or {}
        done = nlaps.get(num) or 0
        times = [None] * total
        for _t, n, secs in events.get(num, []):
            if 1 <= n <= total:
                times[n - 1] = round(secs, 3)
        d = drv.get(num) or {}
        extra = (info_db or {}).get(num) or {}
        a = app.get(num) or {}
        out_drivers.append({
            "num": num, "tla": d.get("tla") or extra.get("tla") or f"#{num}", "name": d.get("name") or "",
            "team": d.get("team") or "", "c": "#" + (d.get("colour") or extra.get("colour") or "8d97aa"),
            "pos": pos, "grid": a.get("GridPos") or None, "laps": done,
            "out": bool(ln.get("Retired") or ln.get("Stopped")) and done < total,
            "stints": _stints(a.get("Stints"), done), "times": times})
        # a stop is the end of every stint but the last (the in-lap); more reliable than the pit flags
        out_drivers[-1]["pits"] = [s["to"] for s in out_drivers[-1]["stints"][:-1]]
    if not any(d["stints"] for d in out_drivers):
        raise ValueError("no tyre data in the archive")
    # second car of a team gets a dashed line in the browser
    seen = set()
    for d in out_drivers:
        key = d["c"].lower()
        if key in seen:
            d["dash"] = True
        seen.add(key)
    return {"v": VERSION, "total": total, "track": status, "drivers": out_drivers}


# ------------------------------------------------------------------ cache -----
def sessions(event_id):
    return db.q("SELECT id, kind, name FROM sessions WHERE event_id=? AND kind IN ('S','R') AND path IS NOT NULL "
                "AND path<>'' AND start_utc<? ORDER BY start_utc",
                event_id, db.iso(db.now_utc() - timedelta(hours=2)))


def path_for(data_dir, sid):
    return Path(data_dir) / "recap" / f"{sid}-v{VERSION}.json.gz"


def get(http, data_dir, sid):
    """('ready', gzipped bytes) | ('building', None) | ('error', text); starts a build when needed."""
    p = path_for(data_dir, sid)
    if p.exists():
        return "ready", p.read_bytes()
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
    if not path_for(data_dir, sid).exists():
        get(http, data_dir, sid)


def _build(http, data_dir, sid):
    with _heavy:
        try:
            s = db.q1("SELECT s.*, e.name AS ev_name FROM sessions s JOIN events e ON e.id=s.event_id "
                      "WHERE s.id=?", sid)
            if not s or not s["path"]:
                raise RuntimeError("this session has no timing archive")
            streams = {}
            for topic in TOPICS:
                txt = http.get(f"{s['path']}{topic}.jsonStream", timeout=180)
                if txt:
                    streams[topic] = txt
            if not streams.get("TimingData"):
                raise RuntimeError(f"F1 timing archive not available yet ({http.last_status or 'no answer'})")
            info = {str(e["number"]): {"tla": e["tla"], "colour": e["colour"]}
                    for e in db.q("SELECT number, tla, colour FROM entrants WHERE event_id=?", s["event_id"])}
            try:
                rep = build(streams, info)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from None
            rep.update(sid=sid, name=f"{s['ev_name']}, {s['name']}")
            p = path_for(data_dir, sid)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(json.dumps(rep, separators=(",", ":")).encode(), 6))
            tmp.replace(p)
            db.logmsg("info", f"Race recap ready for {rep['name']}")
            with _lock:
                _building.pop(sid, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("race recap for session %s failed: %s", sid, exc)
            with _lock:
                _building[sid] = f"Race recap could not be made: {exc}"
