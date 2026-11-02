"""FIA decision documents: the official source for classifications, plus the stewards' decisions.

Per race weekend the FIA publishes ~60 PDFs (fia.com/documents). Used here:
  - Final Race / Sprint / Qualifying / Sprint Qualifying Classification -> results (source 'fia')
  - Infringements, decisions and summonses                              -> 'Stewards' list on Live standings
Results from the FIA rank above Jolpica and F1 timing data; only admin corrections rank higher."""
import io
import logging
import re
import unicodedata
from datetime import timedelta
from urllib.parse import quote, urljoin

import requests

from . import db, mailer

log = logging.getLogger("fia")
BASE = "https://www.fia.com"
CHAMP = BASE + "/documents/championships/fia-formula-one-world-championship-14"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36 PaddockClubReeuwijk/1.0"}
WATCH_BEFORE = timedelta(days=4)       # start watching on the Thursday of the weekend
WATCH_AFTER = timedelta(days=3)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fia_docs(
  event_id INTEGER, url TEXT, title TEXT, published TEXT, kind TEXT, parsed INTEGER DEFAULT 0,
  first_seen TEXT, PRIMARY KEY(event_id, url));
"""

# document title -> (results field, session kind); first match wins
CLASSIFICATIONS = [
    ("final sprint qualifying classification", "sprint_pole", "SQ"),
    ("final sprint shootout classification", "sprint_pole", "SQ"),
    ("final sprint classification", "sprint_pos", "S"),
    ("final qualifying classification", "pole", "Q"),
    ("final race classification", "race_pos", "R"),
]
LABEL = {"race_pos": "P", "sprint_pos": "Sprint P", "pole": "Pole", "sprint_pole": "Sprint pole",
         "fl": "Fastest lap"}
STEWARD_RE = re.compile(r"^(infringe?ment|decision|summons|offence|reprimand|penalty|stewards)", re.I)


def init():
    db.conn().executescript(SCHEMA)
    for d in db.q("SELECT rowid, title, published FROM fia_docs WHERE title LIKE '%Published on%'"):
        m = re.search(r"Published on\s+(\d{2}\.\d{2}\.\d{2,4}\s+\d{2}:\d{2})", d["title"])
        db.ex("UPDATE fia_docs SET title=?, published=? WHERE rowid=?",
              re.split(r"\s+Published on\b", d["title"])[0].strip(),
              d["published"] or (m.group(1) if m else ""), d["rowid"])


def _plain(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def _get(url, timeout=25):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def season_path(year):
    """'season/season-2026-2072' (the id is FIA's own); looked up once per season and cached."""
    key = f"fia_season:{year}"
    hit = db.setting(key)
    if hit:
        return hit
    m = re.search(rf"(season/season-{year}-\d+)", _get(CHAMP).text)
    if not m:
        raise ValueError(f"FIA season {year} not found on {CHAMP}")
    db.set_setting(key, m.group(1))
    return m.group(1)


def list_docs(ev):
    """[(title, url, published)] newest first, from the event's FIA documents page."""
    url = f"{CHAMP}/{season_path(ev['year'])}/event/{quote(_plain(ev['name']))}"
    html = _get(url).text
    out = []
    # each document is an <a href="...pdf"> with the title inside; the published time follows nearby
    for m in re.finditer(r'href="([^"]+?\.pdf)"[^>]*>(.*?)</a>(?=(.{0,400}))', html, re.S | re.I):
        href, inner, tail = m.groups()
        title = re.sub(r"<[^>]+>", " ", inner)
        title = re.sub(r"\s+", " ", title).strip()
        tail = tail.split("href=", 1)[0]                     # only text before the next document
        when = re.search(r"(\d{2}\.\d{2}\.\d{2,4}\s+\d{2}:\d{2})", re.sub(r"<[^>]+>", " ", tail))
        title = re.sub(r"^Doc(ument)?\s*\d+\s*[-–:]\s*", "", title, flags=re.I)
        title = re.split(r"\s+Published on\b", title, flags=re.I)[0].strip()
        pub = re.search(r"Published on\s+(\d{2}\.\d{2}\.\d{2,4}\s+\d{2}:\d{2})", inner + " " + tail, re.I)
        when = pub or when
        if title and "/decision-document/" in href:
            out.append((title, urljoin(BASE, href), when.group(1) if when else ""))
    return out


def _kind(title):
    t = title.lower()
    if "recalled" in t:
        return None
    for needle, field, _skind in CLASSIFICATIONS:
        if needle in t:
            return field
    if STEWARD_RE.match(title.strip()):
        return "stewards"
    return "other"


# ---------------------------------------------------------- classification ---
def pdf_text(data):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


def parse_classification(text, entrants):
    """Rows (position, car number, fastest lap time or None) from an FIA classification PDF.

    Works on the token stream, so it does not depend on how the PDF splits table cells into lines:
    a row is <position> <car number> followed within a few tokens by that driver's surname, with
    positions consecutive from 1 and car numbers taken from this weekend's entry list."""
    by_no = {str(e["number"]): e for e in entrants if e["number"]}
    toks = _plain(text).split()
    starts = []                                     # (token index, position, car number)
    want = 1
    for i in range(len(toks) - 2):
        if toks[i] != str(want) or toks[i + 1] not in by_no or any(n == toks[i + 1] for _i, _p, n in starts):
            continue
        name = (by_no[toks[i + 1]]["name"] or "").split()
        surname = _plain(name[-1]).upper() if name else ""
        if surname and surname not in "".join(toks[i + 2:i + 8]).upper():     # cells may be glued together
            continue
        starts.append((i, want, toks[i + 1]))
        want += 1
    rows = []
    for k, (i, pos, no) in enumerate(starts):
        stop = starts[k + 1][0] if k + 1 < len(starts) else min(len(toks), i + 40)
        rows.append((pos, no, _fastest(" ".join(toks[i + 2:stop]))))
    return rows


TIMES = re.compile(r"\+?\d{1,2}:\d{2}:\d{2}\.\d{3}"     # race time h:mm:ss.sss
                   r"|\+\d{1,2}:\d{2}\.\d{3}|\+\d+\.\d{3}"   # gaps (+m:ss.sss, +s.sss)
                   r"|\d:\d{2}\.\d{3}")                        # lap time m:ss.sss


def _fastest(row):
    """The lap time in a row (scanning left to right, so a race time or gap glued to it is skipped)."""
    laps = [m.group(0) for m in TIMES.finditer(row) if not m.group(0).startswith("+") and m.group(0).count(":") == 1]
    return min(laps) if laps else None


def _results_from(field, rows, entrants):
    tla = {str(e["number"]): e["tla"] for e in entrants}
    if field in ("pole", "sprint_pole"):
        return {field: {1: tla[rows[0][1]]}} if rows else {}
    limit = 8 if field == "sprint_pos" else 22
    out = {field: {pos: tla[no] for pos, no, _fl in rows if pos <= limit}}
    if field == "race_pos":
        laps = [(t, no) for _p, no, t in rows if t]
        if laps:
            out["fl"] = {1: tla[min(laps)[1]]}
    return out


# ------------------------------------------------------------------ engine ---
def check(engine, now):
    """Refresh FIA documents for weekends in their watch window; apply final classifications."""
    changed = 0
    evs = db.q("SELECT e.*, MIN(s.start_utc) AS first_utc, MAX(s.start_utc) AS last_utc FROM events e "
               "JOIN sessions s ON s.event_id=e.id WHERE e.cancelled=0 AND e.imported=0 AND e.frozen=0 "
               "GROUP BY e.id HAVING first_utc<=? AND last_utc>=?",
               db.iso(now + WATCH_BEFORE), db.iso(now - WATCH_AFTER))
    for ev in evs:
        try:
            docs = list_docs(ev)
        except (requests.RequestException, ValueError) as exc:
            if engine.due(f"fia_err:{ev['id']}", 6 * 3600):
                db.logmsg("warning", f"FIA documents for {ev['name']} not reachable: {exc}")
            continue
        new = 0
        for title, url, published in docs:
            cur = db.ex("INSERT OR IGNORE INTO fia_docs(event_id,url,title,published,kind,first_seen) "
                        "VALUES(?,?,?,?,?,?)", ev["id"], url, title, published, _kind(title), db.iso(now))
            new += cur.rowcount
        if new:
            engine.bump()
        # newest version of each final classification (the page lists newest first)
        done = set()
        for d in db.q("SELECT * FROM fia_docs WHERE event_id=? AND kind IN ('race_pos','sprint_pos','pole',"
                      "'sprint_pole') ORDER BY first_seen DESC, rowid ASC", ev["id"]):
            if d["kind"] in done:
                continue
            done.add(d["kind"])
            if d["parsed"]:
                continue
            changed += apply_classification(engine, ev, d)
    return changed


def apply_classification(engine, ev, d):
    entrants = db.q("SELECT * FROM entrants WHERE event_id=?", ev["id"])
    try:
        text = pdf_text(_get(d["url"], timeout=40).content)
    except Exception as exc:  # noqa: BLE001  network or PDF errors: retry on the next round
        db.logmsg("warning", f"FIA {d['title']} for {ev['name']}: could not read PDF ({exc})")
        return 0
    rows = parse_classification(text, entrants)
    need = 1 if d["kind"] in ("pole", "sprint_pole") else (8 if d["kind"] == "sprint_pos" else 10)
    if len(rows) < need:
        sample = " | ".join(l.strip() for l in text.splitlines() if l.strip())[:600]
        db.logmsg("warning", f"FIA {d['title']} for {ev['name']}: only {len(rows)} rows recognised; "
                             f"not applied. Text starts: {sample}")
        db.ex("UPDATE fia_docs SET parsed=-1 WHERE event_id=? AND url=?", ev["id"], d["url"])
        return 0
    res = _results_from(d["kind"], rows, entrants)
    cur = {f: {r["slot"]: r["value"] for r in db.q(
        "SELECT slot, value FROM results WHERE event_id=? AND field=?", ev["id"], f)} for f in res}
    diffs = []
    for f, slots in res.items():
        for k, v in sorted(slots.items()):
            if k <= 10 and cur[f].get(k) not in (None, v):
                label = LABEL[f] + (str(k) if f in ("race_pos", "sprint_pos") else "")
                diffs.append(f"{label}: {cur[f].get(k)} → {v}")
        engine.write_results(ev["id"], {f: slots}, "fia")
    db.ex("UPDATE fia_docs SET parsed=1 WHERE event_id=? AND url=?", ev["id"], d["url"])
    admin = db.q("SELECT field, slot, value FROM results WHERE event_id=? AND source='admin'", ev["id"])
    conflict = [f"{a['field']} {a['slot']}: admin {a['value']}, FIA {res[a['field']][a['slot']]}"
                for a in admin if a["field"] in res and res[a["field"]].get(a["slot"]) not in (None, a["value"])]
    top = ", ".join(v for _k, v in sorted(next(iter(res.values())).items())[:5])
    db.logmsg("info", f"FIA {d['title']} applied for {ev['name']}: {top}"
                      + (f" (changes: {'; '.join(diffs)})" if diffs else ""))
    if diffs or conflict:
        body = f"The FIA published the {d['title']} for {ev['name']}:\n{d['url']}\n\n"
        if diffs:
            body += "It changed: " + "; ".join(diffs) + "\nScores have been recalculated.\n"
        if conflict:
            body += ("\nAdmin corrections differ from the FIA and still take priority: " + "; ".join(conflict)
                     + f"\nCheck them at {mailer.base_url()}/admin/event/{ev['id']}\n")
        mailer.notify_admins(f"{ev['name']}: FIA {d['title']}", body)
    return 1


def stewards(event_id, limit=12):
    return db.q("SELECT title, url, published FROM fia_docs WHERE event_id=? AND kind='stewards' "
                "ORDER BY first_seen DESC, rowid ASC LIMIT ?", event_id, limit)


def classification_docs(event_id):
    return db.q("SELECT title, url FROM fia_docs WHERE event_id=? AND kind IN ('race_pos','sprint_pos','pole',"
                "'sprint_pole') ORDER BY first_seen DESC", event_id)
