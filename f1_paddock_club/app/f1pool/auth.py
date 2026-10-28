"""Passwords, tokens, CSRF and login throttling."""
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from . import db


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:  # noqa: BLE001
        return False


def make_token(user_id, kind, hours):
    tok = secrets.token_urlsafe(32)
    db.ex("UPDATE tokens SET used=1 WHERE user_id=? AND kind=?", user_id, kind)
    db.ex("INSERT INTO tokens(token,user_id,kind,expires_at) VALUES(?,?,?,?)",
          tok, user_id, kind, db.iso(db.now_utc() + timedelta(hours=hours)))
    return tok


def use_token(tok, kind, consume=False):
    r = db.q1("SELECT * FROM tokens WHERE token=? AND kind=? AND used=0", tok, kind)
    if not r or db.parse_iso(r["expires_at"]) < db.now_utc():
        return None
    if consume:
        db.ex("UPDATE tokens SET used=1 WHERE token=?", tok)
    return r["user_id"]


def csrf_token(session):
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def csrf_ok(session, value):
    return bool(value) and hmac.compare_digest(session.get("csrf", ""), value)


_attempts = defaultdict(deque)


def throttled(key, limit=8, window=900):
    dq = _attempts[key]
    now = time.time()
    while dq and now - dq[0] > window:
        dq.popleft()
    return len(dq) >= limit


def record_fail(key):
    _attempts[key].append(time.time())
