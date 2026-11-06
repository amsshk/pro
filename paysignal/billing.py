"""ProAds subscriptions with Stripe Billing: hosted Checkout, Customer Portal, webhooks.

Money goes to whichever Stripe account the secret key belongs to (set in Render),
and from there to that account's payout bank.

Settings:
  PROADS_STRIPE_SECRET_KEY      sk_live_... (or sk_test_... while testing)
  PROADS_STRIPE_WEBHOOK_SECRET  whsec_... from the webhook endpoint
                                https://<domain>/webhooks/proads-billing
  PROADS_STRIPE_PRICE_ID        price_... of the monthly plan (made in the Stripe dashboard)
  PROADS_TRIAL_DAYS             optional free trial on the first subscription
  PROADS_SPEND_FEE_PERCENT      fee on ad spend published through ProAds (default 5, 0 = off).
                                Added to each renewal invoice for the period just ended
                                (needs the invoice.created webhook event).
  PROADS_ADS_TRIAL_DAYS         free ad test before paying (default 3). Signing up and organic
                                tools are always free; the test starts at a client's first Go live.
                                When it ends without a plan, their live campaigns are paused.
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
            "price": env("PROADS_STRIPE_PRICE_ID"), "premium_price": env("PROADS_STRIPE_PREMIUM_PRICE_ID"),
            "trial_days": trial,
            "ads_trial_days": ads_trial_days(), "fee_percent": fee_percent()}


def ads_trial_days() -> float:
    try:
        return max(0.0, min(float(os.environ.get("PROADS_ADS_TRIAL_DAYS", "3") or 0), 60.0))
    except ValueError:
        return 3.0


def fee_percent() -> float:
    try:
        return max(0.0, min(float(os.environ.get("PROADS_SPEND_FEE_PERCENT", "5") or 0), 50.0))
    except ValueError:
        return 5.0


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

    def post(self, path: str, data: dict, idempotency_key: Optional[str] = None) -> dict:
        kw = {"headers": {"Idempotency-Key": idempotency_key}} if idempotency_key else {}
        return self._check(self.http.post(f"{API}/{path}", data=data, auth=(self.key, ""), timeout=30, **kw))

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._check(self.http.get(f"{API}/{path}", params=params or {}, auth=(self.key, ""), timeout=30))


_price_cache: dict = {}


def price_id(stripe: Stripe) -> str:
    """The plan's price ID. A product ID (prod_...) also works: its default price is used."""
    pid = settings()["price"]
    if pid.startswith("price_"):
        return pid
    if pid.startswith("prod_"):
        hit = _price_cache.get("product:" + pid)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        default = stripe.get(f"products/{pid}").get("default_price")
        if isinstance(default, dict):
            default = default.get("id")
        if not default:
            raise BillingError("the product in PROADS_STRIPE_PRICE_ID has no default price")
        _price_cache["product:" + pid] = (time.time(), default)
        return default
    raise BillingError("PROADS_STRIPE_PRICE_ID must be a price ID that starts with price_ "
                       "(Stripe: Product catalog > ProAds Pro > Pricing > the price's ID), not the amount")


def price_info(stripe: Stripe, pid: Optional[str] = None) -> dict:
    """Name and price of the plan for the app to show (cached for an hour)."""
    pid = pid or price_id(stripe)
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


def premium_available() -> bool:
    return available() and bool(settings()["premium_price"])


def tier_of_price(pid: Optional[str]) -> str:
    s = settings()
    return "premium" if pid and s["premium_price"] and pid == s["premium_price"] else "pro"


def checkout_session(stripe: Stripe, client_id: str, email: str, customer_id: Optional[str],
                     return_base: str, first_time: bool, tier: str = "pro") -> str:
    s = settings()
    pid = s["premium_price"] if tier == "premium" and s["premium_price"] else price_id(stripe)
    data = {"mode": "subscription", "line_items[0][price]": pid, "line_items[0][quantity]": "1",
            "success_url": return_base + "/app#/billing?paid=1", "cancel_url": return_base + "/app#/billing",
            "client_reference_id": client_id, "metadata[client_id]": client_id,
            "subscription_data[metadata][client_id]": client_id,
            "allow_promotion_codes": "true", "billing_address_collection": "auto",
            "tax_id_collection[enabled]": "true"}
    if fee_percent() > 0:                    # the customer sees the full price before paying
        data["custom_text[submit][message]"] = (f"Plus {fee_percent():g}% of the ad spend you run through ProAds, "
                                                "added to each monthly invoice.")
    if customer_id:
        data.update({"customer": customer_id, "customer_update[name]": "auto", "customer_update[address]": "auto"})
    elif email:
        data["customer_email"] = email
    if first_time and s["trial_days"]:
        data["subscription_data[trial_period_days]"] = str(s["trial_days"])
    return stripe.post("checkout/sessions", data)["url"]


def change_tier(stripe: Stripe, subscription_id: str, tier: str) -> None:
    """Move an existing subscription between Pro and Premium; Stripe prorates the difference."""
    s = settings()
    sub = stripe.get(f"subscriptions/{subscription_id}")
    items = (sub.get("items") or {}).get("data") or []
    if not items:
        raise BillingError("subscription has no items")
    stripe.post(f"subscriptions/{subscription_id}", {
        "items[0][id]": items[0]["id"], "items[0][price]": s["premium_price"] if tier == "premium" else price_id(stripe),
        "proration_behavior": "always_invoice", "metadata[client_id]": (sub.get("metadata") or {}).get("client_id", "")})


def portal_session(stripe: Stripe, customer_id: str, return_url: str) -> str:
    return stripe.post("billing_portal/sessions", {"customer": customer_id, "return_url": return_url})["url"]


def _period_start(sub: dict) -> Optional[int]:
    if sub.get("current_period_start"):
        return int(sub["current_period_start"])
    items = (sub.get("items") or {}).get("data") or []
    return int(items[0]["current_period_start"]) if items and items[0].get("current_period_start") else None


def fee_amount(spend: float, percent: float) -> int:
    """Fee in the currency's smallest unit (fils for AED)."""
    return int(round(spend * percent / 100 * 100))


_fx_cache: dict = {}


def fx_rate(src: str, dst: str, http=None) -> float:
    """Exchange rate src -> dst from a free daily feed (cached 6 hours)."""
    src, dst = src.upper(), dst.upper()
    if src == dst:
        return 1.0
    hit = _fx_cache.get(src)
    if not hit or time.time() - hit[0] > 6 * 3600:
        if http is None:
            import requests as http
        r = http.get(f"https://open.er-api.com/v6/latest/{src}", timeout=20)
        body = r.json()
        if body.get("result") != "success":
            raise BillingError(f"no exchange rate for {src}")
        hit = _fx_cache[src] = (time.time(), body["rates"])
    if dst not in hit[1]:
        raise BillingError(f"no exchange rate {src} to {dst}")
    return float(hit[1][dst])


def add_spend_fee(stripe: "Stripe", invoice: dict, spend: float, currency: str, percent: float,
                  rate: Optional[float] = None) -> Optional[dict]:
    """Add the ad-spend fee to a draft renewal invoice. Ad spend in another currency is converted
    at today's rate (shown on the invoice). Returns what was added, or None."""
    inv_cur = (invoice.get("currency") or "").upper()
    if spend <= 0 or percent <= 0 or not currency:
        return None
    rate = 1.0 if currency.upper() == inv_cur else (rate if rate is not None else fx_rate(currency, inv_cur))
    converted = round(spend * rate, 2)
    amount = fee_amount(converted, percent)
    if amount <= 0:
        return None
    fmt = lambda t: time.strftime("%d %b %Y", time.gmtime(t))
    spent = f"{currency.upper()} {spend:,.2f}" + (f" (about {inv_cur} {converted:,.2f})" if rate != 1.0 else "")
    desc = (f"Ad spend fee: {percent:g}% of {spent} spent on campaigns published "
            f"through ProAds ({fmt(invoice['period_start'])} - {fmt(invoice['period_end'])})")
    stripe.post("invoiceitems", {"customer": invoice["customer"], "invoice": invoice["id"], "amount": str(amount),
                                 "currency": inv_cur.lower(), "description": desc,
                                 "metadata[kind]": "ad_spend_fee", "metadata[spend]": f"{spend:.2f}"},
                idempotency_key=f"proads-fee-{invoice['id']}")
    return {"amount": amount / 100, "spend": spend, "currency": inv_cur, "description": desc}


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
    FEES = """CREATE TABLE IF NOT EXISTS billing_fees (
        invoice_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, spend REAL, currency TEXT, fee REAL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')))"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.execute(self.FEES)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(billing)").fetchall()}
        if "period_start" not in cols:
            self.db.execute("ALTER TABLE billing ADD COLUMN period_start INTEGER")
        fee_cols = {r[1] for r in self.db.execute("PRAGMA table_info(billing_fees)").fetchall()}
        if "fee_currency" not in fee_cols:
            self.db.execute("ALTER TABLE billing_fees ADD COLUMN fee_currency TEXT")
        if "tier" not in cols:
            self.db.execute("ALTER TABLE billing ADD COLUMN tier TEXT")
        if "ads_trial_started" not in cols:
            self.db.execute("ALTER TABLE billing ADD COLUMN ads_trial_started INTEGER")
        self.db.commit()

    def start_ads_trial(self, client_id: str) -> None:
        """The free ad test starts at the first Go live (once)."""
        if not (self.get(client_id) or {}).get("ads_trial_started"):
            self.upsert(client_id, ads_trial_started=int(time.time()))

    def ads_access(self, client_id: str) -> dict:
        b = self.get(client_id) or {}
        plan = b.get("status") in ACTIVE
        started = b.get("ads_trial_started")
        ends = started + int(ads_trial_days() * 86400) if started else None
        in_trial = bool(started) and time.time() < ends
        return {"plan": plan, "trial_days": ads_trial_days(), "trial_started": started, "trial_ends": ends,
                "in_trial": in_trial and not plan, "allowed": plan or not started or in_trial}

    def fee_logged(self, invoice_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM billing_fees WHERE invoice_id=?", (invoice_id,)).fetchone() is not None

    def log_fee(self, invoice_id: str, client_id: str, spend: float, currency: str, fee: float,
                fee_currency: str = "") -> None:
        self.db.execute("INSERT OR IGNORE INTO billing_fees (invoice_id, client_id, spend, currency, fee, fee_currency)"
                        " VALUES (?,?,?,?,?,?)", (invoice_id, client_id, spend, currency, fee, fee_currency or currency))
        self.db.commit()

    def fees(self, client_id: str) -> list[dict]:
        rows = self.db.execute("SELECT invoice_id, spend, currency, fee, created_at, COALESCE(fee_currency, currency)"
                               " FROM billing_fees WHERE client_id=? ORDER BY created_at DESC LIMIT 24", (client_id,)).fetchall()
        return [dict(zip(("invoice_id", "spend", "currency", "fee", "created_at", "fee_currency"), r)) for r in rows]

    def get(self, client_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT customer_id, subscription_id, status, period_end, cancel_at_period_end,"
                              " period_start, ads_trial_started, tier FROM billing WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            return None
        return dict(zip(("customer_id", "subscription_id", "status", "period_end", "cancel_at_period_end",
                         "period_start", "ads_trial_started", "tier"), row))

    def tier(self, client_id: str) -> str:
        b = self.get(client_id) or {}
        return (b.get("tier") or "pro") if b.get("status") in ACTIVE else "free"

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
                            period_end=_period_end(obj), period_start=_period_start(obj),
                            tier=tier_of_price((((obj.get("items") or {}).get("data") or [{}])[0].get("price") or {}).get("id")),
                            cancel_at_period_end=1 if obj.get("cancel_at_period_end") else 0)
                return cid
        if etype == "invoice.payment_failed":
            log.warning("payment failed for customer %s", obj.get("customer"))
        return None
