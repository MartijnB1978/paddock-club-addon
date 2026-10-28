"""Season calendar sync.

1. Base calendar: FastF1's schedule file (built from the official F1 calendar API).
2. Official override: F1 live timing Index.json (start times + data paths) where available.
Anything changed by F1 (date, time, venue, cancellation) is picked up on the next sync.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from . import db

log = logging.getLogger("schedule")
FF1_URL = "https://raw.githubusercontent.com/theOehrly/f1schedule/master/schedule_{year}.json"

KIND_BY_NAME = {
    "practice 1": "FP1", "practice 2": "FP2", "practice 3": "FP3",
    "qualifying": "Q", "race": "R", "sprint": "S",
    "sprint qualifying": "SQ", "sprint shootout": "SQ",
}
SCORED = ("SQ", "S", "Q", "R")
DURATION = {"FP1": 70, "FP2": 70, "FP3": 70, "SQ": 50, "Q": 70, "S": 70, "R": 150}


def _offset(s):
    if not s:
        return timedelta(0)
    m = re.match(r"([+-]?)(\d{1,2}):(\d{2})", str(s))
    if not m:
        return timedelta(0)
    td = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return -td if m.group(1) == "-" else td


def _utc(local, offset):
    if not local:
        return None
    dt = datetime.fromisoformat(str(local)[:19]).replace(tzinfo=timezone(_offset(offset)))
    return dt.astimezone(timezone.utc)


def _norm_name(s):
    return re.sub(r"[^a-z]", "", (s or "").lower().replace("grand prix", ""))


def fetch_ff1(year):
    r = requests.get(FF1_URL.format(year=year), timeout=30)
    r.raise_for_status()
    d = r.json()
    n = len(d["round_number"])
    events = []
    for i in map(str, range(n)):
        rnd = d["round_number"][i]
        if not rnd or d["event_format"][i] == "testing":
            continue
        sessions = {}
        for j in range(1, 6):
            nm = d[f"session{j}"][i]
            if not nm:
                continue
            kind = KIND_BY_NAME.get(nm.lower())
            if kind:
                local = d[f"session{j}_date"][i]
                guess = None
                if local and d["event_date"][i]:
                    # archive path pattern used by livetiming.formula1.com (official index overrides it)
                    guess = (f"{year}/{d['event_date'][i][:10]}_{d['event_name'][i]}/"
                             f"{local[:10]}_{nm}/").replace(" ", "_")
                sessions[kind] = {"name": nm, "start": _utc(local, d["gmt_offset"][i]), "path": guess}
        events.append({
            "round": int(rnd), "name": d["event_name"][i], "country": d["country"][i],
            "location": d["location"][i], "official_name": d["official_event_name"][i],
            "sprint": "SQ" in sessions or "S" in sessions, "sessions": sessions,
        })
    return events


def apply_index(events, index):
    """Override with official live-timing data (times + archive paths)."""
    if not index:
        return events
    by_name = {_norm_name(e["name"]): e for e in events}
    for m in index.get("Meetings", []):
        if "test" in (m.get("Name") or "").lower():
            continue
        ev = by_name.get(_norm_name(m.get("Name")))
        if ev is None:
            # meeting not in base calendar (added by F1) -> add it
            ev = {"round": m.get("Number") or 0, "name": m.get("Name"),
                  "country": (m.get("Country") or {}).get("Name", ""), "location": m.get("Location", ""),
                  "official_name": m.get("OfficialName", ""), "sprint": False, "sessions": {}}
            events.append(ev)
            by_name[_norm_name(ev["name"])] = ev
        if m.get("Number"):
            ev["round"] = int(m["Number"])
        ev["location"] = m.get("Location") or ev["location"]
        ev["official_name"] = m.get("OfficialName") or ev["official_name"]
        for s in m.get("Sessions", []):
            if s.get("Key") == -1 or not s.get("Name"):
                continue
            kind = KIND_BY_NAME.get(s["Name"].lower())
            if not kind:
                continue
            cur = ev["sessions"].setdefault(kind, {"name": s["Name"]})
            if s.get("StartDate"):
                cur["start"] = _utc(s["StartDate"], s.get("GmtOffset"))
            if s.get("Path"):
                cur["path"] = s["Path"]
        ev["sprint"] = "SQ" in ev["sessions"] or "S" in ev["sessions"]
    return events


def save(year, events):
    changes = []
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO seasons(year) VALUES(?)", (year,))
        seen = set()
        for e in events:
            row = c.execute("SELECT * FROM events WHERE year=? AND name=?", (year, e["name"])).fetchone()
            if row is None:
                c.execute("INSERT INTO events(year,round,name,country,location,official_name,sprint) "
                          "VALUES(?,?,?,?,?,?,?)", (year, e["round"], e["name"], e["country"], e["location"],
                                                    e["official_name"], int(e["sprint"])))
                eid = c.execute("SELECT id FROM events WHERE year=? AND name=?", (year, e["name"])).fetchone()[0]
                changes.append(f"added {e['name']}")
            else:
                eid = row["id"]
                if row["imported"]:
                    seen.add(eid)
                    continue
                for col in ("round", "country", "location", "official_name"):
                    if e[col] and row[col] != e[col]:
                        changes.append(f"{e['name']}: {col} {row[col]} -> {e[col]}")
                c.execute("UPDATE events SET round=?, country=?, location=?, official_name=?, sprint=?, cancelled=0 "
                          "WHERE id=?", (e["round"], e["country"] or row["country"], e["location"] or row["location"],
                                         e["official_name"] or row["official_name"], int(e["sprint"]), eid))
            seen.add(eid)
            for kind, s in e["sessions"].items():
                start = db.iso(s.get("start"))
                srow = c.execute("SELECT * FROM sessions WHERE event_id=? AND kind=?", (eid, kind)).fetchone()
                if srow is None:
                    c.execute("INSERT INTO sessions(event_id,kind,name,start_utc,path) VALUES(?,?,?,?,?)",
                              (eid, kind, s["name"], start, s.get("path")))
                else:
                    if srow["manual_start"]:
                        start = srow["start_utc"]
                    if start and srow["start_utc"] != start and kind in SCORED:
                        changes.append(f"{e['name']} {s['name']}: {srow['start_utc']} -> {start}")
                    c.execute("UPDATE sessions SET name=?, start_utc=COALESCE(?, start_utc), "
                              "path=COALESCE(?, path) WHERE id=?", (s["name"], start, s.get("path"), srow["id"]))
            # sessions that disappeared (e.g. format change)
            kinds = list(e["sessions"].keys())
            if kinds:
                c.execute(f"DELETE FROM sessions WHERE event_id=? AND kind NOT IN ({','.join('?' * len(kinds))}) "
                          "AND status='scheduled'", (eid, *kinds))
        # number the weekends in race-date order (official data can list them out of order)
        rows = c.execute("SELECT e.id FROM events e LEFT JOIN sessions s ON s.event_id=e.id AND s.kind='R' "
                         "WHERE e.year=? ORDER BY e.cancelled, COALESCE(s.start_utc, '9999'), e.round",
                         (year,)).fetchall()
        for n, (eid,) in enumerate(rows, start=1):
            c.execute("UPDATE events SET round=? WHERE id=?", (n, eid))
        for row in c.execute("SELECT id, name FROM events WHERE year=? AND cancelled=0 AND imported=0",
                             (year,)).fetchall():
            if row["id"] not in seen:
                has_res = c.execute("SELECT 1 FROM results WHERE event_id=? LIMIT 1", (row["id"],)).fetchone()
                if not has_res:
                    c.execute("UPDATE events SET cancelled=1 WHERE id=?", (row["id"],))
                    changes.append(f"cancelled {row['name']}")
    return changes


def sync(year, http):
    try:
        events = fetch_ff1(year)
    except Exception as exc:  # noqa: BLE001
        log.warning("base calendar fetch failed: %s", exc)
        events = []
    index = http.index(year) if http else None
    events = apply_index(events, index)
    if not events:
        raise RuntimeError("No calendar data available")
    changes = save(year, events)
    db.set_setting(f"schedule_sync_{year}", json.dumps({"at": db.iso(db.now_utc()),
                                                         "official_index": bool(index),
                                                         "events": len(events)}))
    for ch in changes:
        db.logmsg("info", f"Calendar {year}: {ch}")
    return changes
