"""Home dashboard: sales, real profit and orders for a period vs the period before,
day by day, plus where sales came from and what sold. Measured from stored orders."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from .attribution import resolve
from .engine import Engine
from .identity import customer_key

PERIODS = {"today": 1, "7": 7, "30": 30, "90": 90, "365": 365}


def _realized(engine: Engine, p, refunded: float) -> float:
    keep = 1 - min(refunded / p.amount, 1.0) if p.amount else 0.0
    return engine.scorer.margin(p) * keep


def _change(now: float, before: float) -> Optional[float]:
    return round((now - before) / before, 3) if before else None


def build_dashboard(engine: Engine, period: str = "30", now: Optional[datetime] = None, currency: str = "AED") -> dict:
    days = PERIODS.get(period, 30)
    now = now or datetime.now(timezone.utc)
    cid = engine.client_id
    purchases = engine.store.purchases(cid)
    out = {"currency": currency, "period": period, "days": days, "has_data": bool(purchases)}
    if not purchases:
        return out
    last = max(p.occurred_at for p in purchases)
    # uploaded history may end before today: show its last period instead of an empty chart
    end_day = now.date() if (now - last).days < days else last.date()
    out["as_of"] = end_day.isoformat()
    out["live"] = end_day == now.date()
    start = end_day - timedelta(days=days - 1)
    prev_start = start - timedelta(days=days)

    seen_customers: dict[str, datetime] = {}
    for p in sorted(purchases, key=lambda x: x.occurred_at):
        key = customer_key(p.email, p.phone, p.customer_id)
        if key and key not in seen_customers:
            seen_customers[key] = p.occurred_at

    cur = {"sales": defaultdict(float), "profit": defaultdict(float), "orders": defaultdict(int)}
    prev = {"sales": defaultdict(float), "profit": defaultdict(float), "orders": defaultdict(int)}
    totals = {"cur": defaultdict(float), "prev": defaultdict(float)}
    sources, products = defaultdict(lambda: [0.0, 0]), defaultdict(lambda: [0.0, 0])
    returning = [0, 0]
    for p in purchases:
        d = p.occurred_at.date()
        if prev_start <= d < start:
            bucket, t, idx = prev, totals["prev"], (d - prev_start).days
        elif start <= d <= end_day:
            bucket, t, idx = cur, totals["cur"], (d - start).days
        else:
            continue
        refunded = engine.store.refunded_amount(cid, p.external_id)
        profit = _realized(engine, p, refunded)
        bucket["sales"][idx] += p.amount
        bucket["profit"][idx] += profit
        bucket["orders"][idx] += 1
        t["sales"] += p.amount
        t["profit"] += profit
        t["orders"] += 1
        t["refunds"] += refunded
        if bucket is cur:
            touch = resolve(p, engine.promo_map)
            label = {"unattributed": "Direct / unknown"}.get(touch.kind) or \
                f"{touch.platform.capitalize()} {'ads' if touch.kind == 'paid' else 'posts'}"
            sources[label][0] += p.amount
            sources[label][1] += 1
            for it in p.items:
                products[it.name or it.sku][0] += it.unit_price * it.quantity
                products[it.name or it.sku][1] += it.quantity
            key = customer_key(p.email, p.phone, p.customer_id)
            if key:
                returning[1] += 1
                if seen_customers.get(key) and seen_customers[key] < p.occurred_at:
                    returning[0] += 1

    def series(b, name):
        return [round(b[name].get(i, 0.0), 2) for i in range(days)]

    c, pv = totals["cur"], totals["prev"]
    aov = lambda t: t["sales"] / t["orders"] if t["orders"] else 0.0
    metrics = [
        ("sales", "Total sales", c["sales"], pv["sales"]),
        ("profit", "Real profit", c["profit"], pv["profit"]),
        ("orders", "Orders", c["orders"], pv["orders"]),
        ("aov", "Average order value", aov(c), aov(pv)),
    ]
    out["metrics"] = [{"key": k, "label": l, "value": round(v, 2), "previous": round(b, 2), "change": _change(v, b)}
                      for k, l, v, b in metrics]
    out["refund_rate"] = round(c["refunds"] / c["sales"], 3) if c["sales"] else 0.0
    out["returning_rate"] = round(returning[0] / returning[1], 3) if returning[1] else None
    out["dates"] = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    out["series"] = {
        "sales": {"current": series(cur, "sales"), "previous": series(prev, "sales")},
        "profit": {"current": series(cur, "profit"), "previous": series(prev, "profit")},
        "orders": {"current": series(cur, "orders"), "previous": series(prev, "orders")},
    }
    out["sources"] = sorted(({"source": k, "sales": round(v[0], 2), "orders": v[1]} for k, v in sources.items()),
                            key=lambda r: -r["sales"])[:6]
    out["products"] = sorted(({"product": k, "sales": round(v[0], 2), "units": v[1]} for k, v in products.items()),
                             key=lambda r: -r["sales"])[:5]
    return out
