"""ProAds subscriptions with Stripe Billing: hosted Checkout, Customer Portal, webhooks.

Money goes to whichever Stripe account the secret key belongs to (set in Render),
and from there to that account's payout bank.

Settings:
  PROADS_STRIPE_SECRET_KEY      sk_live_... (or sk_test_... while testing)
  PROADS_STRIPE_WEBHOOK_SECRET  whsec_... from the webhook endpoint
                                https://<domain>/webhooks/proads-billing
  PROADS_STRIPE_PRICE_ID        price_... of the monthly plan (made in the Stripe dashboard)
  PROADS_TRIAL_DAYS             optional free trial on the first subscription
  PROADS_REQUIRE_PLAN           "1" = publishing campaigns needs an active plan
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

log = logging.getLogger("proads.billing")

ACTIVE = {"active", "trialing", "past_due"}        # past_due: Stripe is still retrying the card
API = "https://api.stripe.com/v1"


class BillingError(RuntimeError):
    pass


def settings() -> dict:
    env = lambda k: os.environ.get(k, "").strip()
    try:
        trial = max(0, min(int(env("PROADS_TRIAL_DAYS") or 0), 90))
    except ValueError:
        trial = 0
    return {"key": env("PROADS_STRIPE_SECRET_KEY"), "webhook_secret": env("PROADS_STRIPE_WEBHOOK_SECRET"),
            "price": env("PROADS_STRIPE_PRICE_ID"), "trial_days": trial,
            "require_plan": env("PROADS_REQUIRE_PLAN") == "1"}


def available() -> bool:
    s = settings()
    return bool(s["key"] and s["webhook_secret"] and s["price"])


class Stripe:
    def __init__(self, http=None, key: Optional[str] = None):
        if http is None:
            import requests
            http = requests
        self.http, self.key = http, key or settings()["key"]

    def _check(self, r) -> dict:
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code >= 400:
            raise BillingError((body.get("error") or {}).get("message") or f"Stripe HTTP {r.status_code}")
        return body

    def post(self, path: str, data: dict) -> dict:
        return self._check(self.http.post(f"{API}/{path}", data=data, auth=(self.key, ""), timeout=30))

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._check(self.http.get(f"{API}/{path}", params=params or {}, auth=(self.key, ""), timeout=30))


_price_cache: dict = {}


def price_info(stripe: Stripe) -> dict:
    """Name and price of the plan for the app to show (cached for an hour)."""
    pid = settings()["price"]
    hit = _price_cache.get(pid)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    p = stripe.get(f"prices/{pid}", {"expand[]": "product"})
    amount = p.get("unit_amount")
    info = {"name": (p.get("product") or {}).get("name") if isinstance(p.get("product"), dict) else "ProAds",
            "amount": amount / 100 if amount is not None else None, "currency": (p.get("currency") or "").upper(),
            "interval": (p.get("recurring") or {}).get("interval", "month")}
    _price_cache[pid] = (time.time(), info)
    return info


def checkout_session(stripe: Stripe, client_id: str, email: str, customer_id: Optional[str],
                     return_base: str, first_time: bool) -> str:
    s = settings()
    data = {"mode": "subscription", "line_items[0][price]": s["price"], "line_items[0][quantity]": "1",
            "success_url": return_base + "/app#/billing?paid=1", "cancel_url": return_base + "/app#/billing",
            "client_reference_id": client_id, "metadata[client_id]": client_id,
            "subscription_data[metadata][client_id]": client_id,
            "allow_promotion_codes": "true", "billing_address_collection": "auto",
            "tax_id_collection[enabled]": "true"}
    if customer_id:
        data.update({"customer": customer_id, "customer_update[name]": "auto", "customer_update[address]": "auto"})
    elif email:
        data["customer_email"] = email
    if first_time and s["trial_days"]:
        data["subscription_data[trial_period_days]"] = str(s["trial_days"])
    return stripe.post("checkout/sessions", data)["url"]


def portal_session(stripe: Stripe, customer_id: str, return_url: str) -> str:
    return stripe.post("billing_portal/sessions", {"customer": customer_id, "return_url": return_url})["url"]


def _period_end(sub: dict) -> Optional[int]:
    if sub.get("current_period_end"):
        return int(sub["current_period_end"])
    items = (sub.get("items") or {}).get("data") or []
    return int(items[0]["current_period_end"]) if items and items[0].get("current_period_end") else None


class BillingStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS billing (
        client_id TEXT PRIMARY KEY, customer_id TEXT, subscription_id TEXT, status TEXT,
        period_end INTEGER, cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')))"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def get(self, client_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT customer_id, subscription_id, status, period_end, cancel_at_period_end"
                              " FROM billing WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            return None
        return dict(zip(("customer_id", "subscription_id", "status", "period_end", "cancel_at_period_end"), row))

    def upsert(self, client_id: str, **fields) -> None:
        if not self.get(client_id):
            self.db.execute("INSERT INTO billing (client_id) VALUES (?)", (client_id,))
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE billing SET {sets}, updated_at=datetime('now') WHERE client_id=?",
                        (*fields.values(), client_id))
        self.db.commit()

    def by_customer(self, customer_id: str) -> Optional[str]:
        row = self.db.execute("SELECT client_id FROM billing WHERE customer_id=?", (customer_id,)).fetchone()
        return row[0] if row else None

    def active(self, client_id: str) -> bool:
        b = self.get(client_id)
        return bool(b and b["status"] in ACTIVE)

    def handle(self, event: dict, known) -> Optional[str]:
        """Apply one webhook event. Returns the client it changed, if any."""
        etype, obj = event.get("type", ""), (event.get("data") or {}).get("object") or {}
        if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
            cid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("client_id")
            if cid and known(cid):
                fields = {"customer_id": obj.get("customer"), "subscription_id": obj.get("subscription")}
                if not (self.get(cid) or {}).get("status"):
                    fields["status"] = "active" if obj.get("payment_status") in ("paid", "no_payment_required") else "incomplete"
                self.upsert(cid, **fields)
                return cid
        if etype in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            cid = (obj.get("metadata") or {}).get("client_id") or self.by_customer(obj.get("customer") or "")
            if cid and known(cid):
                self.upsert(cid, customer_id=obj.get("customer"), subscription_id=obj.get("id"),
                            status="canceled" if etype.endswith("deleted") else obj.get("status"),
                            period_end=_period_end(obj), cancel_at_period_end=1 if obj.get("cancel_at_period_end") else 0)
                return cid
        if etype == "invoice.payment_failed":
            log.warning("payment failed for customer %s", obj.get("customer"))
        return None
