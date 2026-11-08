"""Web Push notifications for the chat (works like Teams: a pop-up on phone/desktop, even with the app closed).

Free: uses the browsers' own push services (Google, Apple, Mozilla) with VAPID keys generated on first start.
Payloads are end-to-end encrypted to the player's device. iPhone/iPad: only after "Add to Home Screen"."""
import base64
import json
import os
import threading
import time
from pathlib import Path

from . import db

DATA_DIR = Path(os.environ.get("F1POOL_DATA", "/data"))
KEY_FILE = DATA_DIR / "vapid_private.pem"
ACTIVE_FOR = 12          # seconds: someone whose open chat page polled this recently is reading along -> no pop-up
TTL = 24 * 3600          # push service keeps an undelivered message one day (phone off, flight mode)

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs(
  id INTEGER PRIMARY KEY, user_id INTEGER, endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT, ua TEXT,
  created_at TEXT, last_ok TEXT);
"""
_active = {}             # user_id -> monotonic time of last visible chat poll
_public_key = ""


def init():
    global _public_key
    db.conn().executescript(SCHEMA)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        os.chmod(KEY_FILE, 0o600)
    key = serialization.load_pem_private_key(KEY_FILE.read_bytes(), None)
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    _public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    n = db.q1("SELECT COUNT(*) n FROM push_subs")["n"]
    db.logmsg("info", f"Chat notifications: {n} device(s) subscribed")


def public_key():
    return _public_key


def _subject():
    admin = os.environ.get("ADMIN_EMAIL", "")
    if admin:
        return f"mailto:{admin}"
    return os.environ.get("BASE_URL") or "mailto:paddock-club@example.invalid"


# ---------------------------------------------------------- subscriptions ---
def subscribe(user_id, sub, ua=""):
    ep = (sub or {}).get("endpoint", "")
    keys = (sub or {}).get("keys") or {}
    if not ep.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Invalid subscription")
    known = db.q1("SELECT user_id FROM push_subs WHERE endpoint=?", ep)
    db.ex("INSERT INTO push_subs(user_id,endpoint,p256dh,auth,ua,created_at) VALUES(?,?,?,?,?,?) "
          "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth, "
          "ua=excluded.ua", user_id, ep, keys["p256dh"], keys["auth"], (ua or "")[:200], db.iso(db.now_utc()))
    if not known or known["user_id"] != user_id:
        db.logmsg("info", f"Chat notifications: device added for user {user_id}")


def unsubscribe(user_id, endpoint):
    db.ex("DELETE FROM push_subs WHERE user_id=? AND endpoint=?", user_id, endpoint or "")


def devices(user_id):
    return db.q1("SELECT COUNT(*) n FROM push_subs WHERE user_id=?", user_id)["n"]


def mark_active(user_id):
    _active[user_id] = time.monotonic()


def _is_active(user_id):
    return time.monotonic() - _active.get(user_id, -1e9) < ACTIVE_FOR


# ---------------------------------------------------------------- sending ---
def _send(sub, payload, urgency="high"):
    """True if delivered to the push service; deletes subscriptions the service says are gone."""
    from pywebpush import WebPushException, webpush
    try:
        webpush({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                json.dumps(payload), vapid_private_key=str(KEY_FILE), vapid_claims={"sub": _subject()},
                ttl=TTL, headers={"Urgency": urgency}, timeout=10)
        db.ex("UPDATE push_subs SET last_ok=? WHERE id=?", db.iso(db.now_utc()), sub["id"])
        return True
    except WebPushException as exc:
        code = getattr(exc.response, "status_code", None)
        if code in (404, 410):                     # unsubscribed / app removed / browser data cleared
            db.ex("DELETE FROM push_subs WHERE id=?", sub["id"])
            db.logmsg("info", f"Chat notifications: removed expired device of user {sub['user_id']}")
        else:
            db.logmsg("warning", f"Chat notifications: push to user {sub['user_id']} failed ({code}): {exc}"[:300])
    except Exception as exc:                       # network trouble: never break the chat
        db.logmsg("warning", f"Chat notifications: push to user {sub['user_id']} failed: {exc}"[:300])
    return False


def _preview(text, kind):
    text = " ".join((text or "").split())
    if len(text) > 140:
        text = text[:139] + "…"
    if kind == "image":
        return f"📷 {text}" if text else "📷 Photo"
    if kind == "video":
        return f"🎬 {text}" if text else "🎬 Video"
    return text


def _notify_chat(msg_id):
    from . import chat
    m = db.q1("SELECT m.*, u.name, x.kind FROM chat_messages m JOIN users u ON u.id=m.user_id "
              "LEFT JOIN chat_media x ON x.id=m.media_id WHERE m.id=? AND m.deleted=0", msg_id)
    if not m:
        return
    body = _preview(m["text"], m["kind"])
    subs = db.q("SELECT p.* FROM push_subs p JOIN users u ON u.id=p.user_id "
                "WHERE u.active=1 AND p.user_id<>?", m["user_id"])
    sent = 0
    for s in subs:
        if _is_active(s["user_id"]):
            continue
        unread = chat.unread(s["user_id"])
        payload = {"title": m["name"] if unread <= 1 else f"{m['name']} · {unread} new messages",
                   "body": body, "url": "/chat", "tag": "pcr-chat", "unread": unread, "id": msg_id}
        sent += _send(s, payload)
    if subs:
        db.logmsg("info", f"Chat notifications: message #{msg_id} pushed to {sent} device(s)")


def notify_chat(msg_id):
    """Fire and forget: the sender never waits for the push services."""
    threading.Thread(target=_notify_chat, args=(msg_id,), daemon=True, name="push").start()


def test(user_id):
    subs = db.q("SELECT * FROM push_subs WHERE user_id=?", user_id)
    payload = {"title": "Paddock Club Reeuwijk", "body": "Notifications work on this device. 🏁",
               "url": "/chat", "tag": "pcr-test", "unread": 0}
    return sum(_send(s, payload) for s in subs), len(subs)
