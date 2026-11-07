"""Money Leak Detector: where a business loses money OUTSIDE winning new customers, ranked by AED a month.

Every leak says how it was measured:
  measured  - straight from the business's own orders, refunds and ads
  up to     - the money at stake; only part of it can usually be won back (e.g. unpaid orders)
Nothing is invented: a leak only appears when the data for it exists.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .engine import Engine

MIN_CODE_ORDERS = 10
MIN_PRODUCT_UNITS = 10
LOOKBACK_DAYS = 180


class UnpaidStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS unpaid_orders (
        key TEXT PRIMARY KEY, client_id TEXT NOT NULL, day TEXT NOT NULL, amount REAL NOT NULL,
        status TEXT, source TEXT)"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def add(self, client_id: str, rows: list[dict]) -> int:
        n = 0
        for r in rows:
            key = hashlib.sha1(f"{client_id}|{r['source']}|{r.get('ref') or ''}|{r['date']}|{r['amount']}".encode()).hexdigest()
            cur = self.db.execute("INSERT OR IGNORE INTO unpaid_orders (key, client_id, day, amount, status, source)"
                                  " VALUES (?,?,?,?,?,?)", (key, client_id, r["date"], r["amount"], r["status"], r["source"]))
            n += cur.rowcount
        self.db.commit()
        return n

    def since(self, client_id: str, day: str) -> list[tuple[str, float, str]]:
        return self.db.execute("SELECT day, amount, status FROM unpaid_orders WHERE client_id=? AND day>=?",
                               (client_id, day)).fetchall()

    def clear(self, client_id: str) -> None:
        self.db.execute("DELETE FROM unpaid_orders WHERE client_id=?", (client_id,))
        self.db.commit()


def build_leaks(engine: Engine, currency: str, unpaid: list[tuple[str, float, str]],
                economics: Optional[dict] = None, losing_ads: Optional[list[dict]] = None,
                now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cid = engine.client_id
    rows = []
    for p in engine.store.purchases(cid):
        ref = engine.store.refunded_amount(cid, p.external_id)
        keep = 1 - min(ref / p.amount, 1.0) if p.amount else 0.0
        rows.append((p, ref, engine.scorer.margin(p) * keep))
    leaks = []
    if rows:
        last = max(p.occurred_at for p, _, _ in rows).date()
        start = last - timedelta(days=LOOKBACK_DAYS - 1)
        rows = [r for r in rows if r[0].occurred_at.date() >= start]
        first = min(p.occurred_at for p, _, _ in rows).date()
        months = max(1.0, ((last - first).days + 1) / 30.4)
        per_month = lambda x: x / months
        gross = sum(p.amount for p, _, _ in rows)
        refunds = sum(r for _, r, _ in rows)

        # 1. discount codes that cost more profit than they bring
        codes = defaultdict(lambda: [0, 0.0])
        for p, r, v in rows:
            c = codes[(p.promo_code or "").upper()]
            c[0] += 1; c[1] += v
        base = codes.get("")
        if base and base[0]:
            full = base[1] / base[0]
            for code, (n, v) in codes.items():
                if not code or n < MIN_CODE_ORDERS:
                    continue
                lost = (full - v / n) * n
                if lost > 0:
                    leaks.append({"key": f"code:{code}", "kind": "discount", "title": f"Discount code {code}",
                                  "per_month": round(per_month(lost), 2), "how": "measured",
                                  "evidence": f"{n} orders made {fmt(v / n)} profit each vs {fmt(full)} at full price "
                                              f"({(v / n) / full - 1:.0%}).",
                                  "fix": "Stop the code, or turn it into a bundle or free add-on that costs you less than the discount."})

        # 2. products sent back far more than the rest
        avg_rate = refunds / gross if gross else 0.0
        prod = defaultdict(lambda: [0, 0.0, 0.0])
        for p, r, _ in rows:
            total = sum(i.unit_price * i.quantity for i in p.items) or 0
            for i in p.items:
                share = (i.unit_price * i.quantity / total) if total else 0
                x = prod[i.name]
                x[0] += i.quantity; x[1] += i.unit_price * i.quantity; x[2] += r * share
        for name, (units, g, rf) in prod.items():
            if units < MIN_PRODUCT_UNITS or not g:
                continue
            rate = rf / g
            excess = (rate - max(avg_rate, 0.03)) * g
            if rate >= max(0.10, avg_rate * 1.5) and excess > 0:
                leaks.append({"key": f"product:{name}", "kind": "refunds", "title": f"Refunds on {name}",
                              "per_month": round(per_month(excess), 2), "how": "measured",
                              "evidence": f"{rate:.0%} of its sales were refunded vs {avg_rate:.0%} for your shop overall.",
                              "fix": "Read why people return it: size, photos or description usually don't match what arrives. "
                                     "Pause ads on it until fixed."})

        # 3. delivery cost eating the margin (needs the business's own delivery cost)
        if economics and economics.get("inputs", {}).get("delivery_cost"):
            dc = economics["inputs"]["delivery_cost"]
            price = economics["inputs"].get("price") or 0
            if price and dc / price >= 0.08:
                leaks.append({"key": "delivery", "kind": "delivery", "title": "Delivery you pay for",
                              "per_month": round(per_month(dc * len(rows)), 2), "how": "up to",
                              "evidence": f"{fmt(dc)} per order is {dc / price:.0%} of your price, on {len(rows)} orders.",
                              "fix": "Charge part of delivery, or offer free delivery only above a basket size that covers it."})

    # 4. orders that never got paid (failed payments, pending, cancelled)
    if unpaid:
        cut = (now.date() - timedelta(days=LOOKBACK_DAYS - 1)).isoformat()
        recent = [(d, a, s) for d, a, s in unpaid if d >= cut]
        if recent:
            days = (now.date() - date.fromisoformat(min(d for d, _, _ in recent))).days + 1
            months = max(1.0, days / 30.4)
            total = sum(a for _, a, _ in recent)
            failed = sum(1 for _, _, s in recent if any(w in (s or "") for w in ("fail", "declin", "requires")))
            leaks.append({"key": "unpaid", "kind": "unpaid", "title": "Orders that were never paid",
                          "per_month": round(total / months, 2), "how": "up to",
                          "evidence": f"{len(recent)} orders worth {fmt(total)} stopped before payment"
                                      + (f", {failed} of them failed payments" if failed else "") + ".",
                          "fix": "Message these customers the same day with a payment link; check that the payment "
                                 "methods people want (Apple Pay, cash on delivery) work on phones."})

    # 5. ads that lose money (from the Profit engine)
    for a in losing_ads or []:
        leaks.append({"key": f"ad:{a['campaign_id']}", "kind": "ads", "title": f"Ad: {a['name'] or a['campaign_id']}",
                      "per_month": round(a["spend"], 2), "how": "measured",
                      "evidence": f"{a['label']} ({round((a.get('confidence') or 0) * 100)}% sure): {fmt(a['spend'])} spent "
                                  f"in 30 days, cost per sale {fmt(a['cpa']) if a.get('cpa') else 'no sales'}.",
                      "fix": "Pause it and move the budget to your best campaign (see Profit engine)."})

    leaks.sort(key=lambda x: -x["per_month"])
    measured = round(sum(l["per_month"] for l in leaks if l["how"] == "measured"), 2)
    at_stake = round(sum(l["per_month"] for l in leaks if l["how"] == "up to"), 2)
    return {"currency": currency, "leaks": leaks, "measured_per_month": measured, "at_stake_per_month": at_stake,
            "has_sales": bool(rows), "lookback_days": LOOKBACK_DAYS}


def fmt(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.2f}"
