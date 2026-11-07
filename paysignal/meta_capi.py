"""Send payments to Meta's Conversions API with their *real* value.

Meta learns from the value we send. Sending real value (margin x keep-chance
+ future value) instead of the checkout total teaches its delivery system to
look for customers who pay and keep paying.

Runs in dry-run mode by default: builds the exact payload, sends nothing.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .identity import _sha256, fbc_from_fbclid, hash_email, hash_field, hash_phone
from .models import Payment
from .scoring import Score

GRAPH_URL = "https://graph.facebook.com/{version}/{dataset_id}/events"
BATCH = 1000


def _action_source(p: Payment) -> str:
    if p.channel == "physical_store":
        return "physical_store"
    return "website"


MATCH_KEYS = ("em", "ph", "external_id", "fbc", "fbp", "client_ip_address", "fn", "ln", "ct", "zp", "country")


def user_data_for(p: Payment, default_country_code: str = "971") -> dict:
    """Everything Meta can match a buyer on. Personal details are SHA-256 hashed; click/browser ids are not
    (Meta's rules). Nothing raw ever leaves the server."""
    one = lambda v: [v] if v else None
    return {k: v for k, v in {
        "em": one(hash_email(p.email)),
        "ph": one(hash_phone(p.phone, default_country_code)),
        "external_id": one(_sha256(str(p.customer_id).strip().lower())) if p.customer_id else None,
        "fbc": fbc_from_fbclid(p.fbclid, p.occurred_at),
        "fbp": p.fbp,
        "client_user_agent": p.client_user_agent,
        "client_ip_address": p.client_ip_address,
        "fn": one(hash_field(p.first_name, "name")), "ln": one(hash_field(p.last_name, "name")),
        "ct": one(hash_field(p.city, "city")), "zp": one(hash_field(p.zip, "zip")),
        "country": one(hash_field(p.country, "country")),
    }.items() if v}


def build_purchase_event(p: Payment, score: Score, default_country_code: str = "971") -> dict:
    user_data = user_data_for(p, default_country_code)

    event = {
        "event_name": "Purchase",
        "event_time": int(p.occurred_at.timestamp()),
        "event_id": p.event_id,           # same id from browser pixel = no double count
        "action_source": _action_source(p),
        "user_data": user_data,
        "custom_data": {
            "value": score.real_value,     # the real value, not the checkout total
            "currency": p.currency,
            "order_id": p.external_id,
            "gross_value": p.amount,
            "predicted_return_risk": score.return_risk,
        },
    }
    if p.event_source_url:
        event["event_source_url"] = p.event_source_url
    return event


def build_refund_event(refund: Payment, original: Optional[Payment] = None) -> dict:
    """Custom event (Meta has no standard refund). Positive value; the name says it's a refund.
    Matched on the original buyer, so it can feed exclusion audiences; it does not reduce Ads Manager ROAS."""
    who = original or refund
    return {
        "event_name": "Refund",
        "event_time": int(refund.occurred_at.timestamp()),
        "event_id": refund.event_id,
        "action_source": "system_generated",
        "user_data": user_data_for(who),
        "custom_data": {"value": refund.amount, "currency": refund.currency, "order_id": refund.refund_of},
    }


def has_match_key(event: dict) -> bool:
    """Meta can't match an event with no customer identifier; skip those."""
    return any(k in event["user_data"] for k in ("em", "ph", "external_id", "fbc", "fbp"))


def send(events: Iterable[dict], dataset_id: str, access_token: str, version: str = "v25.0",
         test_event_code: Optional[str] = None, dry_run: bool = True) -> list[dict]:
    """Batches events. With dry_run=True returns the payloads instead of posting them.
    Use test_event_code (from Events Manager > Test events) for a safe live test."""
    events = [e for e in events if has_match_key(e)]
    results = []
    for i in range(0, len(events), BATCH):
        payload = {"data": events[i:i + BATCH]}
        if test_event_code:
            payload["test_event_code"] = test_event_code
        if dry_run:
            results.append({"dry_run": True, "count": len(payload["data"]), "payload": payload})
            continue
        import requests
        r = requests.post(GRAPH_URL.format(version=version, dataset_id=dataset_id),
                          params={"access_token": access_token}, json=payload, timeout=30)
        results.append({"status": r.status_code, "body": r.json() if r.content else {}, "count": len(payload["data"])})
    return results
