"""Circle of Doom: every car on a clock face by its time gap to the leader, plus a pit stop forecast.

One full turn of the circle is one lap (the field's typical current lap time). The leader is at
twelve o'clock; a car 30 s behind on a 90 s lap sits a third of the way round. Pitting moves a car
back by the pit loss, so where it lands on the circle shows where it rejoins and who passes it.

Pit loss per circuit is measured from earlier races at the circuit (pitloss.py); the table below is the
fallback when there is too little data. Under a VSC or Safety Car the field drives slower, so a stop
costs less."""
import re
import statistics

from . import pitloss

# fallback green-flag pit loss in seconds, matched on the meeting's location / circuit / country (lower case)
PIT_LOSS = [
    ("sakhir", 23), ("bahrain", 23), ("jeddah", 20), ("melbourne", 19), ("suzuka", 22), ("shanghai", 23),
    ("miami", 20), ("imola", 28), ("monaco", 19), ("monte carlo", 19), ("barcelona", 22), ("madrid", 22),
    ("montr", 18), ("spielberg", 20), ("austria", 20), ("silverstone", 20), ("budapest", 21),
    ("hungaroring", 21), ("francorchamps", 19), ("belgium", 19), ("zandvoort", 21), ("monza", 24), ("baku", 20), ("singapore", 28),
    ("marina bay", 28), ("austin", 20), ("mexico", 22), ("paulo", 21), ("interlagos", 21),
    ("las vegas", 20), ("lusail", 25), ("qatar", 25), ("yas", 22), ("abu dhabi", 22),
]
DEFAULT_LOSS = 22.0
FACTOR = {"green": 1.0, "vsc": 0.65, "sc": 0.5}   # share of the green-flag loss under VSC / SC
_LAPS = re.compile(r"(\d+)\s*L", re.I)


def _num(v):
    """'+5.123' -> 5.123; '1:36.767' -> 96.767; anything else -> None."""
    if isinstance(v, dict):
        v = v.get("Value")
    s = str(v or "").strip().lstrip("+")
    if not s:
        return None
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            return int(m) * 60 + float(sec)
        return float(s)
    except ValueError:
        return None


def _laps_down(v):
    if isinstance(v, dict):
        v = v.get("Value")
    m = _LAPS.search(str(v or ""))
    return int(m.group(1)) if m else None


def pit_loss(data):
    """(green-flag pit loss in s, circuit name, measured?) for the session in the feed."""
    meet = (data.get("SessionInfo") or {}).get("Meeting") or {}
    names = [str(meet.get("Location") or ""), str((meet.get("Circuit") or {}).get("ShortName") or ""),
             str((meet.get("Country") or {}).get("Name") or ""), str(meet.get("Name") or "")]
    try:
        got = pitloss.measured((meet.get("Circuit") or {}).get("Key"))
    except Exception:  # noqa: BLE001  (no database in tests, locked file): use the table
        got = None
    if got:
        return float(got), names[1] or names[0], True
    hay = " | ".join(names).lower()
    for key, loss in PIT_LOSS:
        if re.search(r"\b" + re.escape(key), hay):
            return float(loss), names[1] or names[0], False
    return DEFAULT_LOSS, names[1] or names[0], False


def lap_reference(lines, fallback=90.0):
    """Typical current lap time: median of the latest laps of cars on track (pit laps left out)."""
    laps = []
    for ln in lines.values():
        if not isinstance(ln, dict) or ln.get("InPit") or ln.get("PitOut") or ln.get("Retired") or ln.get("Stopped"):
            continue
        t = _num(ln.get("LastLapTime"))
        if t and 40 < t < 300:
            laps.append(t)
    if len(laps) < 3:
        return fallback
    med = statistics.median(laps)
    laps = [t for t in laps if t < med * 1.1] or laps     # a slow lap (traffic, damage) must not skew it
    return round(statistics.median(laps), 3)


def build(data, num2info, flag_kind=None, lap_hint=90.0):
    """Circle data for the browser.

    num2info: {racing number: {'tla', 'colour'}} (feed DriverList merged with our entrants).
    Returns {'lap': s, 'loss': {'green','vsc','sc'}, 'mode', 'circuit', 'cars': [...]} with cars in
    running order; 'gap' is the time behind the leader (laps down included, so it can exceed a lap)."""
    lines = ((data.get("TimingData") or {}).get("Lines")) or {}
    lap = lap_reference(lines, lap_hint)
    rows = []
    for num, ln in lines.items():
        if not isinstance(ln, dict):
            continue
        try:
            pos = int(ln.get("Position"))
        except (TypeError, ValueError):
            continue
        rows.append((pos, str(num), ln))
    rows.sort()
    stints = ((data.get("TimingAppData") or {}).get("Lines")) or {}
    cars, prev = [], None
    for pos, num, ln in rows:
        info = num2info.get(num) or {}
        out = bool(ln.get("Retired") or ln.get("Stopped"))
        if prev is None:
            gap = 0.0
        else:
            g = _num(ln.get("GapToLeader"))
            if g is None:
                iv = _num(ln.get("IntervalToPositionAhead"))
                down = _laps_down(ln.get("GapToLeader"))
                iv_down = _laps_down(ln.get("IntervalToPositionAhead"))
                if iv is not None and prev["gap"] is not None:
                    gap = prev["gap"] + iv
                elif iv_down is not None:
                    gap = prev["gap"] + iv_down * lap          # lapped by the car ahead
                elif down is not None:
                    # lapped and no usable interval: at least that many laps down, behind the car ahead
                    gap = max(down * lap, (prev["gap"] or 0) + 0.5)
                else:
                    gap = (prev["gap"] or 0) + 0.5
            else:
                gap = g
                if prev["gap"] is not None and gap < prev["gap"]:
                    gap = prev["gap"]           # feed values arrive at slightly different moments
        tyre = None
        st = (stints.get(num) or {}).get("Stints") if isinstance(stints.get(num), dict) else None
        if isinstance(st, dict) and st:
            keys = sorted((k for k in st if str(k).isdigit() and isinstance(st[k], dict)), key=int)
            if keys:
                c = str(st[keys[-1]].get("Compound") or "").upper()
                if c and c != "UNKNOWN":
                    tyre = {"c": c.lower(), "l": "I" if c == "INTERMEDIATE" else c[:1],
                            "age": int(st[keys[-1]].get("TotalLaps") or 0)}
        car = {"n": num, "tla": info.get("tla") or f"#{num}", "c": "#" + (info.get("colour") or "8d97aa"),
               "p": pos, "gap": round(gap, 3), "int": None if prev is None else round(gap - prev["gap"], 3),
               "pit": bool(ln.get("InPit")), "pitout": bool(ln.get("PitOut")), "out": out,
               "stops": int(ln.get("NumberOfPitStops") or 0), "tyre": tyre}
        cars.append(car)
        if not out:
            prev = car
    loss, circuit, measured = pit_loss(data)
    mode = flag_kind if flag_kind in ("sc", "vsc") else "green"
    return {"lap": lap, "circuit": circuit, "mode": mode, "measured": measured,
            "loss": {k: round(loss * f, 1) for k, f in FACTOR.items()},
            "cars": [c for c in cars if not c["out"]], "out": [c["tla"] for c in cars if c["out"]]}


def forecast(circle, tla, loss):
    """Where `tla` rejoins if it pits now: position, the cars around it and who gets past.
    Same calculation as the browser does (kept here for tests and as the reference)."""
    cars = circle["cars"]
    me = next((c for c in cars if c["tla"] == tla), None)
    if not me:
        return None
    t = me["gap"] + loss
    others = [c for c in cars if c is not me]
    ahead = [c for c in others if c["gap"] <= t]
    behind = [c for c in others if c["gap"] > t]
    return {"pos": len(ahead) + 1,
            "ahead": max(ahead, key=lambda c: c["gap"])["tla"] if ahead else None,
            "ahead_by": round(t - max(c["gap"] for c in ahead), 1) if ahead else None,
            "behind": min(behind, key=lambda c: c["gap"])["tla"] if behind else None,
            "behind_by": round(min(c["gap"] for c in behind) - t, 1) if behind else None,
            "passed_by": [c["tla"] for c in others if me["gap"] < c["gap"] <= t]}
