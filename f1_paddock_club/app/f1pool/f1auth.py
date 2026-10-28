"""Sign-in to the Formula 1 account to obtain the F1TV subscription token
that livetiming.formula1.com accepts as Bearer token.

Adapted from FastF1 3.9.0 (fastf1.internals.f1account), MIT License,
Copyright (c) 2026 Philipp Schäfer. If F1 changes its login flow, compare
with the latest FastF1 release and port the changes here.
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import jwt
import requests

log = logging.getLogger("f1auth")

ACCOUNT_ORIGIN = "https://account.formula1.com"
IDP_ORIGIN = "https://id.formula1.com"
AUTHORIZE_URL = f"{IDP_ORIGIN}/as/authorize"
TOKEN_URL = f"{IDP_ORIGIN}/as/token"
CONFIG_SCRIPT_URL = f"{ACCOUNT_ORIGIN}/scripts/main.min.js"
TIMEOUT = (5, 30)
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
PING_SDK_HEADERS = {"X-Requested-With": "ping-sdk"}
EXPIRY_MARGIN = 300
_DAVINCI_OBJECT = re.compile(r"\bdavinci:\{([^}]*)\}")


class F1AuthError(Exception):
    pass


@dataclass(frozen=True)
class SubscriptionToken:
    value: str
    expires_at: float

    @classmethod
    def from_jwt(cls, value: str) -> "SubscriptionToken":
        try:
            claims = jwt.decode(value, options={"verify_signature": False})
            return cls(value=value, expires_at=float(claims["exp"]))
        except Exception as exc:  # noqa: BLE001
            raise F1AuthError("Subscription token has no valid expiry.") from exc

    @property
    def claims(self) -> dict:
        return jwt.decode(self.value, options={"verify_signature": False})

    def is_valid(self) -> bool:
        return self.expires_at - EXPIRY_MARGIN > time.time()


def _fetch_config(session):
    resp = session.get(CONFIG_SCRIPT_URL, timeout=TIMEOUT)
    resp.raise_for_status()
    obj = _DAVINCI_OBJECT.search(resp.text)
    if not obj:
        raise F1AuthError("No login config found on account.formula1.com")

    def field(name):
        m = re.search(rf'\b{name}:"([^"]+)"', obj.group(1))
        if not m:
            raise F1AuthError(f"Login config missing field {name}")
        return m.group(1)
    return field("clientId"), field("acrValues")


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def login(username: str, password: str) -> SubscriptionToken:
    try:
        with requests.Session() as s:
            s.headers.update({"User-Agent": USER_AGENT,
                              "Accept-Language": "en-US,en;q=0.9"})
            client_id, policy_id = _fetch_config(s)
            verifier = secrets.token_urlsafe(64)
            state = secrets.token_urlsafe(24)
            r = s.get(AUTHORIZE_URL, params={
                "client_id": client_id, "response_type": "code",
                "response_mode": "pi.flow", "scope": "openid", "state": state,
                "nonce": secrets.token_urlsafe(24),
                "code_challenge": _code_challenge(verifier),
                "code_challenge_method": "S256", "acr_values": policy_id,
            }, headers=PING_SDK_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            form = r.json()
            try:
                submit_url = form["_links"]["next"]["href"]
                interaction_id = form["interactionId"]
                flow_id = form["id"]
                event_name = form["eventName"]
            except (KeyError, TypeError) as exc:
                raise F1AuthError("Unexpected response when starting sign-in") from exc
            if not submit_url.startswith(f"{IDP_ORIGIN}/"):
                raise F1AuthError(f"Unexpected sign-in url {submit_url}")
            r = s.post(submit_url, json={
                "id": flow_id, "eventName": event_name,
                "interactionId": interaction_id,
                "parameters": {"eventType": "submit", "data": {
                    "actionKey": "SIGNIN",
                    "formData": {"username": username, "password": password}}},
            }, headers={**PING_SDK_HEADERS, "interactionId": interaction_id},
                timeout=TIMEOUT)
            try:
                data = r.json()
            except ValueError:
                r.raise_for_status()
                raise F1AuthError("Unexpected response from sign-in") from None
            if data.get("status") != "COMPLETED":
                raise F1AuthError(data.get("message") or f"Sign-in failed ({r.status_code})")
            auth = data.get("authorizeResponse") or {}
            if auth.get("state") != state or not auth.get("code"):
                raise F1AuthError("Sign-in failed: no authorization code")
            r = s.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "client_id": client_id,
                "code": auth["code"], "code_verifier": verifier,
            }, timeout=TIMEOUT)
            r.raise_for_status()
            access = r.json()["access_token"]
    except requests.RequestException as exc:
        raise F1AuthError(f"Sign-in failed: {exc}") from exc
    claims = jwt.decode(access, options={"verify_signature": False})
    value = claims.get("subscriptionToken")
    if not value:
        raise F1AuthError("Account has no F1 TV subscription token "
                          "(an active F1 TV Access/Pro subscription is required)")
    return SubscriptionToken.from_jwt(value)


class TokenManager:
    """Keeps a valid token, signing in again only when it expires."""

    def __init__(self, path: Path):
        self.path = path
        self._token = None
        self._lock = threading.Lock()
        self.last_error = None
        self._last_attempt = 0.0

    def credentials(self):
        return os.environ.get("F1_USERNAME", ""), os.environ.get("F1_PASSWORD", "")

    def get(self, force=False):
        """Return a valid token or None (anonymous access)."""
        user, pw = self.credentials()
        if not user or not pw:
            self.last_error = "No F1 account configured"
            return None
        with self._lock:
            if not force:
                tok = self._token or self._load()
                if tok and tok.is_valid():
                    self._token = tok
                    return tok
            # do not hammer the login when it fails
            if time.time() - self._last_attempt < 600 and not force:
                return None
            self._last_attempt = time.time()
            try:
                tok = login(user, pw)
                self._token = tok
                self._store(tok)
                self.last_error = None
                log.info("Signed in to F1 account; token valid until %s",
                         time.strftime("%Y-%m-%d %H:%M", time.gmtime(tok.expires_at)))
                return tok
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("F1 sign-in failed: %s", exc)
                return None

    def clear(self):
        with self._lock:
            self._token = None
            self._last_attempt = 0.0
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _load(self):
        try:
            d = json.loads(self.path.read_text())
            return SubscriptionToken(d["value"], float(d["expires_at"]))
        except Exception:  # noqa: BLE001
            return None

    def _store(self, tok):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"value": tok.value, "expires_at": tok.expires_at}))
        os.chmod(self.path, 0o600)
