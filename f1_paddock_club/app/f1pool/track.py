"""Live track map: circuit outline + car positions in one coordinate frame.

Outline: MultiViewer's free circuit data (same coordinate system as F1's car position feed), cached per
circuit and year. Fallback when it is unavailable: the track drawn from where the cars have been."""
import json
import logging
import math

import requests

from . import db

log = logging.getLogger("track")
MV_URL = "https://api.multiviewer.app/api/v1/circuits/{key}/{year}"
PAD = 400          # feed units of margin around the track


def outline(key, year):
    """{'x': [...], 'y': [...], 'rotation': deg} or None; cached in settings (also a miss, for an hour)."""
    if not key:
        return None
    ck = f"track:{key}:{year}"
    hit = db.setting(ck)
    if hit:
        d = json.loads(hit)
        if d.get("x"):
            return d
        if d.get("miss_at", 0) > db.now_utc().timestamp() - 3600:
            return None
    d = {"miss_at": db.now_utc().timestamp()}
    try:
        r = requests.get(MV_URL.format(key=key, year=year), timeout=10,
                         headers={"User-Agent": "PaddockClubReeuwijk/1.0"})
        j = r.json() if r.ok else {}
        if j.get("x") and j.get("y"):
            d = {"x": j["x"], "y": j["y"], "rotation": float(j.get("rotation") or 0)}
    except (requests.RequestException, ValueError) as exc:
        log.debug("circuit %s: %s", key, exc)
    db.set_setting(ck, json.dumps(d))
    return d if d.get("x") else None


class Frame:
    """Rotate like the official map, flip y (SVG y points down), fit a viewBox."""

    def __init__(self, xs, ys, rotation=0.0):
        self.rot = math.radians(rotation)
        pts = [self.tf(x, y) for x, y in zip(xs, ys)]
        if pts:
            minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
            miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
        else:
            minx = miny = -1000
            maxx = maxy = 1000
        self.box = (minx - PAD, miny - PAD, maxx - minx + 2 * PAD, maxy - miny + 2 * PAD)
        self.pts = pts

    def tf(self, x, y):
        c, s = math.cos(self.rot), math.sin(self.rot)
        return (x * c - y * s, -(x * s + y * c))

    @property
    def viewbox(self):
        return " ".join(f"{v:.0f}" for v in self.box)

    def path(self):
        if not self.pts:
            return ""
        return "M" + " L".join(f"{x:.0f} {y:.0f}" for x, y in self.pts) + " Z"


def build(key, year, trace):
    """Frame + drawing for the map: the outline when known, otherwise the cars' trace as dots."""
    o = outline(key, year)
    if o:
        f = Frame(o["x"], o["y"], o["rotation"])
        return f, {"viewBox": f.viewbox, "path": f.path(), "dots": [], "key": f"{key}:{year}"}
    xs, ys = [p[0] for p in trace], [p[1] for p in trace]
    f = Frame(xs, ys, 0)
    return f, {"viewBox": f.viewbox, "path": "", "dots": [[round(x), round(y)] for x, y in f.pts],
               "key": f"trace:{len(trace) // 200}"}
