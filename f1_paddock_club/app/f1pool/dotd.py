"""Driver of the Day (fan vote) from formula1.com.

F1 publishes a "DRIVER OF THE DAY: ..." article after each Grand Prix in its news feed,
with the top of the vote as lines like "Charles Leclerc - 25.7%".
"""
import html as htmllib
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("dotd")
FEED = "https://www.formula1.com/en/latest/all.xml"
UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
VOTE_RE = re.compile(r"([^\W\d_][^\W\d_'’.\- ]*(?:[ '’.\-]+[^\W\d_][^\W\d_'’.\-]*){0,3})\s*[-–—:]\s*"
                     r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%")


def _get(url, timeout=20):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=timeout)
    r.raise_for_status()
    return r.text


def feed_articles():
    """[(title, link)] of the 'DRIVER OF THE DAY' articles currently in the news feed."""
    root = ET.fromstring(_get(FEED).encode("utf-8"))
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        if title.upper().startswith("DRIVER OF THE DAY") and link:
            out.append((title, link))
    return out


def _plain(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).strip()


def parse_votes(page_html):
    """[(name, pct)] in vote order from the article HTML."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page_html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", text)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
    votes, seen = [], set()
    for m in VOTE_RE.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -–—:")
        pct = float(m.group(2).replace(",", "."))
        key = _plain(name)
        if key and key not in seen and 0 < pct <= 100:
            seen.add(key)
            votes.append((name, pct))
    return votes


def match_driver(name, entrants):
    """Map a full name to a TLA using the weekend's entry list."""
    p = _plain(name)
    by_full = {_plain(e["name"]): e["tla"] for e in entrants}
    if p in by_full:
        return by_full[p]
    last = p.split(" ")[-1] if p else ""
    hits = {e["tla"] for e in entrants if _plain(e["name"]).split(" ")[-1:] == [last]}
    if len(hits) == 1:
        return hits.pop()
    for full, tla in by_full.items():
        if p and (p in full or full in p):
            return tla
    return None


def find_for_race(entrants, already_seen=()):
    """Look for a new Driver of the Day article. Returns dict or None.

    Only articles that were not in the feed before the race started (already_seen) are accepted,
    so last race's article is never used for this one.
    """
    for title, link in feed_articles():
        if link in already_seen:
            continue
        page = _get(link)
        votes = parse_votes(page)
        rows = []
        for name, pct in votes:
            tla = match_driver(name, entrants)
            if tla:
                rows.append({"name": name, "tla": tla, "pct": pct})
        if rows:
            return {"title": title, "link": link, "votes": rows[:10]}
        # winner from the headline, e.g. "DRIVER OF THE DAY: Leclerc gets your vote ..."
        winner = headline_winner(title, entrants)
        if winner:
            return {"title": title, "link": link, "votes": [{"name": winner["name"], "tla": winner["tla"], "pct": None}]}
    return None


def headline_winner(title, entrants):
    """The driver named first in 'DRIVER OF THE DAY: Leclerc gets your vote ...'."""
    head = _plain(title.split(":", 1)[-1])
    best = None
    for e in entrants:
        last = _plain(e["name"]).split(" ")[-1]
        m = re.search(rf"\b{re.escape(last)}\b", head) if last else None
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), e)
    return best[1] if best else None


def from_article(link, entrants):
    """Parse a given Driver of the Day article URL (admin can paste it)."""
    page = _get(link)
    rows = []
    for name, pct in parse_votes(page):
        tla = match_driver(name, entrants)
        if tla:
            rows.append({"name": name, "tla": tla, "pct": pct})
    if not rows:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
        w = headline_winner(htmllib.unescape(m.group(1)) if m else "", entrants)
        if w:
            rows = [{"name": w["name"], "tla": w["tla"], "pct": None}]
    return {"title": "", "link": link, "votes": rows[:10]} if rows else None
