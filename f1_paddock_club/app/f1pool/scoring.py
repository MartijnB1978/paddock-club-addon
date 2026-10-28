"""Game rules and standings."""
import json
from collections import defaultdict

from . import db

# field -> (number of slots, session kind that locks it)
FIELDS = {
    "pole":        (1, "Q"),
    "race_pos":    (10, "R"),
    "fl":          (1, "R"),
    "dotd":        (1, "R"),
    "fp":          (1, "R"),
    "sprint_pole": (1, "SQ"),
    "sprint_pos":  (3, "S"),
}
SEASON_FIELD = "season_pos"
LABELS = {
    "pole": "Pole position", "race_pos": "Race top 10", "fl": "Fastest lap",
    "dotd": "Driver of the Day", "fp": "Fastest pit stop (team)",
    "sprint_pole": "Sprint pole", "sprint_pos": "Sprint top 3", "season_pos": "Championship top 10",
}
RULES = {
    # v2: rules from 2026 go-live
    "v2": {"race_exact": 3, "race_in_top10": 1, "pole": 5, "fl": 5, "dotd": 5, "fp": 5,
           "sprint_exact": 5, "sprint_pole": 5, "season_exact": 10},
    # v1: the original Excel rules (no point for right driver / wrong place)
    "v1": {"race_exact": 3, "race_in_top10": 0, "pole": 5, "fl": 5, "dotd": 5, "fp": 5,
           "sprint_exact": 5, "sprint_pole": 5, "season_exact": 10},
}


def rules_for(year):
    r = db.q1("SELECT rules FROM seasons WHERE year=?", year)
    return RULES.get(r["rules"] if r else "v2", RULES["v2"])


def fields_for_event(ev):
    f = ["pole", "race_pos", "fl", "dotd", "fp"]
    if ev["sprint"]:
        f = ["sprint_pole", "sprint_pos"] + f
    return f


def load_results(event_id):
    out = defaultdict(dict)
    for r in db.q("SELECT field, slot, value, source FROM results WHERE event_id=?", event_id):
        out[r["field"]][r["slot"]] = r["value"]
    return out


def load_predictions(event_id, year=None):
    out = defaultdict(lambda: defaultdict(dict))
    rows = (db.q("SELECT * FROM predictions WHERE event_id=?", event_id) if event_id
            else db.q("SELECT * FROM predictions WHERE event_id=0 AND year=?", year))
    for r in rows:
        out[r["user_id"]][r["field"]][r["slot"]] = r["value"]
    return out


def score_event(pred, res, rules):
    """pred/res: {field: {slot: value}} -> (total, {field: points}, {(field,slot): points})"""
    per_field, per_slot = defaultdict(int), {}
    rp = res.get("race_pos", {})
    top10 = {rp.get(i) for i in range(1, 11)} - {None}
    for slot, d in pred.get("race_pos", {}).items():
        if not d or not rp:
            continue
        pts = rules["race_exact"] if rp.get(slot) == d else (rules["race_in_top10"] if d in top10 else 0)
        per_slot[("race_pos", slot)] = pts
        per_field["race_pos"] += pts
    sp = res.get("sprint_pos", {})
    for slot, d in pred.get("sprint_pos", {}).items():
        if d and sp:
            pts = rules["sprint_exact"] if sp.get(slot) == d else 0
            per_slot[("sprint_pos", slot)] = pts
            per_field["sprint_pos"] += pts
    for f in ("pole", "fl", "dotd", "fp", "sprint_pole"):
        d = pred.get(f, {}).get(1)
        actual = res.get(f, {}).get(1)
        if d and actual:
            pts = rules[f] if d.lower() == actual.lower() else 0
            per_slot[(f, 1)] = pts
            per_field[f] += pts
    return sum(per_field.values()), dict(per_field), per_slot


def current_standings(year):
    return {r["position"]: r["tla"] for r in db.q("SELECT position, tla FROM standings WHERE year=?", year)}


def score_season(pred, standings, rules):
    per_slot = {}
    for slot, d in pred.get(SEASON_FIELD, {}).items():
        if d and standings.get(slot):
            per_slot[slot] = rules["season_exact"] if standings[slot] == d else 0
    return sum(per_slot.values()), per_slot


def event_scores(ev):
    """{user_id: (points, detail)} for an event: frozen snapshot or computed now."""
    if ev["frozen"] or ev["imported"]:
        return {r["user_id"]: (r["points"], json.loads(r["detail"] or "{}"))
                for r in db.q("SELECT * FROM scores WHERE event_id=?", ev["id"])}
    res = load_results(ev["id"])
    if not res:
        return {}
    rules = rules_for(ev["year"])
    preds = load_predictions(ev["id"])
    out = {}
    for uid, p in preds.items():
        tot, per_field, _ = score_event(p, res, rules)
        out[uid] = (tot, per_field)
    return out


def freeze_event(ev):
    scores = event_scores(dict(ev) | {"frozen": 0, "imported": 0})
    with db.tx() as c:
        c.execute("DELETE FROM scores WHERE event_id=?", (ev["id"],))
        for uid, (pts, detail) in scores.items():
            c.execute("INSERT INTO scores(user_id,event_id,year,points,detail) VALUES(?,?,?,?,?)",
                      (uid, ev["id"], ev["year"], pts, json.dumps(detail)))
        c.execute("UPDATE events SET frozen=1 WHERE id=?", (ev["id"],))
    db.logmsg("info", f"Froze scores for {ev['year']} {ev['name']}")


def season_prediction_scores(year):
    s = db.q1("SELECT * FROM seasons WHERE year=?", year)
    if s and (s["imported"] or s["final"]):
        rows = db.q("SELECT user_id, points FROM season_scores WHERE year=?", year)
        if rows or s["imported"]:
            return {r["user_id"]: r["points"] for r in rows}
    stand = current_standings(year)
    rules = rules_for(year)
    preds = load_predictions(0, year)
    return {uid: score_season(p, stand, rules)[0] for uid, p in preds.items()}


def leaderboard(year):
    """Rows sorted by total with tie-break = most race-weekend 'wins'."""
    users = db.q("SELECT id, name FROM users WHERE active=1 OR id IN "
                 "(SELECT user_id FROM scores WHERE year=?) ORDER BY name", year)
    events = db.q("SELECT * FROM events WHERE year=? AND cancelled=0 ORDER BY round", year)
    season = season_prediction_scores(year)
    pens = defaultdict(int)
    pen_ev = defaultdict(int)
    for r in db.q("SELECT p.user_id, p.event_id, p.points FROM penalties p JOIN events e ON e.id=p.event_id "
                  "WHERE e.year=?", year):
        pens[r["user_id"]] += r["points"]
        pen_ev[(r["user_id"], r["event_id"])] += r["points"]
    per_event = {}
    live_event = None
    for ev in events:
        sc = event_scores(ev)
        if sc:
            per_event[ev["id"]] = sc
            if not ev["frozen"] and not ev["imported"]:
                live_event = ev["id"]
    rows = []
    for u in users:
        uid = u["id"]
        ev_pts = {eid: sc.get(uid, (0, {}))[0] - pen_ev[(uid, eid)] for eid, sc in per_event.items()}
        total = season.get(uid, 0) + sum(ev_pts.values())
        before = total - ev_pts.get(live_event, 0) if live_event else total
        rows.append({"user_id": uid, "name": u["name"], "season": season.get(uid, 0),
                     "events": ev_pts, "penalties": pens[uid], "total": total,
                     "before": before, "wins": 0})
    # tie-break: number of weekends with the highest weekend score
    for eid in per_event:
        best = max((r["events"].get(eid, 0) for r in rows), default=0)
        if best > 0:
            for r in rows:
                if r["events"].get(eid, 0) == best:
                    r["wins"] += 1

    def rank(key):
        srt = sorted(rows, key=lambda r: (-r[key], -r["wins"], r["name"].lower()))
        pos = {}
        for i, r in enumerate(srt):
            prev = srt[i - 1] if i else None
            if prev and prev[key] == r[key] and prev["wins"] == r["wins"]:
                pos[r["user_id"]] = pos[prev["user_id"]]
            else:
                pos[r["user_id"]] = i + 1
        return srt, pos
    srt, pos = rank("total")
    _, pos_before = rank("before")
    if len({r["before"] for r in rows}) <= 1:
        pos_before = pos          # nothing to compare with yet
    for r in srt:
        r["rank"] = pos[r["user_id"]]
        r["move"] = pos_before[r["user_id"]] - r["rank"] if live_event else 0
    return srt, events, live_event
