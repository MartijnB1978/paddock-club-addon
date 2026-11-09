"""Driver of the Day (fan vote) and the DHL fastest pit stop from formula1.com.

F1 publishes a "DRIVER OF THE DAY: ..." article after each Grand Prix, with the top of the vote as
lines like "Charles Leclerc - 25.7%" or (since late 2026) "Max Verstappen 31.3%". The article is looked
for in the news feed first and, if the feed misses it, in the 'latest news' pages.

The DHL Fastest Pit Stop Award table (formula1.com/en/results/<year>/awards/fastest-pit-stops) lists the
team with the fastest stop per Grand Prix; it is the official figure, the live timing feed only an estimate.
"""
import html as htmllib
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests

log = logging.getLogger("dotd")
BASE = "https://www.formula1.com"
FEED = BASE + "/en/latest/all.xml"
LISTINGS = [BASE + "/en/latest", BASE + "/en/latest/all"]
PITSTOP_AWARDS = BASE + "/en/results/{year}/awards/fastest-pit-stops"
UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
_NAME = r"[^\W\d_][^\W\d_'’.\- ]*(?:[ '’.\-]+[^\W\d_][^\W\d_'’.\-]*){0,3}"
_PCT = r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%"
# separator between name and percentage is optional: "Leclerc - 25.7%" and "Max Verstappen 31.3%"
VOTE_RE = re.compile(rf"({_NAME})\s*(?:[-–—:]\s*)?{_PCT}")
LINE_RE = re.compile(rf"^\s*({_NAME})\s*(?:[-–—:]\s*)?{_PCT}\s*$", re.M)
ARTICLE_HREF_RE = re.compile(r"""href=["']([^"'<>\s]*/latest/article/driver-of-the-day[^"'<>\s]*)["']""", re.I)
GENERIC = {"grand", "prix", "gp", "the", "de", "of", "city"}


def _get(url, timeout=20):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=timeout)
    r.raise_for_status()
    return r.text


def _plain(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).strip()


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", _plain(s).replace(" ", "-")).strip("-")


def _is_dotd(title, link):
    return "driver of the day" in _plain(title) or "driver-of-the-day" in (link or "").lower()


def feed_articles():
    """[(title, link)] of the Driver of the Day articles currently in the news feed."""
    root = ET.fromstring(_get(FEED).encode("utf-8"))
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        if link and _is_dotd(title, link):
            out.append((title, link))
    return out


def listing_articles():
    """[('', link)] of Driver of the Day articles linked from the 'latest news' pages."""
    out, seen = [], set()
    for page in LISTINGS:
        try:
            text = _get(page)
        except requests.RequestException as exc:
            log.info("listing %s: %s", page, exc)
            continue
        for href in ARTICLE_HREF_RE.findall(text):
            link = urljoin(BASE, htmllib.unescape(href)).split("#")[0]
            if link not in seen:
                seen.add(link)
                out.append(("", link))
    return out


def all_articles():
    """Feed articles first, then any extra ones from the listings. Errors only if both sources fail."""
    arts, errors = [], []
    for src in (feed_articles, listing_articles):
        try:
            arts += src()
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            errors.append(f"{src.__name__}: {exc}")
    if not arts and len(errors) == 2:
        raise RuntimeError("; ".join(errors))
    out, seen = [], set()
    for title, link in arts:
        key = link.split("?")[0].rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append((title, link))
    return out


def _page_text(page_html):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page_html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>|</tr>", "\n", text)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t ]+", " ", text)


def parse_votes(page_html):
    """[(name, pct)] in vote order from the article HTML."""
    text = _page_text(page_html)
    found = LINE_RE.findall(text)          # one "Name 31.3%" per line: the vote list itself
    if len(found) < 2:
        found = VOTE_RE.findall(text)      # all on one line (or an older layout)
    votes, seen = [], set()
    for name, pct in found:
        name = re.sub(r"\s+", " ", name).strip(" -–—:")
        pct = float(pct.replace(",", "."))
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
        if p and full and (p in full or full in p):
            return tla
    return None


def event_words(ev):
    """Words that identify a Grand Prix in a headline or URL, e.g. {'sao paulo', 'brazil'}."""
    words = set()
    for f in ("name", "location", "country"):
        w = " ".join(t for t in _plain((ev or {}).get(f) or "").split() if t not in GENERIC)
        if len(w) >= 4:
            words.add(w)
    return words


def mentions_event(title, link, ev):
    hay = _plain(title) + " " + _plain((link or "").replace("-", " "))
    return any(w in hay for w in event_words(ev))


def _title_of(page):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    return htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def _result(title, link, page, entrants):
    rows = []
    for name, pct in parse_votes(page):
        tla = match_driver(name, entrants)
        if tla and tla not in {r["tla"] for r in rows}:
            rows.append({"name": name, "tla": tla, "pct": pct})
    if rows:
        return {"title": title, "link": link, "votes": rows[:10]}
    # winner from the headline, e.g. "DRIVER OF THE DAY: Leclerc gets your vote ..."
    winner = headline_winner(title, entrants)
    if winner:
        return {"title": title, "link": link, "votes": [{"name": winner["name"], "tla": winner["tla"], "pct": None}]}
    return None


def find_for_race(entrants, already_seen=(), ev=None):
    """Look for a new Driver of the Day article. Returns dict or None.

    Articles that were already published before the race started (already_seen) are skipped, so last
    race's article is never used for this one. An article found only via the listings (not in the
    pre-race snapshot) must also name this Grand Prix in its headline or URL.
    """
    seen = {s.split("?")[0].rstrip("/") for s in already_seen}
    from_feed = set()
    try:
        from_feed = {link for _t, link in feed_articles()}
    except Exception:  # noqa: BLE001  all_articles reports the error if nothing else works
        pass
    for title, link in all_articles():
        if link.split("?")[0].rstrip("/") in seen:
            continue
        page = _get(link)
        title = title or _title_of(page)
        if not _is_dotd(title, link):
            continue
        if ev and link not in from_feed and not mentions_event(title, link, ev):
            continue
        if ev and not mentions_event(title, link, ev) and event_words(ev) and \
                re.search(r"\bin [A-Z]", title.split(":", 1)[-1]):
            continue  # headline names another Grand Prix ("... in Mexico")
        found = _result(title, link, page, entrants)
        if found:
            return found
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
    found = _result(_title_of(page), link, page, entrants)
    if found:
        found["title"] = ""
    return found


# ------------------------------------------------------------ DHL fastest pit stop
def parse_pitstop_awards(page_html):
    """[{'slug': 'brazil', 'team': 'Audi', 'logo': 'audi', 'time': 2.13}] from the awards table."""
    out = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page_html):
        m = re.search(r"/results/\d{4}/races/\d+/([a-z0-9-]+)/race-result", row)
        if not m:
            continue
        cells = [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
        t = re.search(r"(\d{1,2}[.,]\d{1,3})\s*s\b", " ".join(cells))
        logo = re.search(r"/common/f1/\d{4}/([a-z0-9]+)/", row)
        out.append({"slug": m.group(1),
                    "team": cells[1] if len(cells) >= 3 else "",
                    "logo": logo.group(1) if logo else "",
                    "time": float(t.group(1).replace(",", ".")) if t else None})
    return out


def pitstop_row_for_event(rows, ev, year_events):
    """The awards row for this Grand Prix, matched on the race slug ('brazil', 'mexico', 'las-vegas')."""
    def score(e, slug):
        if slug and slug in _slug(e["name"]):
            return 3
        if slug in (_slug(e["location"] or ""), _slug(e["country"] or "")):
            return 2
        return 0
    for r in rows:
        ranked = sorted(((score(e, r["slug"]), e["id"]) for e in year_events), reverse=True)
        if ranked and ranked[0][0] and ranked[0][1] == ev["id"] and \
                (len(ranked) == 1 or ranked[1][0] < ranked[0][0]):
            return r
    return None


def match_team(row, teams):
    """Map the awards row to one of the weekend's team names ('Audi', 'Red Bull Racing')."""
    want = re.sub(r"[^a-z0-9]", "", _plain(row.get("team") or ""))
    logo = row.get("logo") or ""
    keys = {t: re.sub(r"[^a-z0-9]", "", _plain(t)) for t in teams if t}
    for t, k in keys.items():
        if want and k == want:
            return t
    for t, k in keys.items():
        if logo and (k == logo or k.startswith(logo) or logo.startswith(k)):
            return t
    for t, k in keys.items():
        if want and k and (want in k or k in want):
            return t
    return None


def fastest_pitstop(ev, year_events, teams):
    """(team, seconds) of the DHL fastest pit stop for this Grand Prix, or None if not published yet."""
    rows = parse_pitstop_awards(_get(PITSTOP_AWARDS.format(year=ev["year"])))
    if not rows:
        raise RuntimeError("no rows found in the DHL fastest pit stop table (page layout changed?)")
    row = pitstop_row_for_event(rows, ev, year_events)
    if not row:
        return None
    team = match_team(row, teams)
    if not team:
        raise RuntimeError(f"DHL table names team '{row['team'] or row['logo']}', not in this weekend's entry list")
    return team, row["time"]
