"""Official results cross-check after each session, from Jolpica (the free successor of the Ergast F1 API).

F1's live timing feed is used live; once Jolpica has the official classification it replaces the feed's
version (source 'official', which later feed/archive updates do not overwrite). Admin corrections keep
priority; when they disagree with the official result the admins get an e-mail."""
import logging
from datetime import timedelta

import requests

from . import db, mailer

log = logging.getLogger("official")
API = "https://api.jolpi.ca/ergast/f1/{year}/{rnd}/{what}.json"
CHECK_FOR = timedelta(days=3)          # keep checking this long after a session


def _fetch(year, rnd, what):
    r = requests.get(API.format(year=year, rnd=rnd, what=what), timeout=20,
                     headers={"User-Agent": "PaddockClubReeuwijk/1.0"})
    if not r.ok:
        return None
    races = (((r.json() or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
    return races[0] if races else None


def _code(entry):
    d = entry.get("Driver") or {}
    return (d.get("code") or "").upper() or None


def official_fields(ev, kind):
    """{field: {slot: tla}} from Jolpica for one session, or None when not published (yet)."""
    if kind == "R":
        race = _fetch(ev["year"], ev["round"], "results")
        rows = (race or {}).get("Results") or []
        if len(rows) < 10:
            return None
        out = {"race_pos": {}}
        for e in rows:
            try:
                pos = int(e.get("position"))
            except (TypeError, ValueError):
                continue
            if _code(e) and pos <= 22:
                out["race_pos"][pos] = _code(e)
            if str((e.get("FastestLap") or {}).get("rank")) == "1" and _code(e):
                out["fl"] = {1: _code(e)}
        return out, race
    if kind == "S":
        race = _fetch(ev["year"], ev["round"], "sprint")
        rows = (race or {}).get("SprintResults") or []
        if len(rows) < 8:
            return None
        return {"sprint_pos": {int(e["position"]): _code(e) for e in rows
                               if _code(e) and str(e.get("position", "")).isdigit() and int(e["position"]) <= 8}}, race
    if kind == "Q":
        race = _fetch(ev["year"], ev["round"], "qualifying")
        rows = (race or {}).get("QualifyingResults") or []
        pole = next((e for e in rows if str(e.get("position")) == "1"), None)
        if not pole or not _code(pole):
            return None
        return {"pole": {1: _code(pole)}}, race
    return None


def _same_weekend(ev_session_start, race):
    """Guard against a round-number mismatch (e.g. a cancelled race): Jolpica's date must be this weekend."""
    try:
        d = db.parse_iso(f"{race['date']}T{(race.get('time') or '12:00:00Z').rstrip('Z')}Z")
    except (KeyError, ValueError):
        return False
    return abs((d - ev_session_start).total_seconds()) < 4 * 86400


def check(engine, now):
    """Apply official results for sessions of the last 3 days. Returns number of fields changed."""
    changed = 0
    rows = db.q("SELECT s.*, e.year, e.round, e.name AS ev_name FROM sessions s JOIN events e ON e.id=s.event_id "
                "WHERE e.cancelled=0 AND e.imported=0 AND e.frozen=0 AND s.kind IN ('Q','S','R') "
                "AND s.start_utc BETWEEN ? AND ? AND s.id IS NOT ?",
                db.iso(now - CHECK_FOR), db.iso(now - timedelta(hours=1)), engine.live_session)
    for s in rows:
        try:
            got = official_fields(s, s["kind"])
        except (requests.RequestException, ValueError) as exc:
            log.debug("jolpica %s %s: %s", s["ev_name"], s["kind"], exc)
            continue
        if not got:
            continue
        fields, race = got
        if not _same_weekend(db.parse_iso(s["start_utc"]), race):
            continue
        for field, slots in fields.items():
            cur = {r["slot"]: (r["value"], r["source"]) for r in db.q(
                "SELECT slot, value, source FROM results WHERE event_id=? AND field=?", s["event_id"], field)}
            top = {k: v for k, v in slots.items() if k <= 10}
            mine = {k: v[0] for k, v in cur.items() if k <= 10}
            if all(cur.get(k, (None, ""))[1] in ("official", "admin") and mine.get(k) == v for k, v in top.items()):
                continue                                          # already applied
            feed_differs = any(mine.get(k) not in (None, v) for k, v in top.items())
            admin_conflict = {k: (mine[k], v) for k, v in top.items()
                              if cur.get(k, (None, ""))[1] == "admin" and mine.get(k) != v}
            engine.write_results(s["event_id"], {field: slots}, "official")
            changed += 1
            label = {"race_pos": "race result", "fl": "fastest lap", "sprint_pos": "sprint result",
                     "pole": "pole position"}[field]
            if feed_differs or admin_conflict:
                diff = ", ".join(f"P{k}: {mine.get(k) or '-'} → {v}" for k, v in sorted(top.items())
                                 if mine.get(k) != v)
                db.logmsg("warning", f"{s['ev_name']}: official {label} differs from F1 timing ({diff}); "
                                     f"official result applied" + (" except admin corrections" if admin_conflict else ""))
                if db.setting(f"official_mail:{s['event_id']}:{field}") is None:
                    db.set_setting(f"official_mail:{s['event_id']}:{field}", "1")
                    body = (f"The official {label} for {s['ev_name']} differs from what F1's live timing gave:\n"
                            f"{diff}\n\nThe official result has been applied and scores recalculated.")
                    if admin_conflict:
                        body += ("\n\nNote: admin corrections take priority and differ from the official result "
                                 "for: " + ", ".join(f"P{k} (admin {a}, official {o})"
                                                     for k, (a, o) in sorted(admin_conflict.items()))
                                 + f".\nCheck them at {mailer.base_url()}/admin/event/{s['event_id']}")
                    mailer.notify_admins(f"{s['ev_name']}: official {label} applied", body)
            else:
                db.logmsg("info", f"{s['ev_name']}: official {label} confirmed")
    return changed
