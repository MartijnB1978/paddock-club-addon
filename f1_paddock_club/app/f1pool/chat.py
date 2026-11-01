"""Paddock Club chat: text, links, photos and short videos for logged-in players.

Media live in /data/chat (on the Pi) and move to the Synology archive after 3 months, when the
Home Assistant network storage 'pcr_archive' is mounted (it appears as /share/pcr_archive)."""
import html
import io
import os
import re
import secrets
import shutil
from datetime import timedelta
from pathlib import Path

from . import db

DATA_DIR = Path(os.environ.get("F1POOL_DATA", "/data"))
MEDIA_DIR = DATA_DIR / "chat"
ARCHIVE_DIR = Path(os.environ.get("CHAT_ARCHIVE_DIR", "/share/pcr_archive")) / "paddock-club-chat"
ARCHIVE_AFTER = timedelta(days=90)
MAX_TEXT = 2000
MAX_IMAGE = 10 * 1024 * 1024
MAX_VIDEO = 50 * 1024 * 1024
IMAGE_EDGE = 1600

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_media(
  id INTEGER PRIMARY KEY, kind TEXT, mime TEXT, path TEXT, size INTEGER, width INTEGER, height INTEGER,
  created_at TEXT, archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY, user_id INTEGER, text TEXT, media_id INTEGER, created_at TEXT, deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chat_seen(user_id INTEGER PRIMARY KEY, last_id INTEGER);
"""


def init():
    db.conn().executescript(SCHEMA)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


class ChatError(ValueError):
    pass


# ----------------------------------------------------------------- media ---
def _sniff_video(head):
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    return None


def save_upload(fileobj, filename=""):
    """Validate by content (not by what the browser claims), shrink photos, store. Returns media id."""
    raw = fileobj.read(MAX_VIDEO + 1)
    if not raw:
        raise ChatError("The file is empty.")
    vmime = _sniff_video(raw[:16])
    now = db.iso(db.now_utc())
    month = now[:7]
    folder = MEDIA_DIR / month
    folder.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(12)
    if vmime:
        if len(raw) > MAX_VIDEO:
            raise ChatError("Videos can be at most 50 MB.")
        ext = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}[vmime]
        path = folder / f"{name}.{ext}"
        path.write_bytes(raw)
        cur = db.ex("INSERT INTO chat_media(kind,mime,path,size,width,height,created_at) VALUES(?,?,?,?,?,?,?)",
                    "video", vmime, str(path), len(raw), None, None, now)
        return cur.lastrowid
    if len(raw) > MAX_IMAGE:
        raise ChatError("Photos can be at most 10 MB (videos 50 MB).")
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        im = Image.open(io.BytesIO(raw))
        fmt = (im.format or "").upper()
        if fmt not in ("JPEG", "PNG", "GIF", "WEBP", "MPO", "HEIF", "HEIC"):
            raise ChatError("Only photos (JPEG, PNG, GIF, WebP) and videos (MP4, MOV, WebM) can be shared.")
        animated = fmt in ("GIF", "WEBP") and getattr(im, "is_animated", False)
        if animated:                                   # keep animation as-is
            data, mime, ext = raw, f"image/{fmt.lower()}", fmt.lower()
            w, h = im.size
        else:
            im = ImageOps.exif_transpose(im)           # right way up; EXIF (incl. GPS) is dropped below
            im.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
            out = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P") and fmt == "PNG":
                im.save(out, "PNG", optimize=True)
                mime, ext = "image/png", "png"
            else:
                im.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
                mime, ext = "image/jpeg", "jpg"
            data = out.getvalue()
            w, h = im.size
    except (UnidentifiedImageError, OSError):
        raise ChatError("Only photos (JPEG, PNG, GIF, WebP) and videos (MP4, MOV, WebM) can be shared.") from None
    path = folder / f"{name}.{ext}"
    path.write_bytes(data)
    cur = db.ex("INSERT INTO chat_media(kind,mime,path,size,width,height,created_at) VALUES(?,?,?,?,?,?,?)",
                "image", mime, str(path), len(data), w, h, now)
    return cur.lastrowid


def media(media_id):
    m = db.q1("SELECT * FROM chat_media WHERE id=?", media_id)
    if not m or not Path(m["path"]).is_file():
        return None
    return m


def archive_old():
    """Move media older than 3 months to the Synology share. Returns (moved, note)."""
    if not ARCHIVE_DIR.parent.is_dir():
        return 0, f"archive folder {ARCHIVE_DIR.parent} not mounted"
    cutoff = db.iso(db.now_utc() - ARCHIVE_AFTER)
    moved = 0
    for m in db.q("SELECT * FROM chat_media WHERE archived=0 AND created_at<?", cutoff):
        src = Path(m["path"])
        if not src.is_file():
            continue
        dest_dir = ARCHIVE_DIR / m["created_at"][:7]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        if dest.stat().st_size == src.stat().st_size:
            db.ex("UPDATE chat_media SET path=?, archived=1 WHERE id=?", str(dest), m["id"])
            src.unlink()
            moved += 1
    return moved, ""


# -------------------------------------------------------------- messages ---
URL_RE = re.compile(r"(https?://[^\s<>\"']+)", re.I)


def render_text(text):
    """Escape everything, then turn web addresses into links (opened in a new tab, no referrer)."""
    out = []
    for part in URL_RE.split(text or ""):
        if URL_RE.fullmatch(part):
            url = part.rstrip(".,;:!?)")
            tail = part[len(url):]
            out.append(f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">'
                       f'{html.escape(url)}</a>{html.escape(tail)}')
        else:
            out.append(html.escape(part))
    return "".join(out).replace("\n", "<br>")


def post(user_id, text, media_id=None):
    text = (text or "").strip()[:MAX_TEXT]
    if not text and not media_id:
        raise ChatError("Type a message or add a photo or video.")
    last = db.q1("SELECT created_at FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 1", user_id)
    if last and db.parse_iso(last["created_at"]) > db.now_utc() - timedelta(seconds=1):
        raise ChatError("Slow down a little.")
    cur = db.ex("INSERT INTO chat_messages(user_id,text,media_id,created_at) VALUES(?,?,?,?)",
                user_id, text, media_id, db.iso(db.now_utc()))
    return cur.lastrowid


def messages(after=0, before=None, limit=60):
    sql = ("SELECT m.*, u.name, x.kind, x.width, x.height FROM chat_messages m JOIN users u ON u.id=m.user_id "
           "LEFT JOIN chat_media x ON x.id=m.media_id WHERE m.deleted=0 AND m.id>?")
    args = [after]
    if before:
        sql += " AND m.id<?"
        args.append(before)
    rows = db.q(sql + " ORDER BY m.id DESC LIMIT ?", *args, limit)
    return list(reversed(rows))


def delete(msg_id, user):
    m = db.q1("SELECT * FROM chat_messages WHERE id=?", msg_id)
    if not m or (m["user_id"] != user["id"] and not user["is_admin"]):
        return False
    db.ex("UPDATE chat_messages SET deleted=1, text='' WHERE id=?", msg_id)
    if m["media_id"]:
        x = db.q1("SELECT path FROM chat_media WHERE id=?", m["media_id"])
        if x and Path(x["path"]).is_file():
            Path(x["path"]).unlink()
        db.ex("DELETE FROM chat_media WHERE id=?", m["media_id"])
    return True


def unread(user_id):
    seen = db.q1("SELECT last_id FROM chat_seen WHERE user_id=?", user_id)
    return db.q1("SELECT COUNT(*) n FROM chat_messages WHERE deleted=0 AND user_id<>? AND id>?",
                 user_id, seen["last_id"] if seen else 0)["n"]


def mark_seen(user_id, last_id):
    db.ex("INSERT INTO chat_seen(user_id,last_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET "
          "last_id=MAX(last_id, excluded.last_id)", user_id, last_id)
