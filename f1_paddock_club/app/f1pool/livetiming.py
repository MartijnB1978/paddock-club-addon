"""Formula 1 live timing: state handling, live SignalR client and static
archive access (livetiming.formula1.com, the official F1 timing source)."""
import base64
import json
import logging
import threading
import time
import zlib

import requests

log = logging.getLogger("livetiming")

BASE = "https://livetiming.formula1.com"
MIRROR = "https://livetiming-mirror.fastf1.dev"   # FastF1 project's mirror of the same files
SIGNALR_URL = "wss://livetiming.formula1.com/signalrcore"
NEGOTIATE_URL = "https://livetiming.formula1.com/signalrcore/negotiate"

TOPICS = ["Heartbeat", "DriverList", "SessionInfo", "SessionStatus", "SessionData",
          "TimingData", "TimingStats", "TimingAppData", "LapCount", "TrackStatus",
          "ExtrapolatedClock", "TopThree", "ChampionshipPrediction",
          "PitStopSeries", "PitStop", "PitLaneTimeCollection", "RaceControlMessages", "LapSeries"]
# topics delivered as events (each message replaces the previous one)
EVENT_TOPICS = {"PitStop"}
STATIC_TOPICS = ["DriverList", "SessionInfo", "SessionStatus", "TimingData", "TimingStats",
                 "ChampionshipPrediction", "PitStopSeries", "PitStop"]


# ----------------------------------------------------------------- state ---
def _norm(v):
    if isinstance(v, list):
        return {str(i): _norm(x) for i, x in enumerate(v)}
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    return v


def merge(dst: dict, src: dict):
    for k, v in src.items():
        if k == "_deleted":
            continue
        v = _norm(v)
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v


def decode(topic, data):
    if topic.endswith(".z") and isinstance(data, str):
        data = json.loads(zlib.decompress(base64.b64decode(data), -zlib.MAX_WBITS))
    return data


class TimingState:
    """Accumulated session state built from keyframes + deltas."""

    def __init__(self):
        self.data = {}
        self.pit_events = []        # [(racing_number, stop_seconds)]
        self.updated = 0.0
        self.lock = threading.Lock()

    def apply(self, topic, payload):
        try:
            payload = decode(topic, payload)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(payload, dict):
            return
        with self.lock:
            if topic in EVENT_TOPICS:
                self._pit_event(payload)
                self.data[topic] = payload
            else:
                merge(self.data.setdefault(topic, {}), payload)
            self.updated = time.time()

    def _pit_event(self, p):
        try:
            self.pit_events.append((str(p["RacingNumber"]), float(p["PitStopTime"])))
        except (KeyError, TypeError, ValueError):
            pass

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data)), list(self.pit_events)


# --------------------------------------------------------- derive results --
def _laptime(s):
    try:
        s = str(s).strip()
        if not s:
            return None
        if ":" in s:
            m, sec = s.split(":", 1)
            return int(m) * 60 + float(sec)
        return float(s)
    except ValueError:
        return None


def driver_info(data):
    out = {}
    for num, d in (data.get("DriverList") or {}).items():
        if isinstance(d, dict) and d.get("Tla"):
            out[str(num)] = {
                "number": str(num), "tla": d.get("Tla"),
                "name": f"{d.get('FirstName', '')} {d.get('LastName', '')}".strip() or d.get("FullName", ""),
                "team": d.get("TeamName", ""), "colour": d.get("TeamColour", ""),
                "headshot": d.get("HeadshotUrl", ""), "line": d.get("Line", 99),
            }
    return out


def classification(data):
    """Racing numbers ordered by current/final position."""
    lines = (data.get("TimingData") or {}).get("Lines") or {}
    rows = []
    for num, ln in lines.items():
        if not isinstance(ln, dict):
            continue
        try:
            pos = int(ln.get("Position"))
        except (TypeError, ValueError):
            pos = 999
        rows.append((pos, ln.get("Line", 999), str(num)))
    rows.sort()
    return [r[2] for r in rows if r[0] != 999] + [r[2] for r in rows if r[0] == 999]


def fastest_lap_number(data):
    stats = (data.get("TimingStats") or {}).get("Lines") or {}
    for num, ln in stats.items():
        pb = (ln or {}).get("PersonalBestLapTime") or {}
        if str(pb.get("Position")) == "1" and pb.get("Value"):
            return str(num)
    best, who = None, None
    for num, ln in ((data.get("TimingData") or {}).get("Lines") or {}).items():
        t = _laptime(((ln or {}).get("BestLapTime") or {}).get("Value"))
        if t and (best is None or t < best):
            best, who = t, str(num)
    return who


def fastest_pitstop_number(data, pit_events):
    best, who = None, None
    for num, t in pit_events:
        if t > 0 and (best is None or t < best):
            best, who = t, num
    for num, stops in (((data.get("PitStopSeries") or {}).get("PitTimes")) or {}).items():
        if not isinstance(stops, dict):
            continue
        for st in stops.values():
            try:
                t = float(((st or {}).get("PitStop") or {}).get("PitStopTime"))
            except (TypeError, ValueError):
                continue
            if t > 0 and (best is None or t < best):
                best, who = t, str(num)
    return who, best


def championship(data):
    """Live/after-session drivers' standings: [(position, racing_number, points)]"""
    out = []
    for num, d in (((data.get("ChampionshipPrediction") or {}).get("Drivers")) or {}).items():
        if not isinstance(d, dict):
            continue
        pos = d.get("PredictedPosition") or d.get("CurrentPosition")
        if pos:
            out.append((int(pos), str(num), d.get("PredictedPoints", d.get("CurrentPoints"))))
    out.sort()
    return out


def championship_teams(data):
    """Live/after-session constructors' standings: [(position, team name, points)]"""
    out = []
    for key, d in (((data.get("ChampionshipPrediction") or {}).get("Teams")) or {}).items():
        if not isinstance(d, dict):
            continue
        pos = d.get("PredictedPosition") or d.get("CurrentPosition")
        if pos:
            out.append((int(pos), d.get("TeamName") or key, d.get("PredictedPoints", d.get("CurrentPoints"))))
    out.sort()
    return out


def session_status(data):
    st = (data.get("SessionStatus") or {}).get("Status")
    if not st:
        series = ((data.get("SessionData") or {}).get("StatusSeries")) or {}
        for k in sorted(series, key=lambda x: int(x) if str(x).isdigit() else 0):
            if (series[k] or {}).get("SessionStatus"):
                st = series[k]["SessionStatus"]
    return st


def is_finished(data):
    """Classification is final: every car has taken the chequered flag."""
    return session_status(data) in ("Finalised", "Ends")


def flag_out(data):
    """Chequered flag shown; cars may still be completing their last lap."""
    return session_status(data) == "Finished"


def session_part(data):
    """Qualifying part (1-3) or None."""
    part = (data.get("TimingData") or {}).get("SessionPart")
    if not part:
        series = ((data.get("SessionData") or {}).get("Series")) or {}
        for k in sorted(series, key=lambda x: int(x) if str(x).isdigit() else 0):
            if (series[k] or {}).get("QualifyingPart"):
                part = series[k]["QualifyingPart"]
    try:
        return int(part) if part else None
    except (TypeError, ValueError):
        return None


def clock(data):
    """{'remaining': seconds at 'utc', 'running': bool} from ExtrapolatedClock, or None."""
    c = data.get("ExtrapolatedClock") or {}
    rem = c.get("Remaining")
    if not rem:
        return None
    try:
        h, m, sec = (float(x) for x in rem.split(":"))
    except ValueError:
        return None
    return {"remaining": int(h * 3600 + m * 60 + sec), "utc": c.get("Utc"), "running": bool(c.get("Extrapolating"))}


TRACK_FLAGS = {"2": ("yellow", "Yellow flag"), "4": ("sc", "Safety car"), "5": ("red", "Red flag"),
               "6": ("vsc", "VSC"), "7": ("vsc", "VSC ending")}


def track_flag(data):
    """{'kind', 'label', 'restart_utc'} for a yellow / SC / VSC / red flag, None when the track is clear."""
    st = str((data.get("TrackStatus") or {}).get("Status") or "")
    if st not in TRACK_FLAGS:
        return None
    kind, label = TRACK_FLAGS[st]
    return {"kind": kind, "label": label, "restart_utc": restart_utc(data) if st == "5" else None}


def restart_utc(data):
    """Restart time announced by Race Control after the latest red flag (ISO UTC), or None.
    Race Control gives it in track-local time ('RESUMPTION AT 15:45'); converted with the session's GMT offset."""
    import re
    from datetime import datetime, timedelta, timezone
    msgs = (data.get("RaceControlMessages") or {}).get("Messages") or {}
    items = [v for _k, v in sorted(msgs.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else -1)] \
        if isinstance(msgs, dict) else list(msgs)
    off = str((data.get("SessionInfo") or {}).get("GmtOffset") or "00:00:00")
    try:
        sign = -1 if off.startswith("-") else 1
        h, m = off.lstrip("+-").split(":")[:2]
        offset = timedelta(hours=int(h), minutes=int(m)) * sign
    except ValueError:
        offset = timedelta(0)
    for msg in reversed(items):
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("Message") or "").upper()
        if "RED FLAG" in text or msg.get("Flag") == "RED":
            return None                          # nothing announced since this red flag
        hit = re.search(r"\b(\d{1,2})[:.](\d{2})\b", text)
        if hit and ("RESUM" in text or "RESTART" in text or "RE-START" in text):
            try:
                sent = datetime.fromisoformat(str(msg.get("Utc"))[:19]).replace(tzinfo=timezone.utc)
            except ValueError:
                sent = datetime.now(timezone.utc)
            local = (sent + offset).replace(hour=int(hit.group(1)), minute=int(hit.group(2)), second=0)
            utc = local - offset
            if utc < sent - timedelta(hours=1):
                utc += timedelta(days=1)
            return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def lap_count(data):
    """(current lap, total laps) from LapCount; (0, 0) when unknown."""
    lc = data.get("LapCount") or {}
    try:
        return int(lc.get("CurrentLap") or 0), int(lc.get("TotalLaps") or 0)
    except (TypeError, ValueError):
        return 0, 0


def race_underway(data):
    """True once the race has started and at least one full lap is done.

    Before lights out the feed carries the grid plus leftover best-lap and
    pit values from earlier sessions; those must not show as results.
    """
    if session_status(data) in (None, "", "Inactive"):
        return False
    try:
        return int((data.get("LapCount") or {}).get("CurrentLap") or 0) >= 2
    except (TypeError, ValueError):
        return False


def derive(kind, data, pit_events, drivers=None):
    """Turn timing state into result fields for our game.

    kind: R (race), Q (qualifying), S (sprint), SQ (sprint qualifying)
    Returns {field: {slot: value}} with driver TLAs / team names.
    """
    drv = driver_info(data)
    if drivers:
        for k, v in drivers.items():
            drv.setdefault(k, v)

    def tla(num):
        return drv.get(num, {}).get("tla") or f"#{num}"
    order = classification(data)
    res = {}
    if not order:
        return res
    if kind == "R":
        res["race_pos"] = {i + 1: tla(n) for i, n in enumerate(order)}
        if not race_underway(data):
            return res
        fl = fastest_lap_number(data)
        if fl:
            res["fl"] = {1: tla(fl)}
        fp, _t = fastest_pitstop_number(data, pit_events)
        if fp and drv.get(fp, {}).get("team"):
            res["fp"] = {1: drv[fp]["team"]}
    elif kind == "Q":
        res["pole"] = {1: tla(order[0])}
    elif kind == "SQ":
        res["sprint_pole"] = {1: tla(order[0])}
    elif kind == "S":
        res["sprint_pos"] = {i + 1: tla(n) for i, n in enumerate(order)}
    return res


# ------------------------------------------------------------- static -----
def parse_stream(text):
    out = []
    for line in text.lstrip("﻿").splitlines():
        line = line.strip().lstrip("﻿")
        if len(line) < 13:
            continue
        body = line[12:]
        try:
            out.append(json.loads(body) if body[0] == "{" else json.loads(body))
        except (ValueError, IndexError):
            continue
    return out


class F1Http:
    def __init__(self, tokens):
        self.tokens = tokens
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "PaddockClubReeuwijk/1.0"
        self.last_status = ""
        self.timing_status = ""

    def get(self, path, timeout=30):
        """GET a static file; tries F1 (with token) then the mirror."""
        tok = self.tokens.get() if self.tokens else None
        headers = {"Authorization": f"Bearer {tok.value}"} if tok else {}
        statuses = []
        for base in (BASE, MIRROR):
            try:
                r = self.s.get(f"{base}/static/{path}", headers=headers if base == BASE else {},
                               timeout=timeout)
                statuses.append(f"{'F1' if base == BASE else 'mirror'} {r.status_code}")
                self.last_status = ", ".join(statuses)
                if r.status_code == 401 and tok and base == BASE:
                    self.tokens.clear()
                if r.status_code == 200 and r.content:
                    r.encoding = "utf-8-sig"
                    return r.text
            except requests.RequestException as exc:
                log.debug("static fetch %s failed: %s", base, exc)
                statuses.append(f"{'F1' if base == BASE else 'mirror'} unreachable")
                self.last_status = ", ".join(statuses)
        return None

    def index(self, year):
        t = self.get(f"{year}/Index.json")
        if not t:
            return None
        try:
            return json.loads(t.lstrip("﻿"))
        except ValueError:
            return None

    def session_state(self, path):
        """Rebuild the final state of a session from the archive."""
        st = TimingState()
        got = False
        for topic in STATIC_TOPICS:
            t = self.get(f"{path}{topic}.jsonStream")
            if topic == "TimingData":
                self.timing_status = self.last_status
            if not t:
                continue
            got = True
            for msg in parse_stream(t):
                st.apply(topic, msg)
        return st if got else None


# ------------------------------------------------------------- live -------
class LiveClient:
    """SignalR Core connection to the live timing feed."""

    def __init__(self, tokens, on_update):
        self.tokens = tokens
        self.on_update = on_update
        self.state = TimingState()
        self.conn = None
        self.connected = False
        self.last_msg = 0.0
        self.error = None

    def _on_feed(self, msg):
        # msg: [topic, data, timestamp]
        try:
            if isinstance(msg, list) and len(msg) >= 2 and isinstance(msg[0], str):
                self.state.apply(msg[0], msg[1])
                self.last_msg = time.time()
                self.on_update()
        except Exception:  # noqa: BLE001
            log.exception("feed message failed")

    def _on_subscribe(self, msg):
        result = getattr(msg, "result", None)
        if isinstance(result, dict):
            for topic, data in result.items():
                self.state.apply(topic, data)
            self.last_msg = time.time()
            self.on_update()

    def start(self):
        from signalrcore.hub_connection_builder import HubConnectionBuilder
        headers = {"User-Agent": "BestHTTP"}
        try:
            r = requests.options(NEGOTIATE_URL, headers=headers, timeout=15)
            if "AWSALBCORS" in r.cookies:
                headers["Cookie"] = f"AWSALBCORS={r.cookies['AWSALBCORS']}"
        except requests.RequestException:
            pass
        tok = self.tokens.get() if self.tokens else None
        options = {"verify_ssl": True, "headers": headers}
        if tok:
            options["access_token_factory"] = lambda: (self.tokens.get() or tok).value
        self.conn = (HubConnectionBuilder()
                     .with_url(SIGNALR_URL, options=options)
                     .with_automatic_reconnect({"type": "raw", "keep_alive_interval": 10,
                                                "reconnect_interval": 5, "max_attempts": 10_000})
                     .build())
        self.conn.on_open(self._opened)
        self.conn.on_reconnect(self._opened)
        self.conn.on_close(lambda: setattr(self, "connected", False))
        self.conn.on_error(lambda e: setattr(self, "error", str(e)))
        self.conn.on("feed", self._on_feed)
        self.conn.start()

    def _opened(self):
        self.connected = True
        self.error = None
        try:
            self.conn.send("Subscribe", [TOPICS], on_invocation=self._on_subscribe)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)

    def stop(self):
        try:
            if self.conn:
                self.conn.stop()
        except Exception:  # noqa: BLE001
            pass
        self.connected = False
