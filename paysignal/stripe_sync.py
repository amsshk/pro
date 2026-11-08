"""Read a business's Stripe automatically with a restricted (read-only) API key.

The owner creates a restricted key in Stripe with READ access to Charges, Customers, Refunds and Disputes, and
pastes it once. ProAds then:
  * imports the last 12 months of successful payments (first sync, up to 10,000; newest first)
  * every few minutes, picks up new payments, refunds and lost disputes
Records use the same IDs as the Stripe webhook (charge id; "<charge>:refund:<running total>"), so a business
using both never gets a sale or refund counted twice.

The key is stored encrypted and never shown again. Full secret keys (sk_) are refused; ProAds only ever reads.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from .connectors.stripe import _major
from .models import Payment

API = "https://api.stripe.com/v1"
HISTORY_DAYS = 365
MAX_PAGES = 100                  # 100 x 100 = 10,000 charges per run
OVERLAP = 3 * 86400              # re-read the last 3 days: late captures and slow refunds
KEY_HELP_URL = ("https://dashboard.stripe.com/apikeys/create?name=ProAds"
                "&permissions%5B%5D=rak_charge_read&permissions%5B%5D=rak_customer_read"
                "&permissions%5B%5D=rak_refund_read&permissions%5B%5D=rak_dispute_read")


class StripeSyncError(Exception):
    pass


def _get(http, key: str, path: str, params: Optional[dict] = None) -> dict:
    try:
        r = http.get(f"{API}/{path}", params=params or {}, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    except Exception as e:
        raise StripeSyncError(f"Couldn't reach Stripe: {e}") from e
    body = {}
    try:
        body = r.json()
    except ValueError:
        pass
    if r.status_code == 401:
        raise StripeSyncError("Stripe didn't accept this key. It may have been deleted or rolled; paste a new one.")
    if r.status_code == 403:
        raise StripeSyncError("This key can't read " + path.split("/")[0] + ". In Stripe, give the key READ access to "
                              "Charges, Customers, Refunds and Disputes.")
    if r.status_code == 429:
        raise StripeSyncError("Stripe asked ProAds to slow down. It will try again in a few minutes.")
    if r.status_code != 200:
        raise StripeSyncError(((body.get("error") or {}).get("message")) or f"Stripe error {r.status_code}")
    return body


def check_key(http, key: str) -> dict:
    """Only restricted keys (rk_…). A full secret key (sk_…) can move money, so ProAds refuses to hold one."""
    key = (key or "").strip()
    if key.startswith("pk_"):
        raise StripeSyncError("That's a publishable key (pk_…). ProAds needs a restricted key (rk_…).")
    if key.startswith("sk_"):
        raise StripeSyncError("That's your full secret key, which can move money. Please don't share it with any app. "
                              "Create a restricted key (rk_…) with read access instead.")
    if not key.startswith(("rk_live_", "rk_test_")):
        raise StripeSyncError("That doesn't look like a Stripe restricted key. It should start with rk_live_.")
    _get(http, key, "charges", {"limit": 1})
    return {"live": key.startswith("rk_live_"), "kind": "restricted"}


def to_payments(ch: dict, client_id: str) -> list[Payment]:
    """One Stripe charge -> the purchase, plus its refund total so far (as the webhook records it)."""
    if ch.get("status") != "succeeded" or not ch.get("paid") or ch.get("captured") is False:
        return []
    currency = (ch.get("currency") or "usd").upper()
    billing = ch.get("billing_details") or {}
    meta = ch.get("metadata") or {}
    cust = ch.get("customer") if isinstance(ch.get("customer"), dict) else {}
    name = billing.get("name") or cust.get("name") or ""
    addr = billing.get("address") or (cust.get("address") or {})
    common = dict(
        client_id=client_id, source="stripe", currency=currency,
        occurred_at=datetime.fromtimestamp(ch.get("created") or time.time(), tz=timezone.utc),
        email=billing.get("email") or ch.get("receipt_email") or cust.get("email"),
        phone=billing.get("phone") or cust.get("phone"),
        customer_id=cust.get("id") or (ch.get("customer") if isinstance(ch.get("customer"), str) else None),
        fbclid=meta.get("fbclid"), ttclid=meta.get("ttclid"),
        utm_source=meta.get("utm_source"), utm_medium=meta.get("utm_medium"),
        utm_campaign=meta.get("utm_campaign"), utm_content=meta.get("utm_content"),
        promo_code=meta.get("promo_code"), fbp=meta.get("fbp"), fbc=meta.get("fbc"),
        first_name=name.split(" ")[0] or None, last_name=" ".join(name.split(" ")[1:]) or None,
        city=(addr or {}).get("city"), zip=(addr or {}).get("postal_code"), country=(addr or {}).get("country"))
    out = [Payment(external_id=ch["id"], kind="purchase", amount=_major(ch["amount"], currency), **common)]
    refunded = int(ch.get("amount_refunded") or 0)
    if refunded:
        out.append(Payment(external_id=f"{ch['id']}:refund:{refunded}", kind="refund",
                           amount=_major(refunded, currency), refund_of=ch["id"], **common))
    return out


def fetch(http, key: str, client_id: str, since: int, now: Optional[int] = None) -> dict:
    """Payments, refunds and lost disputes since `since`. Returns {"payments", "pages", "truncated", "newest"}."""
    now = int(now or time.time())
    payments, pages, truncated = [], 0, False
    params = {"limit": 100, "created[gte]": since, "expand[]": "data.customer"}
    expand_ok = True
    after = None
    while True:
        p = dict(params)
        if after:
            p["starting_after"] = after
        if not expand_ok:
            p.pop("expand[]", None)
        try:
            body = _get(http, key, "charges", p)
        except StripeSyncError as e:
            if expand_ok and "Customers" in str(e) and "can't read" in str(e):
                expand_ok = False                     # no Customers access: carry on with what charges carry
                continue
            raise
        for ch in body.get("data") or []:
            payments.extend(to_payments(ch, client_id))
        pages += 1
        if not body.get("has_more"):
            break
        if pages >= MAX_PAGES:
            truncated = True
            break
        after = body["data"][-1]["id"]
    # refunds made recently on OLDER charges (their charge isn't in the list above)
    seen = {x.external_id for x in payments}
    try:
        refunds = _get(http, key, "refunds", {"limit": 100, "created[gte]": since}).get("data") or []
    except StripeSyncError as e:
        if "can't read" not in str(e):
            raise
        refunds = []
    for rf in refunds:
        cid = rf.get("charge") if isinstance(rf.get("charge"), str) else (rf.get("charge") or {}).get("id")
        if not cid or rf.get("status") not in ("succeeded", "pending"):
            continue
        if any(x.refund_of == cid for x in payments):
            continue
        ch = _get(http, key, f"charges/{cid}", {})
        for x in to_payments(ch, client_id):
            if x.kind == "refund" and x.external_id not in seen:
                payments.append(x)
                seen.add(x.external_id)
    # lost disputes (chargebacks): the money is gone, so they count as refunds
    try:
        disputes = _get(http, key, "disputes", {"limit": 100, "created[gte]": since - 60 * 86400}).get("data") or []
    except StripeSyncError as e:
        if "can't read" not in str(e):
            raise
        disputes = []
    for d in disputes:
        if d.get("status") != "lost":
            continue
        cid = d.get("charge") if isinstance(d.get("charge"), str) else (d.get("charge") or {}).get("id")
        cur = (d.get("currency") or "usd").upper()
        payments.append(Payment(client_id=client_id, source="stripe", external_id=f"dispute:{d['id']}", kind="refund",
                                amount=_major(int(d.get("amount") or 0), cur), currency=cur, refund_of=cid,
                                occurred_at=datetime.fromtimestamp(d.get("created") or now, tz=timezone.utc)))
    return {"payments": payments, "pages": pages, "truncated": truncated}


class StripeLinks:
    SCHEMA = """CREATE TABLE IF NOT EXISTS stripe_keys (client_id TEXT PRIMARY KEY, key_enc TEXT NOT NULL,
        live INTEGER NOT NULL, kind TEXT NOT NULL, connected_at INTEGER NOT NULL, last_sync INTEGER, status TEXT,
        message TEXT, imported INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0)"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def save(self, client_id: str, key_enc: str, live: bool, kind: str) -> None:
        self.db.execute("INSERT INTO stripe_keys (client_id, key_enc, live, kind, connected_at, status) VALUES (?,?,?,?,?,"
                        "'syncing') ON CONFLICT(client_id) DO UPDATE SET key_enc=excluded.key_enc, live=excluded.live,"
                        " kind=excluded.kind, connected_at=excluded.connected_at, last_sync=NULL, status='syncing',"
                        " message=NULL", (client_id, key_enc, int(live), kind, int(time.time())))
        self.db.commit()

    def get(self, client_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT client_id, key_enc, live, kind, connected_at, last_sync, status, message, imported,"
                            " truncated FROM stripe_keys WHERE client_id=?", (client_id,)).fetchone()
        keys = ("client_id", "key_enc", "live", "kind", "connected_at", "last_sync", "status", "message", "imported", "truncated")
        return dict(zip(keys, r)) if r else None

    def all(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT client_id FROM stripe_keys")]

    def result(self, client_id: str, ok: bool, message: Optional[str], new: int = 0, truncated: bool = False,
               synced_at: Optional[int] = None) -> None:
        if ok:
            self.db.execute("UPDATE stripe_keys SET last_sync=?, status='ok', message=?, imported=imported+?,"
                            " truncated=MAX(truncated, ?) WHERE client_id=?",
                            (int(synced_at or time.time()), message, new, int(truncated), client_id))
        else:
            self.db.execute("UPDATE stripe_keys SET status='error', message=? WHERE client_id=?", (message, client_id))
        self.db.commit()

    def remove(self, client_id: str) -> None:
        self.db.execute("DELETE FROM stripe_keys WHERE client_id=?", (client_id,))
        self.db.commit()

    @staticmethod
    def public(row: Optional[dict]) -> dict:
        if not row:
            return {"connected": False, "key_help_url": KEY_HELP_URL}
        return {"connected": True, "live": bool(row["live"]), "kind": row["kind"], "status": row["status"],
                "message": row["message"], "last_sync": row["last_sync"], "imported": row["imported"],
                "truncated": bool(row["truncated"]), "connected_at": row["connected_at"], "key_help_url": KEY_HELP_URL}
