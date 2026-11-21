"""Sends emails (password reset) through any SMTP provider.

Settings: SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = SSL), SMTP_USER, SMTP_PASSWORD,
SMTP_FROM (e.g. "ProAds <hello@ezdevsoft.net>"). Without SMTP_HOST, email is off and
admins create reset links from the admin page instead.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("proads.mail")


def available() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def send(to: str, subject: str, text: str, html: str | None = None) -> bool:
    return send_checked(to, subject, text, html)[0]


def send_checked(to: str, subject: str, text: str, html: str | None = None) -> tuple[bool, str]:
    """(sent, why not) - the reason is shown to the admin, never the password."""
    if not available():
        missing = [k for k in ("SMTP_HOST", "SMTP_FROM") if not os.environ.get(k)]
        return False, "Missing in Render: " + ", ".join(missing)
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls(context=ssl.create_default_context())
        with server:
            if user:
                server.login(user, password)
            server.send_message(msg)
        return True, ""
    except Exception as e:
        log.warning("email to %s failed: %s", to.split("@")[-1], e)
        why = f"{type(e).__name__}: {str(e)[:200]}"
        if os.environ.get("SMTP_PASSWORD"):
            why = why.replace(os.environ["SMTP_PASSWORD"], "***")
        return False, why
