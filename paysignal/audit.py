"""First-week audit: what a business learns on day one from its own history.

Works from imported past orders (and past posts, if given). Everything here is
measured from real data, except the post section: old posts have no tracking
links, so their effect is ESTIMATED from sales lift in the days after each post.
That section is labelled as an estimate everywhere it appears.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from .engine import Engine
from .identity import customer_key
from .organic import OrganicEngine

DISCOUNT_MIN_ORDERS = 10      # a code needs this many orders before we judge it


def _realized(engine: Engine, p, refunded: float) -> float:
    keep = 1 - min(refunded / p.amount, 1.0) if p.amount else 0.0
    return engine.scorer.margin(p) * keep


def build_audit(engine: Engine, posts: Optional[list] = None, now: Optional[datetime] = None,
                currency: str = "AED") -> dict:
    now = now or datetime.now(timezone.utc)
    cid = engine.client_id
    engine.train(now)
    rows = []
    for p in engine.store.purchases(cid):
        ref = engine.store.refunded_amount(cid, p.external_id)
        rows.append((p, ref, _realized(engine, p, ref)))
    if not rows:
        return {"error": "no paid orders imported yet"}

    first, last = min(p.occurred_at for p, _, _ in rows), max(p.occurred_at for p, _, _ in rows)
    gross = sum(p.amount for p, _, _ in rows)
    refunds = sum(r for _, r, _ in rows)
    profit = sum(v for _, _, v in rows)
    out: dict = {"currency": currency, "period": {"from": first.date().isoformat(), "to": last.date().isoformat(),
                                                   "days": (last - first).days + 1},
                 "orders": len(rows), "gross": round(gross), "refunds": round(refunds),
                 "refund_rate": round(refunds / gross, 3) if gross else 0.0,
                 "real_profit": round(profit)}

    # --- monthly trend
    monthly = defaultdict(lambda: [0.0, 0.0, 0])
    for p, r, v in rows:
        m = monthly[p.occurred_at.strftime("%Y-%m")]
        m[0] += p.amount; m[1] += v; m[2] += 1
    out["monthly"] = [{"month": k, "gross": round(g), "real_profit": round(v), "orders": n}
                      for k, (g, v, n) in sorted(monthly.items())]

    # --- discount codes: which ones lose money
    by_code = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for p, r, v in rows:
        c = by_code[(p.promo_code or "").upper() or "(no code)"]
        c[0] += 1; c[1] += p.amount; c[2] += r; c[3] += v
    base = by_code.get("(no code)")
    base_per_order = base[3] / base[0] if base and base[0] else profit / len(rows)
    codes = []
    for code, (n, g, r, v) in by_code.items():
        if code == "(no code)" or n < DISCOUNT_MIN_ORDERS:
            continue
        per = v / n
        codes.append({"code": code, "orders": n, "refund_rate": round(r / g, 3) if g else 0.0,
                      "profit_per_order": round(per), "vs_full_price": round(per / base_per_order - 1, 2)
                      if base_per_order else 0.0})
    out["discount_codes"] = sorted(codes, key=lambda c: c["vs_full_price"])
    out["full_price_profit_per_order"] = round(base_per_order)

    # --- products with the most refunds (order refunds shared across its items by price)
    prod = defaultdict(lambda: [0, 0.0, 0.0])
    for p, r, _ in rows:
        total = sum(i.unit_price * i.quantity for i in p.items) or 0
        for i in p.items:
            share = (i.unit_price * i.quantity / total) if total else 0
            pr = prod[i.name]
            pr[0] += i.quantity; pr[1] += i.unit_price * i.quantity; pr[2] += r * share
    out["refund_heavy_products"] = sorted(
        [{"product": k, "units": n, "refund_rate": round(rf / g, 3)} for k, (n, g, rf) in prod.items()
         if n >= 10 and g and rf / g >= max(0.15, out["refund_rate"] * 1.5)],
        key=lambda x: -x["refund_rate"])[:5]

    # --- customers: how much comes from people who come back
    cust = defaultdict(list)
    for p, r, v in rows:
        ck = customer_key(p.email, p.phone, p.customer_id)
        if ck:
            cust[ck].append(v)
    if cust:
        repeaters = [vs for vs in cust.values() if len(vs) > 1]
        out["customers"] = {
            "known": len(cust),
            "repeat_rate": round(len(repeaters) / len(cust), 3),
            "profit_from_repeat_customers": round(sum(sum(vs) for vs in repeaters) / profit, 3) if profit else 0.0,
            "avg_profit_one_time": round(sum(vs[0] for vs in cust.values() if len(vs) == 1) /
                                         max(sum(1 for vs in cust.values() if len(vs) == 1), 1)),
            "avg_profit_repeat_customer": round(sum(sum(vs) for vs in repeaters) / max(len(repeaters), 1)),
        }

    # --- when sales happen
    by_day = defaultdict(float)
    by_hour = defaultdict(float)
    for p, _, v in rows:
        by_day[p.occurred_at.strftime("%A")] += v
        by_hour[p.occurred_at.hour // 3 * 3] += v
    out["best_sales_days"] = [d for d, _ in sorted(by_day.items(), key=lambda kv: -kv[1])[:2]]
    h = max(by_hour, key=by_hour.get)
    out["peak_sales_hours"] = f"{h:02d}:00-{h + 3:02d}:00"

    # --- posts (estimated from timing)
    if posts:
        org = OrganicEngine(engine, posts, now=now)
        results = org.scoreboard()
        linked = sum(r.real_value for r in results)
        out["posts"] = {
            "method": "measured from links/codes" if linked > 0 else
                      "ESTIMATED from sales lift after each post (old posts had no tracking links)",
            "count": len(results),
            "top": [{"post_id": r.post.post_id, "type": f"{r.post.format}/{r.post.hook}/{r.post.offer}",
                     "views": r.post.views, "value_per_1k_views": round(r.value_per_1k)} for r in results[:5]],
            "watchers_only": [r.post.post_id for r in results if "WATCHERS ONLY" in r.tags][:5],
            "patterns": {a: {k: {"lift": round(v["lift"], 2), "confident": v["confident"], "posts": v["posts"]}
                             for k, v in t.items()} for a, t in org.patterns(results).items()},
            "post_next": org.recommend(2),
        }

    out["actions"] = _actions(out)
    return out


def _actions(a: dict) -> list[str]:
    """Plain, specific things to do this week, from the numbers above."""
    acts = []
    cur = a["currency"]
    for c in a.get("discount_codes", []):
        if c["vs_full_price"] <= -0.4:
            acts.append(f"Stop or change code {c['code']}: each order makes {abs(c['vs_full_price']):.0%} less profit "
                        f"than full price ({c['refund_rate']:.0%} refunded).")
    for p in a.get("refund_heavy_products", [])[:2]:
        acts.append(f"Look into '{p['product']}': {p['refund_rate']:.0%} of its sales are refunded.")
    cu = a.get("customers")
    if cu and cu["profit_from_repeat_customers"] >= 0.3:
        acts.append(f"Repeat customers bring {cu['profit_from_repeat_customers']:.0%} of profit "
                    f"({cu['avg_profit_repeat_customer']:,} vs {cu['avg_profit_one_time']:,} {cur} each): "
                    "start a WhatsApp win-back message for past buyers.")
    elif cu and cu["repeat_rate"] < 0.15:
        acts.append(f"Only {cu['repeat_rate']:.0%} of customers buy again: add a follow-up offer after the first order.")
    posts = a.get("posts")
    if posts:
        conf = [(attr, k, v["lift"]) for attr, t in posts["patterns"].items() for k, v in t.items() if v["confident"]]
        for attr, k, lift in sorted(conf, key=lambda x: -x[2])[:1]:
            if lift > 0:
                acts.append(f"Post more {k} ({attr}): {lift:+.0%} money per view vs average (estimated).")
        for attr, k, lift in sorted(conf, key=lambda x: x[2])[:1]:
            if lift < -0.3:
                acts.append(f"Post fewer {k} ({attr}): {lift:+.0%} money per view (estimated).")
        if posts["post_next"]:
            n = posts["post_next"][0]
            acts.append(f"Next post: {n['format']} + {n['hook']} + offer {n['offer']}.")
    acts.append("From today, put a tracking link or code on every post, so next month's numbers are measured, not estimated.")
    acts.append(f"Post around {a['peak_sales_hours']}, when people buy most.")
    return acts


def audit_text(a: dict) -> str:
    if "error" in a:
        return a["error"]
    cur = a["currency"]
    L = [f"FIRST-WEEK AUDIT  {a['period']['from']} to {a['period']['to']} ({a['period']['days']} days of history)",
         "",
         f"Orders {a['orders']:,}   Sales {a['gross']:,} {cur}   Refunds {a['refunds']:,} ({a['refund_rate']:.0%})   "
         f"Real profit {a['real_profit']:,} {cur}"]
    if a.get("discount_codes"):
        L += ["", f"DISCOUNT CODES (full-price order = {a['full_price_profit_per_order']:,} {cur} profit)"]
        for c in a["discount_codes"]:
            L.append(f"  {c['code']:<14}{c['orders']:>5} orders  {c['refund_rate']:>4.0%} refunded  "
                     f"{c['profit_per_order']:>5,} {cur}/order  ({c['vs_full_price']:+.0%})")
    if a.get("refund_heavy_products"):
        L += ["", "PRODUCTS WITH HIGH REFUNDS"]
        L += [f"  {p['product']:<28}{p['refund_rate']:.0%} refunded ({p['units']} sold)" for p in a["refund_heavy_products"]]
    if a.get("customers"):
        c = a["customers"]
        L += ["", f"CUSTOMERS  {c['known']:,} known, {c['repeat_rate']:.0%} came back, "
                  f"bringing {c['profit_from_repeat_customers']:.0%} of profit"]
    if a.get("posts"):
        p = a["posts"]
        L += ["", f"POSTS ({p['count']}) - {p['method']}"]
        L += [f"  {t['post_id']:<16}{t['type']:<32}{t['value_per_1k_views']:>6,} {cur} per 1k views" for t in p["top"]]
        if p["watchers_only"]:
            L.append(f"  Watchers only (views, no buyers): {', '.join(p['watchers_only'])}")
    L += ["", "DO THIS WEEK"] + [f"  {i}. {x}" for i, x in enumerate(a["actions"], 1)]
    return "\n".join(L)


SYSTEM_AUDIT = (
    "You write the opening summary of a first-week audit for a business owner. Use ONLY the audit data. "
    "Five short sentences at most: the single biggest money leak, the single biggest opportunity, and what "
    "to do first. Use the business's currency and real numbers. Anything about posts is an estimate unless the "
    "data says it was measured, so say 'likely' for those. No jargon. Write in {language}."
)


def audit_summary(ai, audit: dict, language: str = "English") -> dict:
    """Muse-written summary of the audit (router task 'chat': the best model)."""
    reply = ai.chat("chat", [
        {"role": "developer", "content": SYSTEM_AUDIT.format(language=language)},
        {"role": "user", "content": json.dumps(audit, ensure_ascii=False, default=str)}],
        max_completion_tokens=8192)
    return {"summary": reply.text, "model": reply.model, "cost_usd": reply.cost_usd}
