"""Pit loss per circuit, measured from F1's timing archive instead of a fixed table.

For every stop under green flag: (in-lap + out-lap) minus two of the driver's normal laps around it, so
the slow out-lap on cold tyres counts too (that is what decides where a car rejoins). Wet races are left
out (intermediates and wets change lap times too much). The Circle of Doom uses the median of all stops
at the circuit in the last three seasons. Each season's stops are cached in settings
('pitloss:<circuit key>:<year>'), also a miss, so each race is fetched once. The fixed table in
circle.py stays as the fallback."""
import json
import logging
import statistics
import threading

from . import db
from . import livetiming as lt

log = logging.getLogger("pitloss")
YEARS_BACK = 3
MIN_STOPS = 6               # fewer measured stops than this: keep using the table
_mem = {}                     # circuit key -> averaged loss (or None), filled from settings
_lock = threading.Lock()
_running = set()


def _status_at(track, t):
    """Track status code in force at session time t."""
    st = "1"
    for ts, code in track:
        if ts > t:
            break
        st = code
    return st


def _green(track, t0, t1):
    """True when the track was green (or only a local yellow) for the whole window."""
    if _status_at(track, t0) not in ("1", "2"):
        return False
    return all(code in ("1", "2") for ts, code in track if t0 < ts <= t1)


def wet(appdata_text):
    """True when any car ran intermediates or wets."""
    def walk(v):
        if isinstance(v, dict):
            if str(v.get("Compound") or "").upper() in ("INTERMEDIATE", "WET"):
                return True
            return any(walk(x) for x in v.values())
        if isinstance(v, list):
            return any(walk(x) for x in v)
        return False
    return any(walk(m) for _t, m in lt.parse_timed(appdata_text or ""))


def measure(timing_text, track_text):
    """Pit loss in seconds of every usable green-flag stop in one race."""
    track = []
    for t, m in lt.parse_timed(track_text or ""):
        if isinstance(m, dict) and m.get("Status"):
            track.append((t, str(m["Status"])))
    state, laps, flags = {}, {}, {}
    for t, m in lt.parse_timed(timing_text or ""):
        lines = m.get("Lines") if isinstance(m, dict) else None
        if not isinstance(lines, dict):
            continue
        for num, ln in lines.items():
            if not isinstance(ln, dict):
                continue
            cur = state.setdefault(num, {})
            fl = flags.setdefault(num, {"in": False, "out": False})
            if ln.get("InPit") is True and not cur.get("InPit"):
                fl["in"] = True
            if ln.get("PitOut") is True and not cur.get("PitOut"):
                fl["out"] = True
            for k in ("InPit", "PitOut", "NumberOfLaps"):
                if k in ln:
                    cur[k] = ln[k]
            llt = ln.get("LastLapTime")
            val = llt.get("Value") if isinstance(llt, dict) else None
            secs = lt._laptime(val) if val else None
            if not secs:
                continue
            mine = laps.setdefault(num, [])
            n = int(cur.get("NumberOfLaps") or 0)
            if mine and mine[-1]["time"] == secs and mine[-1]["n"] == n:
                continue                                   # the same value sent again
            mine.append({"n": n, "end": t, "time": secs, "in": fl["in"], "out": fl["out"]})
            flags[num] = {"in": False, "out": False}
    losses = []
    for ls in laps.values():
        # laps in the order they were completed; a stop is an in-lap followed directly by an out-lap
        for i in range(1, len(ls) - 1):
            a, b = ls[i], ls[i + 1]
            if not a["in"] or not b["out"] or a["in"] and a["out"]:
                continue
            if not _green(track, a["end"] - a["time"], b["end"]):
                continue
            ref = [lp["time"] for lp in ls[max(1, i - 6):i] + ls[i + 2:i + 8]
                   if not lp["in"] and not lp["out"] and _green(track, lp["end"] - lp["time"], lp["end"])]
            if len(ref) < 3:
                continue
            loss = a["time"] + b["time"] - 2 * statistics.median(ref)
            if 8 < loss < 45:
                losses.append(loss)
    return [round(x, 2) for x in losses]


def _race_paths(http, year, key):
    """Archive paths of the races at circuit `key` in `year`; None when the index can't be read."""
    idx = http.index(year)
    if not idx:
        return None
    out = []
    for m in idx.get("Meetings") or []:
        if str((m.get("Circuit") or {}).get("Key")) != str(key):
            continue
        for s in m.get("Sessions") or []:
            if s.get("Type") == "Race" and s.get("Name") == "Race" and s.get("Path"):
                out.append(s["Path"])
    return out


def measure_year(http, key, year, path=None):
    """Measure (and cache) the races at circuit `key` in `year`; returns the cached record."""
    ck = f"pitloss:{key}:{year}"
    paths = [path] if path else _race_paths(http, year, key)
    if paths is None:
        return {}
    losses, fetched, skipped = [], False, False
    for p in paths:
        timing = http.get(f"{p}TimingData.jsonStream", timeout=120)
        if not timing:
            continue
        fetched = True
        if wet(http.get(f"{p}TimingAppData.jsonStream") or ""):
            skipped = True
            continue
        losses += measure(timing, http.get(f"{p}TrackStatus.jsonStream") or "")
    rec = {"losses": losses, "loss": round(statistics.median(losses), 1) if losses else None,
           "stops": len(losses), "wet": skipped}
    if fetched or not path:
        db.set_setting(ck, json.dumps(rec))
    with _lock:
        _mem.pop(str(key), None)
    return rec


def _note(key, year, rec):
    if rec.get("loss"):
        db.logmsg("info", f"Pit loss measured for circuit {key} ({year}): median {rec['loss']} s "
                          f"from {rec['stops']} green-flag stops")
    elif rec.get("wet"):
        db.logmsg("info", f"Pit loss for circuit {key} ({year}) not measured: wet race")


def ensure(http, key, year, current_path=None):
    """Fill the cache for the last YEARS_BACK seasons at this circuit (and this year's race when given).
    Runs in the caller's thread; safe to call repeatedly (one run per circuit at a time)."""
    if not key:
        return
    with _lock:
        if key in _running:
            return
        _running.add(key)
    try:
        for y in range(year - YEARS_BACK, year):
            if db.setting(f"pitloss:{key}:{y}") is None:
                rec = measure_year(http, key, y)
                _note(key, y, rec)
        if current_path:
            rec = measure_year(http, key, year, current_path)
            _note(key, year, rec)
    except Exception:  # noqa: BLE001
        log.exception("pit loss measurement failed")
    finally:
        with _lock:
            _running.discard(key)


def measured(key):
    """Median green-flag pit loss of all measured stops at the circuit in the latest seasons, or None."""
    if not key:
        return None
    k = str(key)
    with _lock:
        if k in _mem:
            return _mem[k]
    vals = []
    for r in db.q("SELECT key, value FROM settings WHERE key LIKE ?", f"pitloss:{k}:%"):
        try:
            y = int(r["key"].rsplit(":", 1)[1])
            v = json.loads(r["value"]).get("losses") or []
        except (ValueError, TypeError, AttributeError):
            continue
        if v:
            vals.append((y, v))
    vals.sort(reverse=True)
    pool = [x for _y, v in vals[:YEARS_BACK] for x in v]
    out = round(statistics.median(pool), 1) if len(pool) >= MIN_STOPS else None
    with _lock:
        _mem[k] = out
    return out
