"""PDF reports (A4 landscape) rendered from HTML with WeasyPrint."""
import logging
import os
import time
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, scoring, standings

log = logging.getLogger("reports")
logging.getLogger("fontTools").setLevel(logging.WARNING)
logging.getLogger("weasyprint").setLevel(logging.ERROR)
HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("F1POOL_DATA", "/data"))
FLAG_DIR = DATA_DIR / "flags"
_env = Environment(loader=FileSystemLoader(HERE / "templates" / "reports"), autoescape=select_autoescape())
_flag_fail = {}

KINDS = {
    "players": "Paddock Club standings",
    "drivers": "Drivers' championship",
    "constructors": "Constructors' championship",
}


def flag_uri(code):
    """Local copy of a small country flag (downloaded once), or None."""
    if not code:
        return None
    path = FLAG_DIR / f"{code}.png"
    if path.exists():
        return path.as_uri()
    if time.time() - _flag_fail.get(code, 0) < 3600:
        return None
    try:
        r = requests.get(f"https://flagcdn.com/w40/{code}.png", timeout=5)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            FLAG_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            return path.as_uri()
    except requests.RequestException:
        pass
    _flag_fail[code] = time.time()
    return None


def _short(name):
    from .main import short
    return short(name)


def _context(kind, year, upto_round):
    ev = db.q1("SELECT * FROM events WHERE year=? AND round=?", year, upto_round)
    tables = standings.season_tables(year, upto_round)
    for e in tables["events"]:
        e["flag"] = flag_uri(e["iso2"])
        e["short"] = _short(e["name"])
    ctx = {"kind": kind, "title": KINDS[kind], "year": year, "event": ev, "tables": tables,
           "generated": time.strftime("%d-%m-%Y %H:%M"), "colours": _colours(year)}
    if kind == "players":
        board, events, _ = scoring.leaderboard(year, upto_round)
        played = sorted(e["round"] for e in events if any(e["id"] in r["events"] for r in board))
        before = [r for r in played if r < upto_round]
        prev = scoring.leaderboard(year, before[-1])[0] if before else board
        prev_rank = {r["user_id"]: r["rank"] for r in prev}
        for r in board:
            r["move"] = prev_rank.get(r["user_id"], r["rank"]) - r["rank"]
        evs = [dict(e) | {"short": _short(e["name"]), "flag": flag_uri(standings.iso2(dict(e))),
                          "best": max((r["events"].get(e["id"], 0) for r in board), default=0)}
               for e in events if any(e["id"] in r["events"] for r in board)]
        ctx.update(board=board, pevents=evs)
    return ctx


def _colours(year):
    out = {}
    for r in db.q("SELECT x.tla, x.team, x.colour FROM entrants x JOIN events e ON e.id=x.event_id "
                  "WHERE e.year=? AND x.colour!='' ORDER BY e.round", year):
        out[r["tla"]] = r["colour"]
        out[r["team"]] = r["colour"]
    return out


def render_html(kind, year, upto_round):
    return _env.get_template(f"{kind}.html").render(**_context(kind, year, upto_round),
                                                   static=(HERE / "static").as_uri())


def render_pdf(kind, year, upto_round):
    from weasyprint import HTML
    html = render_html(kind, year, upto_round)
    return HTML(string=html, base_url=str(HERE / "static")).write_pdf()


def filename(kind, year, upto_round):
    ev = db.q1("SELECT name FROM events WHERE year=? AND round=?", year, upto_round)
    name = (ev["name"] if ev else f"Round {upto_round}").replace(" ", "_")
    label = {"players": "Paddock_Club_standings", "drivers": "Drivers_championship",
             "constructors": "Constructors_championship"}[kind]
    return f"{year}_{upto_round:02d}_{name}_-_{label}.pdf"
