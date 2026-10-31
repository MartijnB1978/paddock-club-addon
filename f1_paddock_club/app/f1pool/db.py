"""SQLite storage. One file in the add-on's /data folder (included in HA backups)."""
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("F1POOL_DB", "/data/f1pool.sqlite3")
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, email TEXT UNIQUE,
  pw_hash TEXT, is_admin INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY, user_id INTEGER, kind TEXT, expires_at TEXT, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS seasons(
  year INTEGER PRIMARY KEY, rules TEXT DEFAULT 'v2', season_lock_utc TEXT,
  imported INTEGER DEFAULT 0, final INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, year INTEGER, round INTEGER, name TEXT, country TEXT,
  location TEXT, official_name TEXT, sprint INTEGER DEFAULT 0, cancelled INTEGER DEFAULT 0,
  frozen INTEGER DEFAULT 0, imported INTEGER DEFAULT 0,
  UNIQUE(year, name));
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, event_id INTEGER, kind TEXT, name TEXT, start_utc TEXT,
  path TEXT, status TEXT DEFAULT 'scheduled', finished_at TEXT, last_fetch TEXT,
  manual_start INTEGER DEFAULT 0,
  UNIQUE(event_id, kind));
CREATE TABLE IF NOT EXISTS entrants(
  event_id INTEGER, number TEXT, tla TEXT, name TEXT, team TEXT, colour TEXT, headshot TEXT,
  PRIMARY KEY(event_id, tla));
CREATE TABLE IF NOT EXISTS driver_swaps(
  event_id INTEGER, out_tla TEXT, in_tla TEXT, in_name TEXT, created_at TEXT,
  PRIMARY KEY(event_id, out_tla));
CREATE TABLE IF NOT EXISTS teams(
  year INTEGER, name TEXT, colour TEXT, logo_url TEXT, logo_checked TEXT,
  PRIMARY KEY(year, name));
CREATE TABLE IF NOT EXISTS predictions(
  user_id INTEGER, year INTEGER, event_id INTEGER, field TEXT, slot INTEGER, value TEXT,
  submitted_at TEXT, PRIMARY KEY(user_id, year, event_id, field, slot));
CREATE TABLE IF NOT EXISTS results(
  event_id INTEGER, field TEXT, slot INTEGER, value TEXT, source TEXT, updated_at TEXT,
  PRIMARY KEY(event_id, field, slot));
CREATE TABLE IF NOT EXISTS standings(
  year INTEGER, position INTEGER, tla TEXT, points REAL, updated_at TEXT,
  PRIMARY KEY(year, position));
CREATE TABLE IF NOT EXISTS team_standings(
  year INTEGER, position INTEGER, team TEXT, points REAL, updated_at TEXT,
  PRIMARY KEY(year, position));
CREATE TABLE IF NOT EXISTS dotd_votes(
  event_id INTEGER, rank INTEGER, tla TEXT, name TEXT, pct REAL, link TEXT,
  PRIMARY KEY(event_id, rank));
CREATE TABLE IF NOT EXISTS penalties(
  id INTEGER PRIMARY KEY, user_id INTEGER, event_id INTEGER, points INTEGER, reason TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS scores(
  user_id INTEGER, event_id INTEGER, year INTEGER, points INTEGER, detail TEXT,
  PRIMARY KEY(user_id, event_id));
CREATE TABLE IF NOT EXISTS season_scores(
  user_id INTEGER, year INTEGER, points INTEGER, PRIMARY KEY(user_id, year));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY, ts TEXT, level TEXT, msg TEXT);
"""


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def q(sql, *args):
    return conn().execute(sql, args).fetchall()


def q1(sql, *args):
    return conn().execute(sql, args).fetchone()


def ex(sql, *args):
    return conn().execute(sql, args)


def init():
    conn().executescript(SCHEMA)


def apply_driver_swaps(c, event_id):
    """Keep admin-entered driver replacements in force, even when F1's driver list is still the old one."""
    for sw in c.execute("SELECT * FROM driver_swaps WHERE event_id=?", (event_id,)).fetchall():
        has_in = c.execute("SELECT 1 FROM entrants WHERE event_id=? AND tla=?", (event_id, sw["in_tla"])).fetchone()
        if has_in:
            c.execute("DELETE FROM entrants WHERE event_id=? AND tla=?", (event_id, sw["out_tla"]))
        else:
            c.execute("UPDATE entrants SET tla=?, name=?, headshot='' WHERE event_id=? AND tla=?",
                      (sw["in_tla"], sw["in_name"], event_id, sw["out_tla"]))


def setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", key)
    return r["value"] if r else default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       key, value)


def logmsg(level, msg):
    ex("INSERT INTO log(ts,level,msg) VALUES(?,?,?)", iso(now_utc()), level, msg[:2000])
    ex("DELETE FROM log WHERE id < (SELECT MAX(id)-500 FROM log)")
