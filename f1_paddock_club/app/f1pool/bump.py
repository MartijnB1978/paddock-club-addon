"""Players' positions lap by lap through a race: recorded live, drawn as a bump chart on the standings page."""
from . import db, scoring

SCHEMA = """
CREATE TABLE IF NOT EXISTS race_laps(event_id INTEGER PRIMARY KEY, current INTEGER, total INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS player_laps(
  event_id INTEGER, lap INTEGER, user_id INTEGER, rank INTEGER, total INTEGER,
  PRIMARY KEY(event_id, lap, user_id));
"""

RACE_FIELDS = ("race_pos", "fl", "dotd", "fp")   # what the race itself decides; the rest is known before it
PALETTE = ["#ff5a5f", "#2fd26f", "#ffc83d", "#4da3ff", "#b14dff", "#ff8a3d", "#2ee6d6", "#ff6fcf",
           "#c8e64d", "#9aa7ff", "#ffffff", "#e6a15c", "#5cd68a", "#d65c5c", "#5cc2e6", "#a35ce6"]
ROW = 26          # px per position, shared by the SVG and the labels


def init():
    db.conn().executescript(SCHEMA)


def _ordered(rows, key):
    rows = sorted(rows, key=lambda r: (-r[key], -r.get("wins", 0), r["name"].lower()))
    return [(i + 1, r) for i, r in enumerate(rows)]


def _start_rows(ev, board):
    """Totals just before lights out: season so far plus this weekend's qualifying / sprint points."""
    res = {k: v for k, v in scoring.load_results(ev["id"]).items() if k not in RACE_FIELDS}
    rules = scoring.rules_for(ev["year"])
    preds = scoring.load_predictions(ev["id"])
    out = []
    for r in board:
        pre = scoring.score_event(preds.get(r["user_id"], {}), res, rules)[0] if res else 0
        out.append(r | {"start": r["before"] + pre})
    return out


def record(event_id, lap, total):
    ev = db.q1("SELECT * FROM events WHERE id=?", event_id)
    if not ev:
        return
    board, _, _ = scoring.leaderboard(ev["year"], upto_round=ev["round"])
    if not board:
        return
    have_start = db.q1("SELECT 1 FROM player_laps WHERE event_id=? AND lap=0 LIMIT 1", event_id)
    start = None if have_start else _ordered(_start_rows(ev, board), "start")
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO race_laps VALUES(?,?,?,?)", (event_id, lap, total, db.iso(db.now_utc())))
        if start:
            for rank, r in start:
                c.execute("INSERT OR REPLACE INTO player_laps VALUES(?,?,?,?,?)",
                          (event_id, 0, r["user_id"], rank, r["start"]))
        if lap > 0:
            for rank, r in _ordered(board, "total"):
                c.execute("INSERT OR REPLACE INTO player_laps VALUES(?,?,?,?,?)",
                          (event_id, lap, r["user_id"], rank, r["total"]))


def chart(year, me=None):
    """Data for the bump chart of the latest race of the season that has recorded laps."""
    ev = db.q1("SELECT e.* FROM events e JOIN race_laps l ON l.event_id=e.id WHERE e.year=? "
               "ORDER BY e.round DESC LIMIT 1", year)
    if not ev:
        return None
    laps = db.q1("SELECT * FROM race_laps WHERE event_id=?", ev["id"])
    race = db.q1("SELECT * FROM sessions WHERE event_id=? AND kind='R'", ev["id"])
    finished = bool(race and race["finished_at"])
    total = laps["total"] or laps["current"] or 1
    pts = {}
    for r in db.q("SELECT lap, user_id, rank, total FROM player_laps WHERE event_id=? ORDER BY lap", ev["id"]):
        pts.setdefault(r["user_id"], []).append([r["lap"], r["rank"], r["total"]])
    if not pts:
        return None
    end_x = total
    if finished:            # after the flag: standings including DotD, pit stop and any corrections
        end_x = total + max(2, round(total * 0.08))
        board, _, _ = scoring.leaderboard(year, upto_round=ev["round"])
        for rank, r in _ordered(board, "total"):
            if r["user_id"] in pts:
                pts[r["user_id"]].append([end_x, rank, r["total"]])
    names = {u["id"]: u["name"] for u in db.q("SELECT id, name FROM users")}
    n = max(len(pts), max(p[1] for v in pts.values() for p in v))
    players = []
    for i, uid in enumerate(sorted(pts)):
        p = pts[uid]
        first, last = p[0], p[-1]
        players.append({
            "id": uid, "name": names.get(uid, "?"), "colour": PALETTE[i % len(PALETTE)], "me": uid == me,
            "rank": last[1], "total": last[2], "move": first[1] - last[1] if first[0] == 0 else 0,
            "path": " ".join(f"{x},{(rk - 0.5) * ROW:g}" for x, rk, _ in p)})
    players.sort(key=lambda r: r["rank"])
    step = 5 if total <= 40 else 10
    ticks = [0] + [t for t in range(step, total, step) if total - t >= step * 0.6] + [total]
    return {"ev": ev, "n": n, "row": ROW, "w": end_x, "h": n * ROW, "players": players,
            "current": laps["current"], "total": laps["total"], "finished": finished, "ticks": ticks}
