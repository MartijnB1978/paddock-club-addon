"""Current weather at the circuit, from Open-Meteo (free, no API key). Refreshed by the engine every 10 minutes."""
import json
import logging

import requests

from . import db

log = logging.getLogger("weather")
GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
_cache = {}           # event id -> {'icon', 'label', 'temp'}

# WMO weather codes -> (day icon, night icon, label)
CODES = [
    ((0,), "☀️", "🌙", "Clear"),
    ((1,), "🌤️", "🌙", "Mostly clear"),
    ((2,), "⛅", "☁️", "Partly cloudy"),
    ((3,), "☁️", "☁️", "Cloudy"),
    ((45, 48), "🌫️", "🌫️", "Fog"),
    ((51, 53, 55, 56, 57), "🌦️", "🌧️", "Drizzle"),
    ((61, 63, 65, 66, 67, 80, 81, 82), "🌧️", "🌧️", "Rain"),
    ((71, 73, 75, 77, 85, 86), "🌨️", "🌨️", "Snow"),
    ((95, 96, 99), "⛈️", "⛈️", "Thunderstorm"),
]


def _describe(code, is_day):
    for codes, day, night, label in CODES:
        if code in codes:
            return (day if is_day else night), label
    return "🌡️", "Weather"


def _coords(ev):
    """Circuit location -> (lat, lon), looked up once and kept in settings."""
    for name in (ev["location"], ev["country"]):
        if not name:
            continue
        key = f"geo:{name}"
        hit = db.setting(key)
        if hit:
            lat, lon = json.loads(hit)
            return lat, lon
        r = requests.get(GEO, params={"name": name, "count": 1}, timeout=8)
        res = (r.json() or {}).get("results") if r.ok else None
        if res:
            db.set_setting(key, json.dumps([res[0]["latitude"], res[0]["longitude"]]))
            return res[0]["latitude"], res[0]["longitude"]
    return None


def refresh(event_ids):
    for eid in event_ids:
        ev = db.q1("SELECT * FROM events WHERE id=?", eid)
        try:
            ll = _coords(ev) if ev else None
            if not ll:
                continue
            r = requests.get(FORECAST, params={"latitude": ll[0], "longitude": ll[1],
                                               "current": "temperature_2m,weather_code,is_day"}, timeout=8)
            cur = (r.json() or {}).get("current") if r.ok else None
            if cur:
                icon, label = _describe(int(cur.get("weather_code", -1)), cur.get("is_day", 1))
                _cache[eid] = {"icon": icon, "label": label, "temp": round(cur.get("temperature_2m", 0))}
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            log.debug("weather for %s failed: %s", eid, exc)


def get(event_id, live_data=None):
    """Cached weather for an event; the live F1 feed's rain sensor overrides the icon while a session runs."""
    w = _cache.get(event_id)
    rain = str(((live_data or {}).get("WeatherData") or {}).get("Rainfall") or "0") not in ("0", "")
    if rain:
        w = dict(w or {"temp": None}) | {"icon": "🌧️", "label": "Rain at the track"}
    return w
