"""ProAds' own revenue from Stripe, as purchase data for ProAds' own advertising.

  first     the first successfully paid subscription invoice of a client = one new customer acquired
            (the only kind sent to Meta as a Purchase, so Meta optimises for new paying subscribers)
  renewal   later subscription invoices = recurring revenue; kept for buyer value, never sent as a Purchase
  fee       ad-spend-fee invoices = revenue, not an acquisition

Value is what was actually collected: amount paid minus tax (discounts are already out of it). Refunds and lost
chargebacks are recorded against the invoice. Stripe invoice IDs make every record idempotent.
"""
from __future__ import annotations

import json
import time
from typing import Optional

ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}


def major(amount_minor: int, currency: str) -> float:
    return float(amount_minor) / (1 if currency.upper() in ZERO_DECIMAL else 100)


def invoice_tax(inv: dict) -> int:
    """Tax in minor units, across Stripe API versions."""
    if inv.get("total_taxes"):
        return sum(int(t.get("amount") or 0) for t in inv["total_taxes"])
    if inv.get("total_tax_amounts"):
        return sum(int(t.get("amount") or 0) for t in inv["total_tax_amounts"])
    return int(inv.get("tax") or 0)


def invoice_payment_refs(inv: dict) -> tuple[Optional[str], Optional[str]]:
    """(charge id, payment intent id) across Stripe API versions."""
    charge = inv.get("charge") if isinstance(inv.get("charge"), str) else None
    pi = inv.get("payment_intent") if isinstance(inv.get("payment_intent"), str) else None
    for pay in ((inv.get("payments") or {}).get("data") or []):
        p = pay.get("payment") or {}
        pi = pi or p.get("payment_intent")
        charge = charge or p.get("charge")
    return charge, pi


class RevenueStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS proads_revenue (
        invoice_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, kind TEXT NOT NULL, tier TEXT,
        amount REAL NOT NULL, tax REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL, paid_at INTEGER NOT NULL,
        charge_id TEXT, payment_intent TEXT, refunded REAL NOT NULL DEFAULT 0, chargeback REAL NOT NULL DEFAULT 0,
        utm TEXT, sent_to_meta INTEGER NOT NULL DEFAULT 0)"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def has_first(self, client_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM proads_revenue WHERE client_id=? AND kind='first'", (client_id,)).fetchone() is not None

    def get(self, invoice_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT invoice_id, client_id, kind, tier, amount, tax, currency, paid_at, charge_id,"
                            " payment_intent, refunded, chargeback, utm FROM proads_revenue WHERE invoice_id=?",
                            (invoice_id,)).fetchone()
        return self._row(r)

    @staticmethod
    def _row(r) -> Optional[dict]:
        if not r:
            return None
        keys = ("invoice_id", "client_id", "kind", "tier", "amount", "tax", "currency", "paid_at", "charge_id",
                "payment_intent", "refunded", "chargeback", "utm")
        d = dict(zip(keys, r))
        d["utm"] = json.loads(d["utm"]) if d["utm"] else {}
        return d

    def add(self, row: dict) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO proads_revenue (invoice_id, client_id, kind, tier, amount, tax, currency, paid_at,"
            " charge_id, payment_intent, utm) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row["invoice_id"], row["client_id"], row["kind"], row.get("tier"), row["amount"], row.get("tax", 0),
             row["currency"], row["paid_at"], row.get("charge_id"), row.get("payment_intent"), json.dumps(row.get("utm") or {})))
        self.db.commit()
        return cur.rowcount == 1

    def find_payment(self, charge_id: Optional[str], payment_intent: Optional[str], invoice_id: Optional[str]) -> Optional[dict]:
        for col, val in (("invoice_id", invoice_id), ("charge_id", charge_id), ("payment_intent", payment_intent)):
            if val:
                r = self.db.execute(f"SELECT invoice_id FROM proads_revenue WHERE {col}=?", (val,)).fetchone()
                if r:
                    return self.get(r[0])
        return None

    def set_refunded(self, invoice_id: str, refunded: float) -> None:
        self.db.execute("UPDATE proads_revenue SET refunded=? WHERE invoice_id=?", (refunded, invoice_id))
        self.db.commit()

    def add_chargeback(self, invoice_id: str, amount: float) -> None:
        self.db.execute("UPDATE proads_revenue SET chargeback=chargeback+? WHERE invoice_id=?", (amount, invoice_id))
        self.db.commit()

    def all(self) -> list[dict]:
        rows = self.db.execute("SELECT invoice_id, client_id, kind, tier, amount, tax, currency, paid_at, charge_id,"
                               " payment_intent, refunded, chargeback, utm FROM proads_revenue ORDER BY paid_at").fetchall()
        return [self._row(r) for r in rows]


def acquisition_profit(rows: list[dict], spend_by_slug: dict, slug_names: dict, service_cost: Optional[float],
                       service_is_estimate: bool) -> dict:
    """Customer Acquisition Profit, by the campaign that brought each paying subscriber.
    first payment collected (after refunds) - acquisition cost (campaign spend / its new subscribers) - service cost."""
    groups: dict = {}
    renew_by_client: dict = {}
    for r in rows:
        net = r["amount"] - r["refunded"] - r["chargeback"]
        if r["kind"] == "renewal":
            renew_by_client[r["client_id"]] = renew_by_client.get(r["client_id"], 0.0) + net
    for r in rows:
        if r["kind"] != "first":
            continue
        u = r["utm"] or {}
        paid_meta = (u.get("utm_medium") or "").lower() == "paid" and (u.get("utm_source") or "").lower() in (
            "meta", "facebook", "instagram", "fb", "ig")
        slug = u.get("utm_campaign") if paid_meta else None
        key = slug or ("Meta ads (campaign unknown)" if paid_meta else ("Other: " + u["utm_source"] if u.get("utm_source") else "No campaign (direct or organic)"))
        g = groups.setdefault(key, {"campaign": slug_names.get(slug, slug) if slug else key, "slug": slug, "subscribers": 0,
                                    "first_payments": 0.0, "renewals": 0.0, "refunds": 0.0, "currency": r["currency"]})
        g["subscribers"] += 1
        g["first_payments"] += r["amount"] - r["refunded"] - r["chargeback"]
        g["refunds"] += r["refunded"] + r["chargeback"]
        g["renewals"] += renew_by_client.get(r["client_id"], 0.0)
    out = []
    for g in groups.values():
        n = g["subscribers"]
        spend = spend_by_slug.get(g["slug"]) if g["slug"] else None
        cac = (spend / n) if spend is not None and n else None
        per_first = g["first_payments"] / n
        contrib = per_first - (cac or 0.0) - (service_cost or 0.0)
        out.append({**{k: round(v, 2) if isinstance(v, float) else v for k, v in g.items()},
                    "spend": round(spend, 2) if spend is not None else None,
                    "acquisition_cost": round(cac, 2) if cac is not None else None,
                    "first_payment_each": round(per_first, 2),
                    "service_cost_each": service_cost, "service_cost_is_estimate": service_is_estimate,
                    "acquisition_contribution_each": round(contrib, 2) if (cac is not None or not g["slug"]) else None})
    out.sort(key=lambda x: -x["subscribers"])
    firsts = [r for r in rows if r["kind"] == "first"]
    return {"campaigns": out, "new_subscribers": len(firsts),
            "first_payments": round(sum(r["amount"] - r["refunded"] - r["chargeback"] for r in firsts), 2),
            "renewals": round(sum(r["amount"] - r["refunded"] - r["chargeback"] for r in rows if r["kind"] == "renewal"), 2),
            "fees": round(sum(r["amount"] - r["refunded"] - r["chargeback"] for r in rows if r["kind"] == "fee"), 2),
            "refunds": round(sum(r["refunded"] + r["chargeback"] for r in rows), 2),
            "service_cost_each": service_cost, "service_cost_is_estimate": service_is_estimate}
