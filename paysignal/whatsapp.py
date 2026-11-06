"""WhatsApp: link chats that started from a Click-to-WhatsApp ad to the sales they become.

1. Meta's WhatsApp webhook tells us each new chat. If it started from an ad, the
   message carries a `referral` with the ad id, headline and a click id (ctwa_clid).
2. We keep one row per chat: the customer's phone (hashed), the ad, the click id.
   We never store message text or the customer's name.
3. When a payment arrives (cash, card, transfer, any source) with a phone number
   that matches a chat from an ad, the sale is credited to that ad and, if live,
   sent to Meta's Conversions API as a WhatsApp purchase with its real value.

Server settings:
  META_APP_SECRET                 checks the webhook signature (App settings > Basic)
  PROADS_WHATSAPP_VERIFY_TOKEN    any long random text; the same text goes into
                                  the webhook setup in Meta's App Dashboard
Per business (PAYSIGNAL_CLIENTS):
  "whatsapp": {"waba_id": "...", "dataset_id": "...", "access_token": "...", "live": false}
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from .identity import hash_phone

log = logging.getLogger("proads.whatsapp")

ATTRIBUTION_DAYS = 28        # a sale within this many days of an ad chat is credited to the ad
EARLY_GRACE = timedelta(days=1)   # sales lists often only have a date, not a time


def settings() -> dict:
    return {"app_secret": os.environ.get("META_APP_SECRET", "").strip(),
            "verify_token": os.environ.get("PROADS_WHATSAPP_VERIFY_TOKEN", "").strip()}


def verify_signature(body: bytes, header: str, secret: str) -> bool:
    """X-Hub-Signature-256: 'sha256=' + hex HMAC-SHA256 of the raw body with the app secret."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def parse_webhook(payload: dict) -> Iterator[dict]:
    """Yields one item per inbound customer message: who, when, and the ad referral if any.
    Status updates (sent/delivered/read) are skipped."""
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry") or []:
        waba_id = str(entry.get("id") or "")
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for m in value.get("messages") or []:
                wa_id = str(m.get("from") or "")
                if not wa_id:
                    continue
                try:
                    at = datetime.fromtimestamp(int(m.get("timestamp")), tz=timezone.utc)
                except (TypeError, ValueError):
                    at = datetime.now(timezone.utc)
                ref = m.get("referral") or {}
                yield {
                    "waba_id": waba_id, "phone_number_id": phone_number_id, "message_id": m.get("id"),
                    "phone_hash": hash_phone(wa_id), "at": at,
                    "referral": {
                        "source_type": (ref.get("source_type") or "")[:20],
                        "source_id": str(ref.get("source_id") or "")[:64],
                        "source_url": (ref.get("source_url") or "")[:500],
                        "headline": (ref.get("headline") or "")[:200],
                        "ctwa_clid": (ref.get("ctwa_clid") or "")[:500],
                    } if ref else None,
                }


def attribution_for(chat: Optional[dict], paid_at: datetime) -> Optional[dict]:
    """The ad (or post) a sale should be credited to, if the chat fits the time window."""
    if not chat or not chat.get("source_id") or not chat.get("referral_at"):
        return None
    ref_at = datetime.fromisoformat(chat["referral_at"])
    if paid_at + EARLY_GRACE < ref_at or paid_at - ref_at > timedelta(days=ATTRIBUTION_DAYS):
        return None
    is_ad = chat.get("source_type") == "ad"
    return {"utm_source": "whatsapp", "utm_medium": "paid" if is_ad else "organic",
            "utm_content": ("wa-ad:" if is_ad else "wa-post:") + chat["source_id"],
            "ctwa_clid": chat.get("ctwa_clid") or None}


def purchase_event(payment, value: float, waba_id: str, ctwa_clid: str) -> dict:
    """Conversions API event for a sale that came through a WhatsApp ad chat."""
    return {
        "event_name": "Purchase",
        "event_time": int(min(payment.occurred_at, datetime.now(timezone.utc)).timestamp()),
        "event_id": payment.event_id,
        "action_source": "business_messaging",
        "messaging_channel": "whatsapp",
        "user_data": {"whatsapp_business_account_id": waba_id, "ctwa_clid": ctwa_clid},
        "custom_data": {"value": round(float(value), 2), "currency": payment.currency,
                        "order_id": payment.external_id, "gross_value": payment.amount},
    }


def send(event: dict, dataset_id: str, token: str, http=None, version: Optional[str] = None) -> dict:
    version = version or os.environ.get("PROADS_META_VERSION", "v25.0")
    if http is None:
        import requests
        http = requests
    try:
        r = http.post(f"https://graph.facebook.com/{version}/{dataset_id}/events",
                      params={"access_token": token}, json={"data": [event]}, timeout=20)
        ok = r.status_code == 200
        if not ok:
            log.warning("Meta refused WhatsApp purchase %s: HTTP %s", event["event_id"], r.status_code)
        return {"status": "sent" if ok else f"error {r.status_code}"}
    except Exception as e:
        log.warning("WhatsApp purchase event failed: %s", e)
        return {"status": "error"}


def too_old_for_meta(payment) -> bool:
    """Meta takes events up to 7 days old; older sales are credited in reports only."""
    return datetime.now(timezone.utc) - payment.occurred_at > timedelta(days=7)
