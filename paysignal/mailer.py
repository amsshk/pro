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


BRAND_URL = "https://pro.ezdevsoft.net"


def brand(html_body: str | None, text: str) -> tuple[str, str]:
    """Every ProAds email: logo on top, the message, then the ProAds signature and footer (light card, works in dark mode)."""
    import html as _h
    base = (os.environ.get("PROADS_PUBLIC_URL") or BRAND_URL).rstrip("/")
    body = html_body or "".join(f'<p style="margin:0 0 12px">{_h.escape(par).replace(chr(10), "<br>")}</p>' for par in text.split("\n\n"))
    page = (f'<div style="background:#F4F4F2;padding:24px 12px;font-family:Arial,Helvetica,sans-serif">'
            f'<div style="max-width:600px;margin:0 auto;background:#FFFFFF;border-radius:14px;overflow:hidden;border:1px solid #E6E6E1">'
            f'<a href="{base}" style="display:block;background:#FFFFFF;line-height:0;text-decoration:none" bgcolor="#FFFFFF">'
            f'<img src="{base}/proads-email-header.png?v=2" width="600" alt="ProAds - Turn ad spend into paid orders" style="display:block;width:100%;max-width:600px;height:auto;border:0;background:#FFFFFF"></a>'
            f'<div style="padding:24px;color:#141414;font-size:15px;line-height:1.55">{body}</div>'
            f'<div style="padding:18px 24px;border-top:1px solid #EEEEEA;color:#5C5C5C;font-size:13px;line-height:1.5">'
            f'<b style="color:#141414">The ProAds team</b><br>Turn ad spend into paid orders<br>'
            f'<a href="{base}" style="color:#4A7600">pro.ezdevsoft.net</a> · Reply to this email for help</div></div>'
            f'<p style="max-width:600px;margin:12px auto 0;color:#8A8A8A;font-size:11px;text-align:center">'
            f'You get this email because you have a ProAds account. <a href="{base}/app#/upload" style="color:#8A8A8A">Email settings</a></p></div>')
    plain = text.rstrip() + "\n\n--\nThe ProAds team\nTurn ad spend into paid orders\n" + base + "\n"
    return page, plain


def send_checked(to: str, subject: str, text: str, html: str | None = None) -> tuple[bool, str]:
    """(sent, why not) - the reason is shown to the admin, never the password."""
    if not available():
        missing = [k for k in ("SMTP_HOST", "SMTP_FROM") if not os.environ.get(k)]
        return False, "Missing in Render: " + ", ".join(missing)
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    html, text = brand(html, text)
    msg.set_content(text)
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
