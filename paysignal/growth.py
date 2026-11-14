"""Meta tracking for ProAds' OWN ads, so Meta learns who becomes a real user.

  CompleteRegistration  sent when someone creates an account
  StartTrial            sent once, when that account first uploads or connects data
                        (an active user, not just a sign-up). Optimise campaigns for this.

Both go through the Conversions API from the server, with the same event_id the
browser Pixel uses, so Meta counts each once. Sent only when the visitor accepted
ads measurement in the cookie notice. Identifiers are SHA-256 hashed.

Settings: PROADS_META_DATASET_ID (Pixel / dataset ID), PROADS_META_TOKEN
(Conversions API access token), optional PROADS_META_TEST_CODE and
PROADS_META_VERSION. Without them nothing is sent.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from .identity import hash_email, hash_phone, _sha256

log = logging.getLogger("proads.growth")


def settings() -> dict:
    return {"dataset_id": os.environ.get("PROADS_META_DATASET_ID", "").strip(),
            "token": os.environ.get("PROADS_META_TOKEN", "").strip(),
            "test_code": os.environ.get("PROADS_META_TEST_CODE", "").strip(),
            "version": os.environ.get("PROADS_META_VERSION", "v25.0").strip()}


def pixel_id() -> str:
    return settings()["dataset_id"]


def capture_context(body: dict, headers, remote_addr: str) -> Optional[dict]:
    """What the browser and request tell us, kept only if the visitor consented."""
    if not body.get("ads_consent"):
        return None
    fbc = (body.get("fbc") or "").strip()[:500]
    fbclid = (body.get("fbclid") or "").strip()[:400]
    if not fbc and fbclid:
        fbc = f"fb.1.{int(time.time() * 1000)}.{fbclid}"
    ip = (headers.get("X-Forwarded-For") or remote_addr or "").split(",")[0].strip()
    return {"fbp": (body.get("fbp") or "").strip()[:200] or None, "fbc": fbc or None,
            "ttclid": (body.get("ttclid") or "").strip()[:500] or None, "ttp": (body.get("ttp") or "").strip()[:200] or None,
            "ua": (headers.get("User-Agent") or "")[:400] or None, "ip": ip or None,
            "url": (body.get("source_url") or "").split("#")[0][:500] or None}


def build_event(name: str, account: dict, ctx: dict, event_id: str, value: Optional[float] = None,
                currency: str = "USD", custom: Optional[dict] = None, event_time: Optional[int] = None) -> dict:
    user = {k: v for k, v in {
        "em": [hash_email(account.get("email"))] if hash_email(account.get("email")) else None,
        "ph": [hash_phone(account.get("phone"))] if hash_phone(account.get("phone")) else None,
        "external_id": [_sha256(account["client_id"])],
        "fbp": ctx.get("fbp"), "fbc": ctx.get("fbc"),
        "client_user_agent": ctx.get("ua"), "client_ip_address": ctx.get("ip"),
    }.items() if v}
    event = {"event_name": name, "event_time": int(event_time or time.time()), "event_id": event_id,
             "action_source": "website", "user_data": user}
    if ctx.get("url"):
        event["event_source_url"] = ctx["url"]
    if value is not None:
        event["custom_data"] = {"value": round(float(value), 2), "currency": (currency or "USD").upper()}
    if custom:
        event.setdefault("custom_data", {}).update(custom)
    from . import tiktok                                    # the TikTok twin rides along; emit_growth takes it off
    tt = tiktok.from_growth(event, ctx, account)
    if tt:
        event["_tiktok"] = tt
    return event


def send(event: dict, http=None) -> dict:
    s = settings()
    if not (s["dataset_id"] and s["token"]):
        return {"skipped": "PROADS_META_DATASET_ID / PROADS_META_TOKEN not set"}
    payload = {"data": [event]}
    if s["test_code"]:
        payload["test_event_code"] = s["test_code"]
    if http is None:
        import requests
        http = requests
    try:
        r = http.post(f"https://graph.facebook.com/{s['version']}/{s['dataset_id']}/events",
                      params={"access_token": s["token"]}, json=payload, timeout=20)
        body = r.json() if getattr(r, "content", b"") or hasattr(r, "payload") else {}
        if r.status_code != 200:
            log.warning("Meta rejected %s: HTTP %s %s", event["event_name"], r.status_code, str(body)[:300])
        return {"status": r.status_code, "body": body}
    except Exception as e:                                  # never break sign-up over tracking
        log.warning("Meta event %s failed: %s", event["event_name"], e)
        return {"error": str(e)}


def send_later(event: dict, http=None, sent: Optional[list] = None, on_done=None) -> None:
    """Fire and forget, so a slow Meta never slows the visitor down. on_done(result) records the outcome."""
    def run():
        result = send(event, http)
        if on_done is not None:
            try:
                on_done(result)
            except Exception as e:
                log.warning("recording Meta delivery failed: %s", e)
        if sent is not None:
            sent.append((event, result))
    threading.Thread(target=run, daemon=True, name="meta-growth-event").start()


def load_ctx(raw: Optional[str]) -> Optional[dict]:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None
