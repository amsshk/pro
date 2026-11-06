"""Shopify app connection: a store owner clicks "Connect Shopify", approves on
Shopify's own screen, and ProAds imports their orders and receives new ones live.

Setup (once, by the ProAds owner):
  1. Create an app in the Shopify Dev Dashboard (dev.shopify.com).
     Redirect URL:  https://<your domain>/shopify/callback
     Compliance webhooks (customers/data_request, customers/redact, shop/redact):
                    https://<your domain>/webhooks/shopify-compliance
  2. Server settings: SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, PROADS_SECRET_KEY,
     and PROADS_PUBLIC_URL (e.g. https://pro.ezdevsoft.net).

Shopify limits (as of Nov 2026):
  - read_orders covers the last 60 days. Older orders need read_all_orders, which a
    Partner app must request and get approved (then set SHOPIFY_SCOPES).
  - Customer email/phone/name are "protected customer data": public apps need
    Shopify's review. Without it, orders import without them (repeat-customer
    findings are then weaker). This module retries without those fields.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Iterator, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from .models import LineItem, Payment

log = logging.getLogger("proads.shopify")

API_VERSION = "2026-10"
SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")
STATE_TTL = 600                       # seconds a connect link stays valid
PAID = {"PAID", "PARTIALLY_REFUNDED", "REFUNDED", "PARTIALLY_PAID"}


class ShopifyError(Exception):
    pass


# ---------------- settings ----------------

def settings() -> dict:
    return {
        "client_id": os.environ.get("SHOPIFY_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip(),
        "scopes": os.environ.get("SHOPIFY_SCOPES", "read_orders").strip(),
        "public_url": os.environ.get("PROADS_PUBLIC_URL", "").strip().rstrip("/"),
    }


def available() -> bool:
    s = settings()
    return bool(s["client_id"] and s["client_secret"] and os.environ.get("PROADS_SECRET_KEY"))


def _fernet():
    from cryptography.fernet import Fernet
    raw = os.environ.get("PROADS_SECRET_KEY", "").strip()
    if not raw:
        raise ShopifyError("PROADS_SECRET_KEY is not set on the server")
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


# ---------------- OAuth ----------------

def normalize_shop(value: str) -> Optional[str]:
    """'my-store', 'https://my-store.myshopify.com/admin' -> 'my-store.myshopify.com'."""
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://", "", v)
    m = re.match(r"^admin\.shopify\.com/store/([a-z0-9][a-z0-9\-]*)", v)   # the new admin address
    v = m.group(1) if m else v.split("/")[0]
    v = re.sub(r"^www\.", "", v)
    if v and "." not in v:
        v += ".myshopify.com"
    return v if SHOP_RE.match(v) else None


def authorize_url(shop: str, state: str, redirect_uri: str) -> str:
    s = settings()
    q = urlencode({"client_id": s["client_id"], "scope": s["scopes"], "redirect_uri": redirect_uri, "state": state})
    return f"https://{shop}/admin/oauth/authorize?{q}"


def verify_callback(args: dict, secret: str) -> bool:
    """Shopify signs the callback query: hmac over the other params, sorted, key=value joined by &."""
    given = args.get("hmac", "")
    message = "&".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in args.items() if k != "hmac"))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return bool(given) and hmac.compare_digest(expected, given)


def verify_webhook(body: bytes, header: str, secret: str) -> bool:
    digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return bool(header) and hmac.compare_digest(digest, header)


def _post_form(http, url: str, data: dict) -> dict:
    r = http.post(url, data=data, timeout=30)
    if r.status_code != 200:
        raise ShopifyError(f"Shopify refused the token request (HTTP {r.status_code})")
    return r.json()


def _token_record(data: dict) -> dict:
    now = time.time()
    return {"access_token": data["access_token"], "scope": data.get("scope", ""),
            "access_expires": now + int(data.get("expires_in") or 0) - 60 if data.get("expires_in") else None,
            "refresh_token": data.get("refresh_token"),
            "refresh_expires": now + int(data.get("refresh_token_expires_in") or 0) if data.get("refresh_token") else None}


def exchange_code(http, shop: str, code: str) -> dict:
    s = settings()
    return _token_record(_post_form(http, f"https://{shop}/admin/oauth/access_token", {
        "client_id": s["client_id"], "client_secret": s["client_secret"], "code": code, "expiring": "1"}))


def refresh_tokens(http, shop: str, refresh_token: str) -> dict:
    s = settings()
    return _token_record(_post_form(http, f"https://{shop}/admin/oauth/access_token", {
        "client_id": s["client_id"], "client_secret": s["client_secret"],
        "grant_type": "refresh_token", "refresh_token": refresh_token}))


# ---------------- Admin API ----------------

_MONEY = "shopMoney { amount currencyCode }"
_VISIT = "landingPage utmParameters { source medium campaign content }"
ORDER_FIELDS = f"""
  id name processedAt createdAt currencyCode test cancelledAt displayFinancialStatus sourceName
  totalPriceSet {{ {_MONEY} }}
  totalDiscountsSet {{ {_MONEY} }}
  discountCodes
  lineItems(first: 10) {{ nodes {{ sku title quantity originalUnitPriceSet {{ {_MONEY} }} }} }}
  refunds {{ id createdAt totalRefundedSet {{ {_MONEY} }} }}
"""
PROTECTED_FIELDS = f"""
  email phone customer {{ id }}
  customerJourneySummary {{ firstVisit {{ {_VISIT} }} lastVisit {{ {_VISIT} }} }}
"""


class ShopifyClient:
    def __init__(self, shop: str, token: str, http):
        self.shop, self.token, self.http = shop, token, http
        self.protected = True     # turned off if the app isn't approved for customer data

    def graphql(self, query: str, variables: dict | None = None, attempt: int = 0) -> dict:
        r = self.http.post(f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json", timeout=60,
                           headers={"X-Shopify-Access-Token": self.token, "Content-Type": "application/json"},
                           json={"query": query, "variables": variables or {}})
        if r.status_code == 401:
            raise ShopifyError("Shopify rejected the access token")
        if r.status_code in (429, 502, 503) and attempt < 5:
            time.sleep(2 * (attempt + 1))
            return self.graphql(query, variables, attempt + 1)
        if r.status_code != 200:
            raise ShopifyError(f"Shopify API error HTTP {r.status_code}")
        data = r.json()
        errors = data.get("errors") or []
        if errors:
            codes = {str((e.get("extensions") or {}).get("code", "")) for e in errors}
            if "THROTTLED" in codes and attempt < 5:
                time.sleep(2 * (attempt + 1))
                return self.graphql(query, variables, attempt + 1)
            raise ShopifyError("; ".join(str(e.get("message")) for e in errors)[:500])
        return data["data"]

    def orders(self, since: Optional[str] = None) -> Iterator[dict]:
        cursor = None
        search = f"processed_at:>={since}" if since else None
        while True:
            fields = ORDER_FIELDS + (PROTECTED_FIELDS if self.protected else "")
            q = ("query($cursor: String, $search: String) { orders(first: 25, after: $cursor, query: $search, "
                 "sortKey: PROCESSED_AT) { nodes {" + fields + "} pageInfo { hasNextPage endCursor } } }")
            try:
                page = self.graphql(q, {"cursor": cursor, "search": search})["orders"]
            except ShopifyError as e:
                msg = str(e).lower()
                if self.protected and ("access denied" in msg or "protected" in msg or "not approved" in msg):
                    log.warning("shop %s: no protected customer data access, importing without it", self.shop)
                    self.protected = False
                    continue
                raise
            yield from page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"]["endCursor"]

    def register_webhooks(self, callback_url: str) -> list[str]:
        problems = []
        m = ("mutation($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {"
             " webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {"
             " webhookSubscription { id } userErrors { field message } } }")
        for topic in ("ORDERS_PAID", "REFUNDS_CREATE", "APP_UNINSTALLED"):
            try:
                errs = self.graphql(m, {"topic": topic, "sub": {"uri": callback_url}})[
                    "webhookSubscriptionCreate"]["userErrors"]
                problems += [f"{topic}: {e['message']}" for e in errs if "taken" not in e["message"].lower()]
            except ShopifyError as e:
                problems.append(f"{topic}: {e}")
        return problems


# ---------------- order -> Payment ----------------

def _num_id(gid: str) -> str:
    return str(gid).rsplit("/", 1)[-1]


def _money(m) -> float:
    try:
        return float(((m or {}).get("shopMoney") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def order_to_payments(o: dict, client_id: str, costs: dict | None = None) -> list[Payment]:
    """One purchase plus one refund per Shopify refund. Ids match the webhook ids,
    so an order arriving by import and by webhook is stored once."""
    costs = costs or {}
    if o.get("test") or (o.get("displayFinancialStatus") or "") not in PAID:
        return []
    oid = _num_id(o["id"])
    currency = ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("currencyCode") or o.get("currencyCode")
    journey = o.get("customerJourneySummary") or {}
    visit = journey.get("lastVisit") or journey.get("firstVisit") or {}
    utm = visit.get("utmParameters") or {}
    qs = parse_qs(urlparse(visit.get("landingPage") or "").query)
    items = []
    for li in (o.get("lineItems") or {}).get("nodes") or []:
        sku = li.get("sku") or li.get("title") or ""
        items.append(LineItem(sku, li.get("title") or "", int(li.get("quantity") or 1),
                              _money(li.get("originalUnitPriceSet")), costs.get(sku)))
    customer = o.get("customer") or {}
    codes = o.get("discountCodes") or []
    out = [Payment(
        client_id=client_id, source="shopify", external_id=oid, kind="purchase",
        amount=_money(o.get("totalPriceSet")), currency=currency, occurred_at=_ts(o["processedAt"]),
        email=o.get("email"), phone=o.get("phone"),
        customer_id=_num_id(customer["id"]) if customer.get("id") else None,
        discount=_money(o.get("totalDiscountsSet")), promo_code=codes[0] if codes else None, items=items,
        utm_source=utm.get("source"), utm_medium=utm.get("medium"), utm_campaign=utm.get("campaign"),
        utm_content=utm.get("content"), fbclid=(qs.get("fbclid") or [None])[0],
        ttclid=(qs.get("ttclid") or [None])[0],
    )]
    for rf in o.get("refunds") or []:
        amount = _money(rf.get("totalRefundedSet"))
        if amount > 0:
            out.append(Payment(client_id=client_id, source="shopify", external_id=f"refund:{_num_id(rf['id'])}",
                               kind="refund", amount=amount, currency=currency,
                               occurred_at=_ts(rf.get("createdAt") or o["processedAt"]), refund_of=oid,
                               email=o.get("email")))
    return out


def compliance_customer_keys(payload: dict) -> list[str]:
    """Customer keys to erase for a customers/redact request."""
    from .identity import customer_key
    cust = payload.get("customer") or {}
    keys = {customer_key(cust.get("email"), None, None), customer_key(None, cust.get("phone"), None),
            customer_key(None, None, str(cust["id"]) if cust.get("id") else None)}
    return [k for k in keys if k]
