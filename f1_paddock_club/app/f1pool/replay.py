"""Circle of Doom replays of earlier races and sprints, rebuilt from F1's timing archive.

The archive keeps every timing message of a session with its time stamp. We play those messages in
order and take a picture of the circle every STEP seconds of race time, from the start until the
chequered flag. The result is stored gzipped in /data/replay, so each race is built only once."""
import gzip
import heapq
import json
import logging
import threading
from pathlib import Path

from . import circle, db
from . import livetiming as lt

log = logging.getLogger("replay")
STEP = 4.0                       # seconds of race time between two pictures
VERSION = 1                      # bump when the file format changes; older files are rebuilt
TOPICS = ["DriverList", "SessionInfo", "SessionStatus", "LapCount", "TrackStatus", "TimingData", "TimingAppData"]
_building = {}                   # session id -> 'building' | error text
_lock = threading.Lock()


def _stamp(s):
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def parse_timed(text):
    """[(seconds, message)] from a .jsonStream file ('HH:MM:SS.mmm{...}' per line)."""
    out = []
    for line in text.lstrip("﻿").splitlines():
        line = line.strip().lstrip("﻿")
        if len(line) < 13 or line[2] != ":":
            continue
        try:
            out.append((_stamp(line[:12]), json.loads(line[12:])))
        except (ValueError, IndexError):
            continue
    return out


def _tagged(topic, text):
    for t, m in parse_timed(text):
        yield t, topic, m


def frames(streams, info_db):
    """Pictures of the circle from the timed messages of each topic.

    streams: {topic: file text}; info_db: {racing number: {'tla', 'colour'}} from our entrants."""
    status_times = [(t, m.get("Status")) for t, m in parse_timed(streams.get("SessionStatus", "")) if isinstance(m, dict)]
    stamps = [t for t, _m in parse_timed(streams.get("LapCount", "") or streams.get("TimingData", ""))]
    start = next((t for t, s in status_times if s == "Started"), None)
    finish = next((t for t, s in status_times if s in ("Finished", "Finalised", "Ends") and start is not None
                   and t > start), None)
    if start is None:
        start = stamps[0] if stamps else 0.0
    end = (finish + 20) if finish is not None else (stamps[-1] if stamps else 0.0)
    events = heapq.merge(*(_tagged(topic, txt) for topic, txt in streams.items()), key=lambda e: e[0])
    st = lt.TimingState()
    pending = next(events, None)
    drivers, index, out = [], {}, []
    loss = circuit = None
    nxt = start
    while nxt <= end:
        while pending is not None and pending[0] <= nxt:
            st.apply(pending[1], pending[2])
            pending = next(events, None)
        data = st.data
        if loss is None and data.get("SessionInfo"):
            g, circuit = circle.pit_loss(data)
            loss = {m: round(g * f, 1) for m, f in circle.FACTOR.items()}
        info = dict(info_db)
        for num, d in lt.driver_info(data).items():
            info[num] = {"tla": d["tla"], "colour": d["colour"] or (info.get(num) or {}).get("colour")}
        flag = lt.track_flag(data)
        c = circle.build(data, info, (flag or {}).get("kind"), out[-1]["ref"] if out else 90.0)
        cars = []
        for car in c["cars"]:
            if car["tla"] not in index:
                index[car["tla"]] = len(drivers)
                drivers.append({"tla": car["tla"], "c": car["c"]})
            ty = car["tyre"] or {}
            cars.append([index[car["tla"]], car["p"], car["gap"], 1 if car["pit"] else 0, car["stops"],
                         ty.get("c", ""), ty.get("age", 0)])
        lap, total = lt.lap_count(data)
        if cars:
            out.append({"t": round(nxt - start), "lap": lap, "total": total, "ref": c["lap"],
                        "mode": c["mode"], "flag": (flag or {}).get("label"), "cars": cars, "out": c["out"]})
        nxt += STEP
    laps = {}
    for i, f in enumerate(out):
        laps.setdefault(str(f["lap"]), i)
    return {"v": VERSION, "step": STEP, "circuit": circuit, "loss": loss or
            {m: round(circle.DEFAULT_LOSS * f, 1) for m, f in circle.FACTOR.items()},
            "drivers": drivers, "laps": laps, "frames": out}


def path_for(data_dir, sid):
    return Path(data_dir) / "replay" / f"{sid}-v{VERSION}.json.gz"


def sessions(year):
    """Races and sprints of the season that have an archive to replay, newest first."""
    return db.q("SELECT s.id, s.kind, s.name, s.start_utc, e.name AS ev_name FROM sessions s "
                "JOIN events e ON e.id=s.event_id WHERE e.year=? AND e.cancelled=0 AND e.imported=0 "
                "AND s.kind IN ('R','S') AND s.status IN ('finished','final') AND s.path IS NOT NULL "
                "AND s.path<>'' ORDER BY s.start_utc DESC", year)


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
            _building.pop(sid)          # show the error once, try again on the next request
            return "error", state
        _building[sid] = "building"
    threading.Thread(target=_build, args=(http, data_dir, sid), daemon=True).start()
    return "building", None


def _build(http, data_dir, sid):
    try:
        s = db.q1("SELECT s.*, e.name AS ev_name FROM sessions s JOIN events e ON e.id=s.event_id WHERE s.id=?", sid)
        if not s or not s["path"]:
            raise RuntimeError("this session has no timing archive")
        streams = {}
        for topic in TOPICS:
            txt = http.get(f"{s['path']}{topic}.jsonStream", timeout=120)
            if txt:
                streams[topic] = txt
        if not streams.get("TimingData"):
            raise RuntimeError(f"F1 timing archive not available ({http.last_status or 'no answer'})")
        info = {str(e["number"]): {"tla": e["tla"], "colour": e["colour"]}
                for e in db.q("SELECT number, tla, colour FROM entrants WHERE event_id=?", s["event_id"])}
        rep = frames(streams, info)
        if not rep["frames"]:
            raise RuntimeError("no race data in the archive")
        rep.update(sid=sid, name=f"{s['ev_name']}, {s['name']}")
        p = path_for(data_dir, sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(rep, separators=(",", ":")).encode(), 6))
        tmp.replace(p)
        db.logmsg("info", f"Circle of Doom replay ready for {rep['name']} ({len(rep['frames'])} frames)")
        with _lock:
            _building.pop(sid, None)
    except Exception as exc:  # noqa: BLE001
        log.exception("replay build failed")
        with _lock:
            _building[sid] = f"Replay could not be built: {exc}"
