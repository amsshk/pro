"""Shopify webhooks -> Payment.

Topics: `orders/paid` (purchase) and `refunds/create` (refund).
Click ids are read from the order's landing_site URL or note_attributes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from ..models import LineItem, Payment


def verify_hmac(payload: bytes, hmac_header: str, secret: str) -> bool:
    digest = base64.b64encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, hmac_header or "")


_TRACKED = ("fbclid", "ttclid", "utm_source", "utm_medium", "utm_campaign", "utm_content")


def _tracking(order: dict) -> dict:
    """Click ids and UTM tags from the landing URL, falling back to note attributes."""
    qs = parse_qs(urlparse(order.get("landing_site") or "").query)
    notes = {n.get("name"): n.get("value") for n in order.get("note_attributes") or []}
    return {k: (qs.get(k) or [None])[0] or notes.get(k) for k in _TRACKED}


def _person(order: dict, customer: dict) -> dict:
    """Browser and address details Shopify keeps on the order: they help Meta match the buyer (sent hashed)."""
    addr = order.get("billing_address") or order.get("shipping_address") or customer.get("default_address") or {}
    client = order.get("client_details") or {}
    notes = {n.get("name"): n.get("value") for n in order.get("note_attributes") or []}
    return {"first_name": addr.get("first_name") or customer.get("first_name"),
            "last_name": addr.get("last_name") or customer.get("last_name"),
            "city": addr.get("city"), "zip": addr.get("zip"), "country": addr.get("country_code"),
            "client_ip_address": order.get("browser_ip") or client.get("browser_ip"),
            "client_user_agent": client.get("user_agent"),
            "event_source_url": order.get("order_status_url") or None,
            "fbp": notes.get("_fbp") or notes.get("fbp")}


def _ts(value: str | None) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now().astimezone()


def parse_shopify_webhook(topic: str, body: dict, client_id: str,
                          costs: dict[str, float] | None = None) -> list[Payment]:
    costs = costs or {}
    if topic == "orders/paid":
        track = _tracking(body)
        codes = body.get("discount_codes") or []
        customer = body.get("customer") or {}
        items = [LineItem(sku=str(li.get("sku") or li.get("variant_id")), name=li.get("title", ""),
                          quantity=int(li.get("quantity", 1)), unit_price=float(li.get("price", 0)),
                          unit_cost=costs.get(str(li.get("sku"))))
                 for li in body.get("line_items", [])]
        return [Payment(
            client_id=client_id, source="shopify", external_id=str(body["id"]), kind="purchase",
            amount=float(body.get("total_price", 0)), currency=body.get("currency", "USD"),
            occurred_at=_ts(body.get("processed_at") or body.get("created_at")),
            email=body.get("email") or customer.get("email"),
            phone=body.get("phone") or customer.get("phone"),
            customer_id=str(customer["id"]) if customer.get("id") else None,
            discount=float(body.get("total_discounts", 0) or 0), items=items,
            promo_code=(codes[0].get("code") if codes else None), **track, **_person(body, customer),
        )]

    if topic == "refunds/create":
        amount = sum(float(t.get("amount", 0)) for t in body.get("transactions", [])
                     if t.get("kind") == "refund" and t.get("status", "success") == "success")
        currency = next((t.get("currency") for t in body.get("transactions", []) if t.get("currency")), "USD")
        return [Payment(
            client_id=client_id, source="shopify", external_id=f"refund:{body['id']}", kind="refund",
            amount=amount, currency=currency, occurred_at=_ts(body.get("created_at")),
            refund_of=str(body["order_id"]),
        )]
    return []
