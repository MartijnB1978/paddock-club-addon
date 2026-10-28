"""Outgoing e-mail (Gmail SMTP with an app password)."""
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

from . import db

log = logging.getLogger("mail")


def base_url():
    return (db.setting("base_url") or os.environ.get("BASE_URL", "")).rstrip("/")


def configured():
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def send(to, subject, body, background=True):
    if not configured() or not to:
        log.info("mail not sent (SMTP not configured): %s", subject)
        return False

    def _send():
        msg = EmailMessage()
        sender = os.environ["SMTP_USER"]
        msg["From"] = f"Paddock Club Reeuwijk <{sender}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
            port = int(os.environ.get("SMTP_PORT", "465"))
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(sender, os.environ["SMTP_PASSWORD"])
                s.send_message(msg)
            db.logmsg("info", f"Mail sent to {to}: {subject}")
        except Exception as exc:  # noqa: BLE001
            db.logmsg("error", f"Mail to {to} failed: {exc}")
    if background:
        threading.Thread(target=_send, daemon=True).start()
    else:
        _send()
    return True


def notify_admins(subject, body):
    for r in db.q("SELECT email FROM users WHERE is_admin=1 AND active=1 AND email IS NOT NULL"):
        send(r["email"], subject, body)
