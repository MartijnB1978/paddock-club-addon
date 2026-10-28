"""Drivers' and constructors' championship tables, per weekend, optionally 'as of' a round."""
from collections import defaultdict

from . import db, scoring

RACE_PTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
SPRINT_PTS = [8, 7, 6, 5, 4, 3, 2, 1]

ISO2 = {
    "australia": "au", "china": "cn", "japan": "jp", "bahrain": "bh", "saudi arabia": "sa", "usa": "us",
    "united states": "us", "miami": "us", "canada": "ca", "monaco": "mc", "spain": "es", "austria": "at",
    "great britain": "gb", "united kingdom": "gb", "uk": "gb", "belgium": "be", "hungary": "hu",
    "netherlands": "nl", "italy": "it", "azerbaijan": "az", "singapore": "sg", "mexico": "mx",
    "brazil": "br", "qatar": "qa", "united arab emirates": "ae", "uae": "ae", "abu dhabi": "ae",
    "malaysia": "my", "portugal": "pt", "turkey": "tr", "france": "fr", "germany": "de", "russia": "ru",
    "las vegas": "us", "emilia romagna": "it", "argentina": "ar", "south africa": "za", "korea": "kr",
}


def iso2(ev):
    for key in (ev.get("location") or "", ev.get("country") or ""):
        k = key.lower()
        if k in ISO2:
            return ISO2[k]
    if "sepang" in (ev.get("location") or "").lower() or "kuala" in (ev.get("location") or "").lower():
        return "my"
    return ISO2.get((ev.get("country") or "").lower(), "")


def completed_events(year, upto_round=None):
    out = []
    for ev in db.q("SELECT * FROM events WHERE year=? AND cancelled=0 ORDER BY round", year):
        if upto_round is not None and ev["round"] > upto_round:
            continue
        if db.q1("SELECT 1 FROM results WHERE event_id=? AND field IN ('race_pos','sprint_pos') LIMIT 1", ev["id"]):
            out.append(dict(ev) | {"iso2": iso2(dict(ev))})
    return out


def season_tables(year, upto_round=None):
    events = completed_events(year, upto_round)
    latest = events[-1]["round"] if events else None
    use_official = upto_round is None or upto_round >= (latest or 0)

    # team of each driver per weekend (official entry list), fallback: latest known team in the season
    last_team, ev_team, info = {}, defaultdict(dict), {}
    for r in db.q("SELECT x.*, e.round FROM entrants x JOIN events e ON e.id=x.event_id WHERE e.year=? "
                  "ORDER BY e.round", year):
        if r["team"]:
            ev_team[r["event_id"]][r["tla"]] = r["team"]
            last_team[r["tla"]] = r["team"]
        if r["colour"] or r["tla"] not in info:
            info[r["tla"]] = dict(r)

    drv = defaultdict(lambda: {"events": {}, "pos": {}, "teams": {}})
    teams = defaultdict(lambda: {"events": defaultdict(int), "drivers": defaultdict(lambda: defaultdict(int))})
    for ev in events:
        res = scoring.load_results(ev["id"])
        got = defaultdict(int)
        for slot, tla in res.get("race_pos", {}).items():
            drv[tla]["pos"][ev["id"]] = slot
            if 1 <= slot <= 10:
                got[tla] += RACE_PTS[slot - 1]
        for slot, tla in res.get("sprint_pos", {}).items():
            if 1 <= slot <= 8:
                got[tla] += SPRINT_PTS[slot - 1]
        for tla in set(got) | set(res.get("race_pos", {}).values()):
            pts = got.get(tla, 0)
            drv[tla]["events"][ev["id"]] = pts
            team = ev_team[ev["id"]].get(tla) or last_team.get(tla) or ""
            drv[tla]["teams"][ev["id"]] = team
            if team:
                teams[team]["events"][ev["id"]] += pts
                teams[team]["drivers"][tla][ev["id"]] += pts

    official = {}
    team_official = {}
    if use_official:
        official = {r["tla"]: (r["position"], r["points"]) for r in db.q(
            "SELECT position, tla, points FROM standings WHERE year=?", year)}
        team_official = {r["team"]: (r["position"], r["points"]) for r in db.q(
            "SELECT position, team, points FROM team_standings WHERE year=?", year)}

    drivers = []
    for tla in set(drv) | set(official):
        d = drv[tla]
        calc = sum(d["events"].values())
        pos, pts = official.get(tla, (None, None))
        drivers.append({"tla": tla, "points": pts if pts is not None else calc, "official_pos": pos,
                        "events": d["events"], "pos": d["pos"],
                        "team": last_team.get(tla, ""), "name": (info.get(tla) or {}).get("name", tla)})
    drivers.sort(key=lambda r: (r["official_pos"] is None, r["official_pos"] or 0, -(r["points"] or 0),
                                min(r["pos"].values() or [99])))
    for i, r in enumerate(drivers, start=1):
        r["rank"] = i

    team_rows = []
    for name, t in teams.items():
        calc = sum(t["events"].values())
        off = _match_team(name, team_official)
        team_rows.append({"team": name, "points": off[1] if off else calc, "official_pos": off[0] if off else None,
                          "events": dict(t["events"]),
                          "drivers": sorted(({"tla": k, "events": dict(v), "points": sum(v.values()),
                                              "name": (info.get(k) or {}).get("name", k)}
                                             for k, v in t["drivers"].items()), key=lambda x: -x["points"])})
    team_rows.sort(key=lambda r: (r["official_pos"] is None, r["official_pos"] or 0, -(r["points"] or 0)))
    for i, r in enumerate(team_rows, start=1):
        r["rank"] = i
    return {"events": events, "drivers": drivers, "teams": team_rows, "official": bool(official)}


def _match_team(name, official):
    if not official:
        return None
    if name in official:
        return official[name]
    low = name.lower()
    for k, v in official.items():
        if k.lower() in low or low in k.lower():
            return v
    return None
