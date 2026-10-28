"""Background worker: calendar sync, entry lists, live timing, finalisation, freezing."""
import logging
import os
import threading
import time
from datetime import timedelta
from pathlib import Path

import requests

from . import db, livetiming as lt, scoring, mailer
from .f1auth import TokenManager
from .schedule import DURATION, SCORED, sync as sync_schedule

log = logging.getLogger("engine")

FREEZE_AFTER = timedelta(hours=48)
REFETCH_EVERY = timedelta(hours=2)
LIVE_PRE = timedelta(minutes=15)


class Engine:
    def __init__(self, data_dir="/data"):
        self.tokens = TokenManager(Path(data_dir) / "f1_token.json")
        self.http = lt.F1Http(self.tokens)
        self.version = 0                 # bumped on every visible change (drives SSE)
        self.live = None                 # LiveClient
        self.live_session = None         # sessions row id
        self.live_status = "idle"
        self._dirty = False
        self._last_flush = 0.0
        self._next = {}
        self._stop = threading.Event()
        self.wake = threading.Event()

    # ------------------------------------------------------------ helpers
    def bump(self):
        self.version += 1

    def due(self, key, every_s):
        t = time.time()
        if t >= self._next.get(key, 0):
            self._next[key] = t + every_s
            return True
        return False

    def force(self, key):
        self._next[key] = 0
        self.wake.set()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="engine").start()

    def _run(self):
        time.sleep(2)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("engine tick failed")
            self.wake.wait(timeout=2 if self.live else 20)
            self.wake.clear()

    # ------------------------------------------------------------ main tick
    def tick(self):
        now = db.now_utc()
        year = now.year
        if self.due("schedule", 6 * 3600):
            for y in {year, (now + timedelta(days=45)).year}:
                try:
                    if sync_schedule(y, self.http):
                        self.bump()
                except Exception as exc:  # noqa: BLE001
                    db.logmsg("warning", f"Calendar sync {y} failed: {exc}")
        if self.due("entrants", 1800):
            self.refresh_entrants(now)
        if self.due("logos", 24 * 3600):
            self.resolve_logos(year)
        self.handle_live(now)
        if self.due("finalise", 300):
            self.finalise(now)
        if self.due("standings", 6 * 3600):
            self.fallback_standings(year)
        if self.due("backfill", 3600):
            self.backfill_entrants(year)

    # ------------------------------------------------------------ entrants
    def refresh_entrants(self, now):
        """Entry list of upcoming/current weekends from the official driver list."""
        rows = db.q("SELECT e.* FROM events e WHERE e.cancelled=0 AND e.imported=0 AND EXISTS("
                    "SELECT 1 FROM sessions s WHERE s.event_id=e.id AND s.kind='R' AND s.start_utc>=?)"
                    " ORDER BY e.year, e.round LIMIT 2", db.iso(now - timedelta(days=1)))
        for ev in rows:
            sess = db.q("SELECT * FROM sessions WHERE event_id=? AND path IS NOT NULL AND start_utc<=? "
                        "ORDER BY start_utc DESC", ev["id"], db.iso(now))
            drivers = {}
            for s in sess:
                txt = self.http.get(f"{s['path']}DriverList.jsonStream")
                if txt:
                    st = lt.TimingState()
                    for m in lt.parse_stream(txt):
                        st.apply("DriverList", m)
                    drivers = lt.driver_info(st.data)
                    if drivers:
                        break
            if drivers:
                self.save_entrants(ev, drivers)
            elif not db.q1("SELECT 1 FROM entrants WHERE event_id=?", ev["id"]):
                self.copy_previous_entrants(ev)

    def save_entrants(self, ev, drivers):
        with db.tx() as c:
            c.execute("DELETE FROM entrants WHERE event_id=?", (ev["id"],))
            for d in drivers.values():
                c.execute("INSERT OR REPLACE INTO entrants VALUES(?,?,?,?,?,?,?)",
                          (ev["id"], d["number"], d["tla"], d["name"], d["team"], d["colour"], d["headshot"]))
                if d["team"]:
                    c.execute("INSERT INTO teams(year,name,colour) VALUES(?,?,?) ON CONFLICT(year,name) "
                              "DO UPDATE SET colour=excluded.colour", (ev["year"], d["team"], d["colour"]))
        self.bump()

    def backfill_entrants(self, year):
        """Teams/colours for past weekends (also imported ones) from the official driver list."""
        rows = db.q("SELECT e.*, s.path AS rpath FROM events e JOIN sessions s ON s.event_id=e.id AND s.kind='R' "
                    "WHERE e.year IN (?, ?) AND e.cancelled=0 AND s.path IS NOT NULL AND s.start_utc<? "
                    "AND EXISTS(SELECT 1 FROM results r WHERE r.event_id=e.id) "
                    "AND NOT EXISTS(SELECT 1 FROM entrants x WHERE x.event_id=e.id AND x.colour!='') "
                    "ORDER BY e.year DESC, e.round DESC LIMIT 8",
                    year, year - 1, db.iso(db.now_utc() - timedelta(hours=3)))
        for ev in rows:
            txt = self.http.get(f"{ev['rpath']}DriverList.jsonStream")
            if not txt:
                continue
            st = lt.TimingState()
            for m in lt.parse_stream(txt):
                st.apply("DriverList", m)
            drivers = lt.driver_info(st.data)
            if drivers:
                self.save_entrants(ev, drivers)
        if rows:
            self.resolve_logos(year - 1)

    def copy_previous_entrants(self, ev):
        prev = db.q1("SELECT e.id FROM events e WHERE EXISTS(SELECT 1 FROM entrants x WHERE x.event_id=e.id) "
                     "AND (e.year<? OR (e.year=? AND e.round<?)) ORDER BY e.year DESC, e.round DESC LIMIT 1",
                     ev["year"], ev["year"], ev["round"])
        if prev:
            db.ex("INSERT OR IGNORE INTO entrants SELECT ?, number, tla, name, team, colour, headshot "
                  "FROM entrants WHERE event_id=?", ev["id"], prev["id"])

    # ------------------------------------------------------------ logos
    def resolve_logos(self, year):
        for t in db.q("SELECT * FROM teams WHERE year=? AND (logo_url IS NULL OR logo_url='')", year):
            slug = "".join(ch for ch in t["name"].lower() if ch.isalnum())
            cands = []
            for y in (year, year - 1):
                for sl in dict.fromkeys([slug, slug.replace("f1team", ""), slug.replace("racing", "")]):
                    cands.append(f"https://media.formula1.com/image/upload/c_lfill,w_96/q_auto/"
                                 f"v1740000000/common/f1/{y}/{sl}/{y}{sl}logowhite.webp")
            for u in cands:
                try:
                    r = requests.head(u, timeout=10, allow_redirects=True)
                    if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
                        db.ex("UPDATE teams SET logo_url=?, logo_checked=? WHERE year=? AND name=?",
                              u, db.iso(db.now_utc()), year, t["name"])
                        self.bump()
                        break
                except requests.RequestException:
                    continue

    # ------------------------------------------------------------ live
    def active_session(self, now):
        rows = db.q("SELECT s.*, e.year, e.name AS ev_name, e.id AS ev_id FROM sessions s JOIN events e "
                    "ON e.id=s.event_id WHERE e.cancelled=0 AND e.imported=0 AND s.kind IN ('SQ','S','Q','R') "
                    "AND s.status IN ('scheduled','live') AND s.start_utc<=? AND s.start_utc>=? "
                    "ORDER BY s.start_utc", db.iso(now + LIVE_PRE), db.iso(now - timedelta(hours=5)))
        for s in rows:
            end = db.parse_iso(s["start_utc"]) + timedelta(minutes=DURATION[s["kind"]]) + timedelta(hours=2)
            if now <= end:
                return s
        return None

    def handle_live(self, now):
        s = self.active_session(now)
        if s is None:
            if self.live:
                self.stop_live("window closed")
            return
        if self.live_session != s["id"]:
            if self.live:
                self.stop_live("next session")
            self.live_session = s["id"]
            self.live = lt.LiveClient(self.tokens, self._on_live_update)
            self.live_status = "connecting"
            try:
                self.live.start()
                db.logmsg("info", f"Live timing connected for {s['ev_name']} {s['name']}")
            except Exception as exc:  # noqa: BLE001
                self.live_status = f"live connect failed: {exc}"
                db.logmsg("warning", self.live_status)
        live = self.live
        if live and live.connected:
            self.live_status = "connected"
        # fallback: poll the archive when the live feed is unavailable
        if (not live or not live.connected or time.time() - live.last_msg > 120) and s["path"] \
                and self.due("archive_poll", 60):
            st = self.http.session_state(s["path"])
            if st:
                self.apply_state(s, st.data, st.pit_events, "archive")
        if live and (self._dirty and time.time() - self._last_flush > 2):
            self._dirty = False
            self._last_flush = time.time()
            data, pits = live.state.snapshot()
            self.apply_state(s, data, pits, "live")
            if lt.is_finished(data):
                db.ex("UPDATE sessions SET status='finished', finished_at=? WHERE id=?",
                      db.iso(db.now_utc()), s["id"])
                self.on_session_finished(s)
                self.stop_live("session finished")

    def _on_live_update(self):
        self._dirty = True
        self.wake.set()

    def stop_live(self, why):
        if self.live:
            self.live.stop()
            db.logmsg("info", f"Live timing stopped ({why})")
        self.live = None
        self.live_session = None
        self.live_status = "idle"

    def apply_state(self, s, data, pits, source):
        """Write derived results (+ standings) from a timing state."""
        if not data.get("TimingData"):
            return
        info = data.get("SessionInfo") or {}
        if source == "live" and info.get("Name") and info["Name"].lower() != (s["name"] or "").lower():
            return      # feed still shows the previous session
        if s["status"] == "scheduled":
            db.ex("UPDATE sessions SET status='live' WHERE id=? AND status='scheduled'", s["id"])
        ev_id = s["event_id"]
        known = {e["number"]: {"tla": e["tla"], "team": e["team"]}
                 for e in db.q("SELECT * FROM entrants WHERE event_id=?", ev_id)}
        drv = lt.driver_info(data)
        if drv and len(drv) >= len(known):
            ev = db.q1("SELECT * FROM events WHERE id=?", ev_id)
            if set(drv) != set(known):
                self.save_entrants(ev, drv)
        res = lt.derive(s["kind"], data, pits, known)
        self.write_results(ev_id, res, source)
        champ = lt.championship(data)
        if champ:
            num2tla = {k: v["tla"] for k, v in (drv or {}).items()} or {k: v["tla"] for k, v in known.items()}
            year = db.q1("SELECT year FROM events WHERE id=?", ev_id)["year"]
            with db.tx() as c:
                c.execute("DELETE FROM standings WHERE year=?", (year,))
                for pos, num, pts in champ:
                    if num in num2tla:
                        c.execute("INSERT OR REPLACE INTO standings VALUES(?,?,?,?,?)",
                                  (year, pos, num2tla[num], pts, db.iso(db.now_utc())))
        teams = lt.championship_teams(data)
        if teams:
            year = db.q1("SELECT year FROM events WHERE id=?", ev_id)["year"]
            with db.tx() as c:
                c.execute("DELETE FROM team_standings WHERE year=?", (year,))
                for pos, team, pts in teams:
                    c.execute("INSERT OR REPLACE INTO team_standings VALUES(?,?,?,?,?)",
                              (year, pos, team, pts, db.iso(db.now_utc())))
        self.bump()

    def write_results(self, ev_id, res, source):
        ts = db.iso(db.now_utc())
        with db.tx() as c:
            for field, slots in res.items():
                admin = {r["slot"] for r in c.execute(
                    "SELECT slot FROM results WHERE event_id=? AND field=? AND source='admin'", (ev_id, field))}
                c.execute("DELETE FROM results WHERE event_id=? AND field=? AND source!='admin'", (ev_id, field))
                for slot, val in slots.items():
                    if slot not in admin and val:
                        c.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)",
                                  (ev_id, field, int(slot), val, source, ts))

    def on_session_finished(self, s):
        if s["kind"] != "R":
            return
        ev = db.q1("SELECT * FROM events WHERE id=?", s["event_id"])
        missing = []
        res = scoring.load_results(ev["id"])
        if not res.get("dotd"):
            missing.append("Driver of the Day")
        if not res.get("fp"):
            missing.append("Fastest pit stop")
        if missing:
            mailer.notify_admins(f"{ev['name']}: please enter {', '.join(missing)}",
                                 f"The race has finished. Please enter: {', '.join(missing)} on the "
                                 f"admin results page so the scores are complete.\n\n"
                                 f"{mailer.base_url()}/admin/event/{ev['id']}")

    # ------------------------------------------------------------ finalise
    def finalise(self, now):
        """Re-read finished sessions from the official archive; freeze 48h after the race."""
        rows = db.q("SELECT s.*, e.frozen FROM sessions s JOIN events e ON e.id=s.event_id "
                    "WHERE e.imported=0 AND e.cancelled=0 AND e.frozen=0 AND s.kind IN ('SQ','S','Q','R') "
                    "AND s.start_utc<=?", db.iso(now - timedelta(minutes=30)))
        for s in rows:
            start = db.parse_iso(s["start_utc"])
            if s["id"] == self.live_session:
                continue
            ended = start + timedelta(minutes=DURATION[s["kind"]])
            if now < ended + timedelta(minutes=20):
                continue
            if now - start > timedelta(days=4) and not db.q1(
                    "SELECT 1 FROM predictions WHERE event_id=? LIMIT 1", s["event_id"]):
                continue    # old weekend nobody played in the app: nothing to score
            last = db.parse_iso(s["last_fetch"])
            if last and now - last < REFETCH_EVERY:
                continue
            if not s["path"]:
                # path appears in the official index once data exists
                if self.due("schedule_for_path", 1800):
                    self.force("schedule")
                continue
            st = self.http.session_state(s["path"])
            db.ex("UPDATE sessions SET last_fetch=? WHERE id=?", db.iso(now), s["id"])
            if st and lt.classification(st.data):
                first = s["status"] != "final"
                self.apply_state(s, st.data, st.pit_events, "final")
                db.ex("UPDATE sessions SET status='final', finished_at=COALESCE(finished_at, ?) WHERE id=?",
                      db.iso(ended), s["id"])
                if first and s["status"] != "finished":
                    self.on_session_finished(s)
        # freeze events 48h after the race
        for ev in db.q("SELECT e.*, s.start_utc AS race_start, s.status AS race_status FROM events e "
                       "JOIN sessions s ON s.event_id=e.id AND s.kind='R' WHERE e.frozen=0 AND e.imported=0 "
                       "AND e.cancelled=0"):
            race_end = db.parse_iso(ev["race_start"]) + timedelta(minutes=DURATION["R"])
            has_dotd = db.q1("SELECT 1 FROM results WHERE event_id=? AND field='dotd'", ev["id"])
            if now >= race_end + FREEZE_AFTER and ev["race_status"] in ("final", "finished") \
                    and (has_dotd or now >= race_end + timedelta(days=7)):
                scoring.freeze_event(ev)
                self.bump()

    def fallback_standings(self, year):
        """If no official championship data has arrived, use Jolpica (Ergast successor)."""
        if db.q1("SELECT 1 FROM standings WHERE year=?", year):
            return
        try:
            r = requests.get(f"https://api.jolpi.ca/ergast/f1/{year}/driverstandings.json", timeout=20)
            lists = r.json()["MRData"]["StandingsTable"]["StandingsLists"]
            if not lists:
                return
            with db.tx() as c:
                for d in lists[-1]["DriverStandings"]:
                    c.execute("INSERT OR REPLACE INTO standings VALUES(?,?,?,?,?)",
                              (year, int(d["position"]), d["Driver"]["code"], float(d["points"]),
                               db.iso(db.now_utc())))
            self.bump()
        except Exception as exc:  # noqa: BLE001
            log.info("fallback standings unavailable: %s", exc)

    # ------------------------------------------------------------ status
    def status(self):
        tok = self.tokens._token or self.tokens._load()
        sub = None
        if tok:
            try:
                sub = tok.claims.get("SubscriptionStatus") or tok.claims.get("SubscribedProduct")
            except Exception:  # noqa: BLE001
                sub = None
        return {
            "f1_account": bool(os.environ.get("F1_USERNAME")),
            "token_valid": bool(tok and tok.is_valid()),
            "token_expires": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(tok.expires_at)) if tok else None,
            "subscription": sub,
            "auth_error": self.tokens.last_error,
            "live": self.live_status,
            "live_last_msg": time.strftime("%H:%M:%S", time.localtime(self.live.last_msg))
            if self.live and self.live.last_msg else None,
        }
