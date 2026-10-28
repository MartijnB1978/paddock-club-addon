"""Paddock Club Reeuwijk – F1 prediction game."""
import asyncio
import json
import logging
import os
import secrets
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, importer, mailer, reports, scoring, standings
from .engine import Engine
from .schedule import SCORED

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

DATA_DIR = Path(os.environ.get("F1POOL_DATA", "/data"))
TZ = ZoneInfo(os.environ.get("TZ_DISPLAY", "Europe/Amsterdam"))
HERE = Path(__file__).parent

db.init()
_secret_file = DATA_DIR / "secret.key"
if not _secret_file.exists():
    _secret_file.parent.mkdir(parents=True, exist_ok=True)
    _secret_file.write_text(secrets.token_urlsafe(48))
    os.chmod(_secret_file, 0o600)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=_secret_file.read_text().strip(),
                   session_cookie="pcr_session", max_age=60 * 60 * 24 * 60, same_site="lax",
                   https_only=os.environ.get("COOKIE_SECURE", "1") == "1")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
engine = Engine(str(DATA_DIR))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def bootstrap_admin():
    """Make sure the configured admin exists; print a setup link while it has no password."""
    name = (os.environ.get("ADMIN_NAME") or "").strip()
    email = (os.environ.get("ADMIN_EMAIL") or "").strip() or None
    if not name:
        return
    u = db.q1("SELECT * FROM users WHERE lower(name)=lower(?)", name)
    if not u:
        db.ex("INSERT INTO users(name,email,is_admin,created_at) VALUES(?,?,1,?)", name, email, db.iso(db.now_utc()))
        u = db.q1("SELECT * FROM users WHERE name=?", name)
    db.ex("UPDATE users SET is_admin=1, active=1, email=COALESCE(email, ?) WHERE id=?", email, u["id"])
    if not u["pw_hash"]:
        tok = auth.make_token(u["id"], "invite", 24 * 7)
        base = mailer.base_url() or "https://<your-address>"
        log.warning("=" * 70)
        log.warning("ADMIN SETUP: open this link to set the password for %s:", name)
        log.warning("%s/invite/%s", base, tok)
        log.warning("=" * 70)


@app.on_event("startup")
def _startup():
    if os.environ.get("BASE_URL") and not db.setting("base_url"):
        db.set_setting("base_url", os.environ["BASE_URL"].rstrip("/"))
    bootstrap_admin()
    if os.environ.get("F1POOL_NO_ENGINE") != "1":
        engine.start()


# ------------------------------------------------------------------ helpers
def localtime(s, fmt="%a %d %b %H:%M"):
    dt = db.parse_iso(s) if isinstance(s, str) else s
    return dt.astimezone(TZ).strftime(fmt) if dt else "–"


templates.env.filters["localtime"] = localtime
SHORT = {"united states": "USA", "mexico city": "MEX", "sao paulo": "BRA", "são paulo": "BRA", "las vegas": "LVG",
         "abu dhabi": "ABU", "emilia romagna": "IMO", "saudi arabian": "SAU", "british": "GBR", "dutch": "NED",
         "austrian": "AUT", "australian": "AUS", "barcelona": "BCN", "spanish": "ESP", "hungarian": "HUN",
         "belgian": "BEL", "italian": "ITA", "japanese": "JPN", "chinese": "CHN", "canadian": "CAN",
         "singapore": "SIN", "azerbaijan": "AZE", "qatar": "QAT", "bahrain": "BHR", "monaco": "MON",
         "miami": "MIA", "madrid": "MAD", "portuguese": "POR", "turkish": "TUR"}


def short(name):
    n = (name or "").lower().replace("grand prix", "").strip()
    return SHORT.get(n, n[:3].upper())


templates.env.filters["short"] = short
templates.env.globals["LABELS"] = scoring.LABELS


def current_user(request):
    uid = request.session.get("uid")
    if not uid:
        return None
    u = db.q1("SELECT * FROM users WHERE id=? AND active=1", uid)
    return u


def render(request, name, **ctx):
    user = current_user(request)
    ctx.update(request=request, user=user, csrf=auth.csrf_token(request.session),
               year=ctx.get("year") or default_year(), flash=request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def flash(request, msg, kind="ok"):
    request.session["flash"] = {"msg": msg, "kind": kind}


def redirect(url):
    return RedirectResponse(url, status_code=303)


def default_year():
    now = db.now_utc()
    r = db.q1("SELECT MAX(year) AS y FROM events WHERE year<=? AND cancelled=0", now.year)
    return r["y"] if r and r["y"] else now.year


def require_user(request):
    u = current_user(request)
    if not u:
        raise LoginRequired()
    return u


def require_admin(request):
    u = require_user(request)
    if not u["is_admin"]:
        raise LoginRequired()
    return u


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_required(request, exc):
    return redirect(f"/login?next={request.url.path}")


async def form_checked(request):
    form = await request.form()
    if not auth.csrf_ok(request.session, form.get("csrf")):
        raise LoginRequired()
    return form


def sessions_of(event_id):
    return {s["kind"]: s for s in db.q("SELECT * FROM sessions WHERE event_id=?", event_id)}


def lock_utc(event_id, field):
    kind = scoring.FIELDS[field][1]
    s = db.q1("SELECT start_utc FROM sessions WHERE event_id=? AND kind=?", event_id, kind)
    return db.parse_iso(s["start_utc"]) if s and s["start_utc"] else None


def is_locked(event_id, field):
    t = lock_utc(event_id, field)
    return t is None or db.now_utc() >= t


def season_lock(year):
    s = db.q1("SELECT season_lock_utc FROM seasons WHERE year=?", year)
    if s and s["season_lock_utc"]:
        return db.parse_iso(s["season_lock_utc"])
    r = db.q1("SELECT MIN(s.start_utc) AS t FROM sessions s JOIN events e ON e.id=s.event_id "
              "WHERE e.year=? AND e.cancelled=0 AND s.kind='R'", year)
    return db.parse_iso(r["t"]) if r and r["t"] else None


def entrants(event_id, year=None):
    rows = db.q("SELECT * FROM entrants WHERE event_id=? ORDER BY team, name", event_id)
    if not rows and year:
        ev = db.q1("SELECT e.id FROM events e WHERE e.year=? AND EXISTS(SELECT 1 FROM entrants x WHERE "
                   "x.event_id=e.id) ORDER BY e.round DESC LIMIT 1", year)
        if ev:
            rows = db.q("SELECT * FROM entrants WHERE event_id=? ORDER BY team, name", ev["id"])
    return rows


def driver_map(year):
    """tla -> latest entrant info for the season (for display)."""
    out = {}
    for r in db.q("SELECT x.* FROM entrants x JOIN events e ON e.id=x.event_id WHERE e.year=? "
                  "ORDER BY e.round", year):
        out[r["tla"]] = dict(r)
    return out


def team_map(year):
    return {t["name"]: dict(t) for t in db.q("SELECT * FROM teams WHERE year=?", year)}


def next_event(year=None):
    now = db.now_utc()
    return db.q1("SELECT e.* FROM events e JOIN sessions s ON s.event_id=e.id AND s.kind='R' "
                 "WHERE e.cancelled=0 AND e.imported=0 AND s.start_utc>=? ORDER BY s.start_utc LIMIT 1",
                 db.iso(now - timedelta(hours=3)))


def upcoming_deadlines(limit=4):
    now = db.now_utc()
    return db.q("SELECT s.*, e.name AS ev_name, e.id AS ev_id FROM sessions s JOIN events e ON e.id=s.event_id "
                "WHERE e.cancelled=0 AND s.kind IN ('SQ','S','Q','R') AND s.start_utc>=? "
                "ORDER BY s.start_utc LIMIT ?", db.iso(now), limit)


def live_panel():
    """The session currently/last shown on the home page."""
    sid = engine.live_session
    s = None
    if sid:
        s = db.q1("SELECT s.*, e.name AS ev_name, e.year FROM sessions s JOIN events e ON e.id=s.event_id "
                  "WHERE s.id=?", sid)
    if not s:
        s = db.q1("SELECT s.*, e.name AS ev_name, e.year FROM sessions s JOIN events e ON e.id=s.event_id "
                  "WHERE s.kind IN ('SQ','S','Q','R') AND s.status!='scheduled' AND e.imported=0 "
                  "ORDER BY s.start_utc DESC LIMIT 1")
    if not s:
        return None
    field = {"R": "race_pos", "S": "sprint_pos", "Q": "pole", "SQ": "sprint_pole"}[s["kind"]]
    rows = db.q("SELECT slot, value FROM results WHERE event_id=? AND field=? ORDER BY slot",
                s["event_id"], field)
    extra = {r["field"]: r["value"] for r in db.q(
        "SELECT field, value FROM results WHERE event_id=? AND field IN ('fl','fp','dotd') AND slot=1",
        s["event_id"])} if s["kind"] == "R" else {}
    return {"session": s, "rows": rows, "extra": extra,
            "is_live": bool(sid and sid == s["id"]), "status": engine.live_status}


# ------------------------------------------------------------------ public
@app.get("/", response_class=HTMLResponse)
def home(request: Request, year: int | None = None):
    year = year or default_year()
    board, events, live_ev = scoring.leaderboard(year)
    return render(request, "home.html", year=year, board=board, events=events, live_ev=live_ev,
                  panel=live_panel(), deadlines=upcoming_deadlines(), drivers=driver_map(year),
                  teams=team_map(year), years=[r["year"] for r in db.q(
                      "SELECT DISTINCT year FROM events ORDER BY year DESC")])


@app.get("/fragment/board", response_class=HTMLResponse)
def board_fragment(request: Request, year: int | None = None):
    year = year or default_year()
    board, events, live_ev = scoring.leaderboard(year)
    return templates.TemplateResponse(request, "_board.html", {
        "request": request, "board": board, "live_ev": live_ev, "panel": live_panel(),
        "drivers": driver_map(year), "teams": team_map(year), "events": events})


@app.get("/stream")
async def stream(request: Request):
    async def gen():
        last = -1
        while True:
            if await request.is_disconnected():
                break
            if engine.version != last:
                last = engine.version
                yield f"data: {last}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(2 if engine.live else 10)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    return render(request, "rules.html", r=scoring.RULES["v2"])


# ------------------------------------------------------------------ auth
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", next=next)


@app.post("/login")
async def login(request: Request):
    form = await form_checked(request)
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    name = (form.get("name") or "").strip()
    if auth.throttled(ip) or auth.throttled(name.lower()):
        flash(request, "Too many attempts. Wait 15 minutes and try again.", "err")
        return redirect("/login")
    u = db.q1("SELECT * FROM users WHERE (lower(name)=lower(?) OR lower(email)=lower(?)) AND active=1",
              name, name)
    if not u or not u["pw_hash"] or not auth.check_pw(form.get("password") or "", u["pw_hash"]):
        auth.record_fail(ip)
        auth.record_fail(name.lower())
        flash(request, "Name or password is incorrect.", "err")
        return redirect("/login")
    request.session.clear()
    request.session["uid"] = u["id"]
    nxt = form.get("next") or "/"
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")


@app.post("/logout")
async def logout(request: Request):
    await form_checked(request)
    request.session.clear()
    return redirect("/")


@app.get("/invite/{token}", response_class=HTMLResponse)
@app.get("/reset/{token}", response_class=HTMLResponse)
def set_pw_form(request: Request, token: str):
    kind = "invite" if request.url.path.startswith("/invite") else "reset"
    uid = auth.use_token(token, kind)
    if not uid:
        return render(request, "message.html", title="Link expired",
                      text="This link is no longer valid. Ask the admin for a new one, or use 'Forgot password'.")
    u = db.q1("SELECT * FROM users WHERE id=?", uid)
    return render(request, "setpw.html", token=token, kind=kind, target=u)


@app.post("/setpw")
async def set_pw(request: Request):
    form = await form_checked(request)
    kind = form.get("kind")
    uid = auth.use_token(form.get("token") or "", kind)
    if not uid:
        flash(request, "This link is no longer valid.", "err")
        return redirect("/login")
    pw = form.get("password") or ""
    if len(pw) < 8 or pw != form.get("password2"):
        flash(request, "Use at least 8 characters and type the same password twice.", "err")
        return redirect(f"/{kind}/{form.get('token')}")
    auth.use_token(form.get("token"), kind, consume=True)
    db.ex("UPDATE users SET pw_hash=? WHERE id=?", auth.hash_pw(pw), uid)
    request.session.clear()
    request.session["uid"] = uid
    flash(request, "Password saved. You're signed in.")
    return redirect("/predict")


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return render(request, "forgot.html")


@app.post("/forgot")
async def forgot(request: Request):
    form = await form_checked(request)
    email = (form.get("email") or "").strip()
    ip = request.client.host if request.client else "?"
    if not auth.throttled("forgot" + ip, limit=5):
        auth.record_fail("forgot" + ip)
        u = db.q1("SELECT * FROM users WHERE lower(email)=lower(?) AND active=1", email)
        if u:
            tok = auth.make_token(u["id"], "reset", 2)
            mailer.send(u["email"], "Reset your Paddock Club password",
                        f"Hi {u['name']},\n\nSet a new password here (valid for 2 hours):\n"
                        f"{mailer.base_url()}/reset/{tok}\n\nIf you didn't ask for this, ignore this mail.")
    flash(request, "If that address belongs to a player, a reset link is on its way.")
    return redirect("/login")


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    require_user(request)
    return render(request, "account.html")


@app.post("/account")
async def account_save(request: Request):
    u = require_user(request)
    form = await form_checked(request)
    if not auth.check_pw(form.get("current") or "", u["pw_hash"] or ""):
        flash(request, "Current password is incorrect.", "err")
        return redirect("/account")
    pw = form.get("password") or ""
    if len(pw) < 8 or pw != form.get("password2"):
        flash(request, "Use at least 8 characters and type the same password twice.", "err")
        return redirect("/account")
    db.ex("UPDATE users SET pw_hash=? WHERE id=?", auth.hash_pw(pw), u["id"])
    flash(request, "Password changed.")
    return redirect("/account")


# ------------------------------------------------------------------ predictions
@app.get("/predict", response_class=HTMLResponse)
@app.get("/predict/{event_id}", response_class=HTMLResponse)
def predict(request: Request, event_id: int | None = None):
    u = require_user(request)
    ev = db.q1("SELECT * FROM events WHERE id=?", event_id) if event_id else next_event()
    if not ev:
        return render(request, "message.html", title="No race coming up",
                      text="The next race weekend appears here as soon as F1 publishes the calendar.")
    year = ev["year"]
    mine = scoring.load_predictions(ev["id"]).get(u["id"], {})
    fields = scoring.fields_for_event(ev)
    locks = {f: lock_utc(ev["id"], f) for f in fields}
    drivers = entrants(ev["id"], year)
    teams = sorted({d["team"] for d in drivers if d["team"]})
    upcoming = db.q("SELECT e.* FROM events e JOIN sessions s ON s.event_id=e.id AND s.kind='R' "
                    "WHERE e.year=? AND e.cancelled=0 AND e.imported=0 AND s.start_utc>=? ORDER BY e.round",
                    year, db.iso(db.now_utc() - timedelta(hours=3)))
    sl = season_lock(year)
    return render(request, "predict.html", ev=ev, mine=mine, fields=fields, locks=locks,
                  now=db.now_utc(), drivers=drivers, teams=teams, team_info=team_map(year),
                  sessions=sessions_of(ev["id"]), upcoming=upcoming, season_lock=sl, year=year,
                  season_open=bool(sl and db.now_utc() < sl),
                  season_pick=dict(scoring.load_predictions(0, year).get(u["id"], {}).get("season_pos", {})))


@app.post("/predict/{event_id}")
async def predict_save(request: Request, event_id: int):
    u = require_user(request)
    form = await form_checked(request)
    ev = db.q1("SELECT * FROM events WHERE id=? AND imported=0", event_id)
    if not ev:
        return redirect("/predict")
    valid_drivers = {d["tla"] for d in entrants(ev["id"], ev["year"])}
    valid_teams = {d["team"] for d in entrants(ev["id"], ev["year"])}
    saved, skipped, errors = [], [], []
    ts = db.iso(db.now_utc())
    for f in scoring.fields_for_event(ev):
        n = scoring.FIELDS[f][0]
        vals = {i: (form.get(f"{f}_{i}") or "").strip() for i in range(1, n + 1)}
        if not any(vals.values()):
            continue
        if is_locked(ev["id"], f):
            skipped.append(scoring.LABELS[f])
            continue
        allowed = valid_teams if f == "fp" else valid_drivers
        picked = [v for v in vals.values() if v]
        if any(v not in allowed for v in picked):
            errors.append(f"{scoring.LABELS[f]}: unknown choice")
            continue
        if n > 1 and len(set(picked)) != len(picked):
            errors.append(f"{scoring.LABELS[f]}: each driver can only be picked once")
            continue
        with db.tx() as c:
            c.execute("DELETE FROM predictions WHERE user_id=? AND event_id=? AND field=?", (u["id"], ev["id"], f))
            for i, v in vals.items():
                if v:
                    c.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?)",
                              (u["id"], ev["year"], ev["id"], f, i, v, ts))
        saved.append(scoring.LABELS[f])
    msg = []
    if saved:
        msg.append("Saved: " + ", ".join(saved) + ".")
    if skipped:
        msg.append("Not saved, deadline passed: " + ", ".join(skipped) + ".")
    if errors:
        msg.append(" ".join(errors))
    flash(request, " ".join(msg) or "Nothing to save.", "err" if (errors or skipped) else "ok")
    engine.bump()
    return redirect(f"/predict/{ev['id']}")


@app.post("/season/{year}")
async def season_save(request: Request, year: int):
    u = require_user(request)
    form = await form_checked(request)
    sl = season_lock(year)
    nxt = form.get("back") or "/predict"
    if not sl or db.now_utc() >= sl:
        flash(request, "The championship prediction is closed.", "err")
        return redirect(nxt)
    ev = next_event()
    valid = {d["tla"] for d in entrants(ev["id"], year)} if ev else set()
    vals = {i: (form.get(f"season_{i}") or "").strip() for i in range(1, 11)}
    picked = [v for v in vals.values() if v]
    if any(v not in valid for v in picked) or len(set(picked)) != len(picked):
        flash(request, "Pick each driver only once.", "err")
        return redirect(nxt)
    with db.tx() as c:
        c.execute("DELETE FROM predictions WHERE user_id=? AND year=? AND event_id=0", (u["id"], year))
        for i, v in vals.items():
            if v:
                c.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?)",
                          (u["id"], year, 0, "season_pos", i, v, db.iso(db.now_utc())))
    flash(request, "Championship prediction saved.")
    return redirect(nxt)


# ------------------------------------------------------------------ results
@app.get("/races", response_class=HTMLResponse)
def races(request: Request, year: int | None = None):
    require_user(request)
    year = year or default_year()
    board, events, live_ev = scoring.leaderboard(year)
    sess = defaultdict(dict)
    for s in db.q("SELECT s.* FROM sessions s JOIN events e ON e.id=s.event_id WHERE e.year=?", year):
        sess[s["event_id"]][s["kind"]] = s
    best = {e["id"]: max((r["events"][e["id"]] for r in board if e["id"] in r["events"]), default=None)
            for e in events}
    return render(request, "races.html", year=year, board=board, events=events, sess=sess, best=best,
                  live_ev=live_ev, years=[r["year"] for r in db.q("SELECT DISTINCT year FROM events ORDER BY year DESC")])


@app.get("/race/{event_id}", response_class=HTMLResponse)
def race(request: Request, event_id: int):
    u = require_user(request)
    ev = db.q1("SELECT * FROM events WHERE id=?", event_id)
    if not ev:
        return redirect("/races")
    res = scoring.load_results(ev["id"])
    preds = scoring.load_predictions(ev["id"])
    rules = scoring.rules_for(ev["year"])
    users = db.q("SELECT id, name FROM users WHERE active=1 OR id IN (SELECT user_id FROM predictions WHERE "
                 "event_id=?) ORDER BY name", ev["id"])
    fields = scoring.fields_for_event(ev)
    visible = {f: is_locked(ev["id"], f) or ev["imported"] for f in fields}
    stored = scoring.event_scores(ev)
    pens = defaultdict(list)
    for p in db.q("SELECT * FROM penalties WHERE event_id=?", ev["id"]):
        pens[p["user_id"]].append(p)
    rows = []
    for usr in users:
        p = preds.get(usr["id"], {})
        tot, per_field, per_slot = scoring.score_event(p, res, rules)
        if usr["id"] in stored:
            tot = stored[usr["id"]][0]
        rows.append({"user": usr, "pred": p, "per_slot": per_slot, "total": tot,
                     "pen": sum(x["points"] for x in pens[usr["id"]]), "pens": pens[usr["id"]]})
    rows.sort(key=lambda r: -(r["total"] - r["pen"]))
    return render(request, "race.html", ev=ev, res=res, rows=rows, fields=fields, visible=visible,
                  me=u, drivers=driver_map(ev["year"]), teams=team_map(ev["year"]),
                  sessions=sessions_of(ev["id"]), year=ev["year"],
                  season_preds=None)


def _f1_context(year):
    tables = standings.season_tables(year)
    live_ev = None
    if engine.live_session:
        r = db.q1("SELECT event_id FROM sessions WHERE id=?", engine.live_session)
        live_ev = r["event_id"] if r else None
    return {"tables": tables, "live_ev": live_ev, "drivers": driver_map(year), "teams": team_map(year)}


def _season_prediction_rows(year):
    sl = season_lock(year)
    open_ = bool(sl and db.now_utc() < sl)
    stand = scoring.current_standings(year)
    preds = scoring.load_predictions(0, year)
    rules = scoring.rules_for(year)
    stored = scoring.season_prediction_scores(year)
    rows = []
    for usr in db.q("SELECT id, name FROM users ORDER BY name"):
        p = preds.get(usr["id"], {})
        if not p and usr["id"] not in stored:
            continue
        tot, per_slot = scoring.score_season(p, stand, rules)
        rows.append({"user": usr, "pred": p.get("season_pos", {}), "per_slot": per_slot,
                     "total": stored.get(usr["id"], tot)})
    rows.sort(key=lambda r: -r["total"])
    return rows, open_, sl


@app.get("/f1", response_class=HTMLResponse)
def f1_standings(request: Request, year: int | None = None):
    year = year or default_year()
    rows, open_, sl = _season_prediction_rows(year)
    return render(request, "f1.html", year=year, rows=rows, open_=open_, season_lock=sl,
                  years=[r["year"] for r in db.q("SELECT DISTINCT year FROM events ORDER BY year DESC")],
                  **_f1_context(year))


@app.get("/fragment/f1", response_class=HTMLResponse)
def f1_fragment(request: Request, year: int | None = None):
    year = year or default_year()
    return templates.TemplateResponse(request, "_f1tables.html", {"request": request, "year": year,
                                                                   **_f1_context(year)})


@app.get("/championship/{year}")
def championship(year: int):
    return redirect(f"/f1?year={year}")


# ------------------------------------------------------------------ admin
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    require_admin(request)
    users = db.q("SELECT * FROM users ORDER BY active DESC, name")
    ev = next_event()
    return render(request, "admin.html", users=users, status=engine.status(), ev=ev,
                  logs=db.q("SELECT * FROM log ORDER BY id DESC LIMIT 40"),
                  base_url=mailer.base_url(), smtp=mailer.configured(),
                  sync=json.loads(db.setting(f"schedule_sync_{db.now_utc().year}", "{}")),
                  events=db.q("SELECT * FROM events WHERE year=? ORDER BY round", default_year()),
                  invite_links=request.session.pop("invite_link", None))


@app.post("/admin/user")
async def admin_user(request: Request):
    require_admin(request)
    form = await form_checked(request)
    action = form.get("action")
    uid = form.get("user_id")
    if action == "create":
        name = (form.get("name") or "").strip()
        email = (form.get("email") or "").strip() or None
        if not name:
            flash(request, "Enter a name.", "err")
            return redirect("/admin")
        existing = db.q1("SELECT * FROM users WHERE lower(name)=lower(?)", name)
        if existing:
            db.ex("UPDATE users SET email=COALESCE(?, email), active=1 WHERE id=?", email, existing["id"])
            uid = existing["id"]
        else:
            db.ex("INSERT INTO users(name,email,created_at) VALUES(?,?,?)", name, email, db.iso(db.now_utc()))
            uid = db.q1("SELECT id FROM users WHERE name=?", name)["id"]
        action = "invite"
    u = db.q1("SELECT * FROM users WHERE id=?", uid)
    if not u:
        return redirect("/admin")
    if action == "invite":
        tok = auth.make_token(u["id"], "invite", 24 * 14)
        link = f"{mailer.base_url()}/invite/{tok}"
        sent = mailer.send(u["email"], "You're invited to Paddock Club Reeuwijk",
                           f"Hi {u['name']},\n\nYou're in the F1 prediction game. Choose your password here "
                           f"(link valid for 14 days):\n{link}\n\nAfter that, sign in with your name "
                           f"or this e-mail address.") if u["email"] else False
        request.session["invite_link"] = {"name": u["name"], "link": link, "sent": sent}
        flash(request, f"Invite for {u['name']} created" + (" and e-mailed." if sent else ". Copy the link below."))
    elif action == "toggle_admin":
        db.ex("UPDATE users SET is_admin=1-is_admin WHERE id=?", u["id"])
    elif action == "deactivate":
        db.ex("UPDATE users SET active=0 WHERE id=?", u["id"])
    elif action == "activate":
        db.ex("UPDATE users SET active=1 WHERE id=?", u["id"])
    elif action == "email":
        db.ex("UPDATE users SET email=? WHERE id=?", (form.get("email") or "").strip() or None, u["id"])
    engine.bump()
    return redirect("/admin")


@app.post("/admin/settings")
async def admin_settings(request: Request):
    require_admin(request)
    form = await form_checked(request)
    if form.get("base_url") is not None:
        db.set_setting("base_url", form.get("base_url").strip().rstrip("/"))
    if form.get("action") == "sync":
        engine.force("schedule")
        engine.force("entrants")
        engine.force("finalise")
        flash(request, "Sync started. Refresh in a minute.")
    elif form.get("action") == "relogin":
        engine.tokens.clear()
        tok = engine.tokens.get(force=True)
        flash(request, "Signed in to F1." if tok else f"F1 sign-in failed: {engine.tokens.last_error}",
              "ok" if tok else "err")
    else:
        flash(request, "Settings saved.")
    return redirect("/admin")


@app.get("/admin/event/{event_id}", response_class=HTMLResponse)
def admin_event(request: Request, event_id: int):
    require_admin(request)
    ev = db.q1("SELECT * FROM events WHERE id=?", event_id)
    if not ev:
        return redirect("/admin")
    res = defaultdict(dict)
    src = {}
    for r in db.q("SELECT * FROM results WHERE event_id=?", event_id):
        res[r["field"]][r["slot"]] = r["value"]
        src[(r["field"], r["slot"])] = r["source"]
    drivers = entrants(ev["id"], ev["year"])
    return render(request, "admin_event.html", ev=ev, res=res, src=src, drivers=drivers,
                  teams=sorted({d["team"] for d in drivers if d["team"]}), sessions=sessions_of(ev["id"]),
                  users=db.q("SELECT * FROM users WHERE active=1 ORDER BY name"),
                  pens=db.q("SELECT p.*, u.name FROM penalties p JOIN users u ON u.id=p.user_id "
                            "WHERE event_id=?", event_id), fields=scoring.fields_for_event(ev),
                  year=ev["year"])


@app.post("/admin/event/{event_id}")
async def admin_event_save(request: Request, event_id: int):
    require_admin(request)
    form = await form_checked(request)
    ev = db.q1("SELECT * FROM events WHERE id=?", event_id)
    action = form.get("action")
    if action == "results":
        if ev["frozen"] or ev["imported"]:
            flash(request, "This weekend is frozen; results can no longer change.", "err")
            return redirect(f"/admin/event/{event_id}")
        ts = db.iso(db.now_utc())
        with db.tx() as c:
            for f in ("dotd", "fp", "pole", "fl", "sprint_pole"):
                v = form.get(f"res_{f}")
                if v is None:
                    continue
                if v == "__auto__":
                    c.execute("DELETE FROM results WHERE event_id=? AND field=? AND source='admin'", (event_id, f))
                elif v:
                    c.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)", (event_id, f, 1, v, "admin", ts))
        flash(request, "Results saved.")
    elif action == "penalty":
        try:
            pts = int(form.get("points") or 5)
        except ValueError:
            pts = 5
        db.ex("INSERT INTO penalties(user_id,event_id,points,reason,created_at) VALUES(?,?,?,?,?)",
              int(form.get("user_id")), event_id, pts, (form.get("reason") or "Late entry")[:200],
              db.iso(db.now_utc()))
        flash(request, "Penalty added.")
    elif action == "unpenalty":
        db.ex("DELETE FROM penalties WHERE id=? AND event_id=?", int(form.get("pid")), event_id)
        flash(request, "Penalty removed.")
    elif action == "refetch":
        db.ex("UPDATE sessions SET last_fetch=NULL WHERE event_id=?", event_id)
        engine.force("finalise")
        flash(request, "Fetching official results again.")
    engine.bump()
    return redirect(f"/admin/event/{event_id}")


@app.get("/admin/import", response_class=HTMLResponse)
def admin_import(request: Request):
    require_admin(request)
    return render(request, "admin_import.html", preview=None)


@app.post("/admin/import", response_class=HTMLResponse)
async def admin_import_post(request: Request, file: UploadFile = File(...), year: int = Form(...),
                            csrf: str = Form(...)):
    require_admin(request)
    if not auth.csrf_ok(request.session, csrf):
        raise LoginRequired()
    tmp = DATA_DIR / f"import_{year}.xlsx"
    tmp.write_bytes(await file.read())
    try:
        if not db.q1("SELECT 1 FROM events WHERE year=?", year):
            from .schedule import sync
            sync(year, engine.http)
        plan = importer.plan(str(tmp), year)
    except Exception as exc:  # noqa: BLE001
        log.exception("import failed")
        flash(request, f"Could not read this file: {exc}", "err")
        return redirect("/admin/import")
    return render(request, "admin_import.html", preview=plan, year=year)


@app.post("/admin/import/commit")
async def admin_import_commit(request: Request):
    require_admin(request)
    form = await form_checked(request)
    year = int(form.get("year"))
    tmp = DATA_DIR / f"import_{year}.xlsx"
    if not tmp.exists():
        return redirect("/admin/import")
    plan = importer.plan(str(tmp), year)
    importer.commit(plan)
    engine.bump()
    flash(request, f"Imported {year}: {len(plan['events'])} weekends, {len(plan['players'])} players.")
    return redirect(f"/races?year={year}")


@app.get("/admin/reports", response_class=HTMLResponse)
def admin_reports(request: Request, year: int | None = None):
    require_admin(request)
    year = year or default_year()
    events = list(reversed(standings.completed_events(year)))
    return render(request, "admin_reports.html", year=year, events=events, kinds=reports.KINDS,
                  years=[r["year"] for r in db.q("SELECT DISTINCT year FROM events ORDER BY year DESC")])


@app.get("/admin/reports/{year}/{rnd}/{kind}.pdf")
def admin_report_pdf(request: Request, year: int, rnd: int, kind: str, html: int = 0):
    require_admin(request)
    if kind not in reports.KINDS or not db.q1("SELECT 1 FROM events WHERE year=? AND round=?", year, rnd):
        return redirect("/admin/reports")
    if html:
        return HTMLResponse(reports.render_html(kind, year, rnd))
    try:
        pdf = reports.render_pdf(kind, year, rnd)
    except Exception as exc:  # noqa: BLE001
        log.exception("report failed")
        flash(request, f"Could not create the report: {exc}", "err")
        return redirect(f"/admin/reports?year={year}")
    from fastapi.responses import Response
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{reports.filename(kind, year, rnd)}"'})


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "version": engine.version})
