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
# same order as PALETTE, darker so every line reads on white paper
PRINT = ["#d62f35", "#169c4b", "#c98f00", "#1f6fd1", "#8a2be2", "#e0660f", "#0f9e94", "#d13c9f",
         "#7f9c12", "#5a67d8", "#3c4352", "#a8692d", "#2f8f57", "#a33a3a", "#2b8ab3", "#7a3fb8"]
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


def _race_view(pts):
    """Turn season positions into race-only positions: points scored with this race's predictions
    (race top 10, fastest lap, DotD, pit stop) = season total now minus season total at the start.
    Ties keep the season order from the start."""
    base = {uid: (p[0][2], p[0][1]) for uid, p in pts.items()}        # (total, rank) at the first point
    by_x = {}
    for uid, p in pts.items():
        for x, _rk, tot in p:
            by_x.setdefault(x, []).append((uid, tot - base[uid][0]))
    out = {uid: [] for uid in pts}
    for x in sorted(by_x):
        for rank, (uid, got) in enumerate(sorted(by_x[x], key=lambda t: (-t[1], base[t[0]][1])), start=1):
            out[uid].append([x, rank, got])
    return out


def chart(year, me=None, event_id=None, mode="season"):
    """Data for the bump chart of one race, by default the latest of the season with recorded laps.
    mode 'season': position in the season standings; 'race': position on this race's points only."""
    if event_id:
        ev = db.q1("SELECT e.* FROM events e JOIN race_laps l ON l.event_id=e.id WHERE e.id=?", event_id)
    else:
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
    if mode == "race":
        pts = _race_view(pts)
    names = {u["id"]: u["name"] for u in db.q("SELECT id, name FROM users")}
    n = max(len(pts), max(p[1] for v in pts.values() for p in v))
    players = []
    for i, uid in enumerate(sorted(pts)):
        p = pts[uid]
        first, last = p[0], p[-1]
        players.append({
            "id": uid, "name": names.get(uid, "?"), "colour": PALETTE[i % len(PALETTE)],
            "print": PRINT[i % len(PRINT)], "me": uid == me, "pts": p,
            "rank": last[1], "total": last[2],
            "move": first[1] - last[1] if first[0] == 0 and mode == "season" else 0,
            "path": " ".join(f"{x},{(rk - 0.5) * ROW:g}" for x, rk, _ in p)})
    players.sort(key=lambda r: r["rank"])
    step = 5 if total <= 40 else 10
    ticks = [0] + [t for t in range(step, total, step) if total - t >= step * 0.6] + [total]
    return {"ev": ev, "mode": mode, "n": n, "row": ROW, "w": end_x, "h": n * ROW, "players": players,
            "current": laps["current"], "total": laps["total"], "finished": finished, "ticks": ticks}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def report_svg(event_id, mode="season"):
    """Finished race only: the bump chart as a self-contained SVG for the PDF report (A4 landscape width)."""
    ev = db.q1("SELECT year FROM events WHERE id=?", event_id)
    c = chart(ev["year"], event_id=event_id, mode=mode) if ev else None
    if not c or not c["finished"]:
        return None
    row = max(10, min(34, 520 // c["n"]))         # fill the page, whatever the number of players
    left, right, top, W = 22, 150, 4, 1000
    pw, ph = W - left - right, c["n"] * row
    H = top + ph + 18
    X = lambda x: left + x / c["w"] * pw          # noqa: E731
    Y = lambda rk: top + (rk - 0.5) * row         # noqa: E731
    font = 'font-family="BL, DejaVu Sans, sans-serif"'
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" {font}>']
    for t in c["ticks"]:
        out.append(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{top}" y2="{top + ph}" stroke="#e1e4ea" stroke-width="0.8"/>')
        lab = "Start" if t == 0 else t
        anchor = "start" if t == 0 else "middle"
        out.append(f'<text x="{X(t):.1f}" y="{top + ph + 13}" font-size="9" fill="#5b6475" text-anchor="{anchor}">{lab}</text>')
    out.append(f'<text x="{X(c["w"]):.1f}" y="{top + ph + 13}" font-size="9" fill="#5b6475" text-anchor="end" font-weight="600">Final</text>')
    for i in range(1, c["n"] + 1):
        out.append(f'<text x="{left - 6}" y="{Y(i) + 3.2:.1f}" font-size="9" fill="#5b6475" text-anchor="end">{i}</text>')
    for p in reversed(c["players"]):
        pts = " ".join(f"{X(x):.1f},{Y(rk):.1f}" for x, rk, _ in p["pts"])
        out.append(f'<polyline points="{pts}" fill="none" stroke="{p["print"]}" stroke-width="2" '
                   'stroke-linejoin="round" stroke-linecap="round"/>')
    for p in c["players"]:
        y = Y(p["rank"])
        mv = ""
        if p["move"] > 0:
            mv = f'<tspan fill="#178a43" font-weight="600"> ▲{p["move"]}</tspan>'
        elif p["move"] < 0:
            mv = f'<tspan fill="#c62f35" font-weight="600"> ▼{-p["move"]}</tspan>'
        out.append(f'<circle cx="{left + pw + 9}" cy="{y:.1f}" r="3.6" fill="{p["print"]}"/>'
                   f'<text x="{left + pw + 17}" y="{y + 3.3:.1f}" font-size="10" fill="#141821" font-weight="600">'
                   f'{_esc(p["name"])}{mv}</text>'
                   f'<text x="{W - 2}" y="{y + 3.3:.1f}" font-size="10" fill="#5b6475" text-anchor="end">'
                   f'{"+" if mode == "race" and p["total"] > 0 else ""}{p["total"]}</text>')
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------- backfill ---
REPLAY_TOPICS = ["DriverList", "SessionStatus", "TimingData", "TimingStats", "PitStopSeries", "PitStop"]


def missing_laps(event_id):
    have = {r["lap"] for r in db.q("SELECT DISTINCT lap FROM player_laps WHERE event_id=? AND lap>0", event_id)}
    return sorted(set(range(1, max(have))) - have) if have else []


def _stream(text):
    """[(seconds since session start, message)] from a .jsonStream file."""
    import json
    out = []
    for line in text.lstrip("﻿").splitlines():
        line = line.strip().lstrip("﻿")
        if len(line) < 13:
            continue
        try:
            h, m, s = line[:12].split(":")
            out.append((int(h) * 3600 + int(m) * 60 + float(s), json.loads(line[12:])))
        except (ValueError, IndexError):
            continue
    return out


def backfill(http, event_id, path):
    """Replay the race from the F1 archive and fill laps the live recording missed. Returns laps added."""
    from . import livetiming as lt
    ev = db.q1("SELECT * FROM events WHERE id=?", event_id)
    missing = set(missing_laps(event_id))
    if not ev or not path or not missing:
        return 0
    msgs, seen = [], {}
    for order, topic in enumerate(REPLAY_TOPICS):
        text = http.get(f"{path}{topic}.jsonStream")
        seen[topic] = getattr(http, "last_status", "")
        if not text:
            if topic == "TimingData":
                return 0
            continue
        msgs += [(ts, order, topic, m) for ts, m in _stream(text)]
    msgs.sort(key=lambda x: (x[0], x[1]))

    board, _, _ = scoring.leaderboard(ev["year"], upto_round=ev["round"])
    pen = {}
    for p in db.q("SELECT user_id, points FROM penalties WHERE event_id=?", event_id):
        pen[p["user_id"]] = pen.get(p["user_id"], 0) + p["points"]
    pre = {k: v for k, v in scoring.load_results(event_id).items() if k not in RACE_FIELDS}
    preds, rules = scoring.load_predictions(event_id), scoring.rules_for(ev["year"])
    known = {e["number"]: {"tla": e["tla"], "team": e["team"]}
             for e in db.q("SELECT * FROM entrants WHERE event_id=?", event_id)}

    st, cur, snaps = lt.TimingState(), 0, {}
    laps_seen = []

    def snap(lap):
        data, pits = st.snapshot()
        data["LapCount"] = {"CurrentLap": lap + 1}      # end of `lap`: what the live feed would show
        res = dict(pre) | lt.derive("R", data, pits, known)
        rows = []
        for r in board:
            uid = r["user_id"]
            before = r["total"] - r["events"].get(event_id, 0)
            got = scoring.score_event(preds.get(uid, {}), res, rules)[0]
            rows.append({"user_id": uid, "name": r["name"], "wins": r["wins"], "total": before + got - pen.get(uid, 0)})
        snaps[lap] = _ordered(rows, "total")

    # lap number = laps completed by the leader (the archive's LapCount stream can stop early)
    for _ts, _o, topic, m in msgs:
        if topic == "LapCount":
            continue
        st.apply(topic, m)
        if topic != "TimingData" or not isinstance(m, dict):
            continue
        done = cur
        for ln in (m.get("Lines") or {}).values():
            try:
                done = max(done, int((ln or {}).get("NumberOfLaps") or 0))
            except (TypeError, ValueError, AttributeError):
                pass
        while cur < done:
            cur += 1
            laps_seen.append(cur)
            if cur in missing:
                snap(cur)                      # leader just completed lap `cur`
    counts = {t: sum(1 for x in msgs if x[2] == t) for t in ("TimingData",)}
    db.logmsg("info", f"Lap chart replay: archive laps {min(laps_seen, default=0)}-{max(laps_seen, default=0)} "
                      f"messages {counts}, missing laps "
                      f"{min(missing)}-{max(missing)} ({len(missing)}), filled {sorted(snaps)[:10]}, "
                      f"source {seen.get('TimingData')}")
    if not snaps:
        return 0
    with db.tx() as c:
        for lap, rows in snaps.items():
            for rank, r in rows:
                c.execute("INSERT OR IGNORE INTO player_laps VALUES(?,?,?,?,?)",
                          (event_id, lap, r["user_id"], rank, r["total"]))
    return len(snaps)


def _lap_positions(data):
    """{lap: [racing numbers in order]} from the live LapSeries topic (every driver's position per lap)."""
    series = data.get("LapSeries") or {}
    per = {}
    for num, d in series.items():
        if not isinstance(d, dict):
            continue                       # e.g. the feed's '_kf' keyframe flag
        lp = d.get("LapPosition") or {}
        items = lp.items() if isinstance(lp, dict) else enumerate(lp)
        for k, v in items:
            try:
                per.setdefault(int(k), []).append((int(v), str(num)))
            except (TypeError, ValueError):
                continue
    if not per:
        return {}
    # index 0 is the grid when the leader's list is one longer than his completed laps
    done = 0
    for ln in ((data.get("TimingData") or {}).get("Lines") or {}).values():
        if isinstance(ln, dict):
            try:
                done = max(done, int(ln.get("NumberOfLaps") or 0))
            except (TypeError, ValueError):
                pass
    shift = 0 if max(per) + 1 > done else 1
    return {k + shift: [n for _p, n in sorted(v)] for k, v in per.items() if k + shift >= 1}


def backfill_series(event_id, data):
    """Fill missed laps from the live feed: positions from LapSeries; fastest lap and fastest pit stop
    only where the feed shows they had already happened by that lap. Returns laps added."""
    missing = set(missing_laps(event_id))
    order = _lap_positions(data)
    laps = sorted(missing & set(order))
    if not laps:
        return 0
    ev = db.q1("SELECT * FROM events WHERE id=?", event_id)
    known = {e["number"]: e for e in db.q("SELECT * FROM entrants WHERE event_id=?", event_id)}
    tla = lambda n: known[n]["tla"] if n in known else None          # noqa: E731
    # fastest lap: current holder counts from the lap he set it on
    fl_who, fl_lap = None, None
    for num, ln in (((data.get("TimingStats") or {}).get("Lines")) or {}).items():
        pb = ln.get("PersonalBestLapTime") if isinstance(ln, dict) else None
        if isinstance(pb, dict) and str(pb.get("Position")) == "1" and pb.get("Lap"):
            try:
                fl_who, fl_lap = tla(str(num)), int(pb["Lap"])
            except (TypeError, ValueError):
                pass
    stops = []                                                         # (lap, seconds, team)
    for num, ss in ((((data.get("PitStopSeries") or {}).get("PitTimes")) or {}).items()):
        for st in ss.values() if isinstance(ss, dict) else []:
            p = st.get("PitStop") if isinstance(st, dict) else None
            if not isinstance(p, dict):
                continue
            try:
                t, lap = float(p.get("PitStopTime")), int(p.get("Lap"))
            except (TypeError, ValueError):
                continue
            if t > 0 and str(num) in known and known[str(num)]["team"]:
                stops.append((lap, t, known[str(num)]["team"]))

    board, _, _ = scoring.leaderboard(ev["year"], upto_round=ev["round"])
    pen = {}
    for p in db.q("SELECT user_id, points FROM penalties WHERE event_id=?", event_id):
        pen[p["user_id"]] = pen.get(p["user_id"], 0) + p["points"]
    pre = {k: v for k, v in scoring.load_results(event_id).items() if k not in RACE_FIELDS}
    preds, rules = scoring.load_predictions(event_id), scoring.rules_for(ev["year"])
    with db.tx() as c:
        for lap in laps:
            res = dict(pre)
            res["race_pos"] = {i + 1: t for i, t in enumerate(filter(None, map(tla, order[lap]))) if i < 20}
            if fl_who and fl_lap and fl_lap <= lap and lap >= 2:
                res["fl"] = {1: fl_who}
            done = [s for s in stops if s[0] <= lap]
            if done:
                res["fp"] = {1: min(done, key=lambda s: s[1])[2]}
            rows = []
            for r in board:
                uid = r["user_id"]
                before = r["total"] - r["events"].get(event_id, 0)
                got = scoring.score_event(preds.get(uid, {}), res, rules)[0]
                rows.append({"user_id": uid, "name": r["name"], "wins": r["wins"],
                             "total": before + got - pen.get(uid, 0)})
            for rank, r in _ordered(rows, "total"):
                c.execute("INSERT OR IGNORE INTO player_laps VALUES(?,?,?,?,?)",
                          (event_id, lap, r["user_id"], rank, r["total"]))
    return len(laps)
