"""Stripe webhook events -> Payment.

Handles `charge.succeeded` (purchase) and `charge.refunded` (refund).
Stripe sends amounts in the smallest currency unit (cents/fils).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone

from ..models import Payment

# Currencies Stripe treats as having no minor unit.
_ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG",
                 "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}


def _major(amount: int, currency: str) -> float:
    return float(amount) if currency.upper() in _ZERO_DECIMAL else amount / 100.0


def verify_signature(payload: bytes, sig_header: str, secret: str, tolerance: int = 300) -> bool:
    """Check the Stripe-Signature header so nobody can fake payments."""
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        ts, sig = parts["t"], parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - int(ts)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def parse_stripe_event(event: dict, client_id: str) -> list[Payment]:
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})
    if etype not in ("charge.succeeded", "charge.refunded"):
        return []

    currency = obj.get("currency", "usd").upper()
    billing = obj.get("billing_details") or {}
    meta = obj.get("metadata") or {}
    common = dict(
        client_id=client_id,
        source="stripe",
        currency=currency,
        occurred_at=datetime.fromtimestamp(obj.get("created", time.time()), tz=timezone.utc),
        email=billing.get("email") or obj.get("receipt_email"),
        phone=billing.get("phone"),
        customer_id=obj.get("customer"),
        fbclid=meta.get("fbclid"),
        ttclid=meta.get("ttclid"),
        utm_source=meta.get("utm_source"), utm_medium=meta.get("utm_medium"),
        utm_campaign=meta.get("utm_campaign"), utm_content=meta.get("utm_content"),
        promo_code=meta.get("promo_code"),
        fbp=meta.get("fbp"),
        first_name=(billing.get("name") or "").split(" ")[0] or None,
        last_name=" ".join((billing.get("name") or "").split(" ")[1:]) or None,
        city=(billing.get("address") or {}).get("city"),
        zip=(billing.get("address") or {}).get("postal_code"),
        country=(billing.get("address") or {}).get("country"),
    )

    if etype == "charge.succeeded":
        return [Payment(external_id=obj["id"], kind="purchase",
                        amount=_major(obj["amount"], currency), **common)]

    refunded = obj.get("amount_refunded", 0)
    return [Payment(external_id=f"{obj['id']}:refund:{refunded}", kind="refund",
                    amount=_major(refunded, currency), refund_of=obj["id"], **common)]
