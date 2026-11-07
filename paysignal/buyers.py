"""Buyer Quality Intelligence.

Question it answers: "Based on the customers who actually made this business money, what should we test next
on Meta to acquire more customers like them?"

These are PATTERNS among existing customers, not proof of cause: who bought a product first may differ from
who didn't in many ways. Findings only become hypotheses; a randomized test on Meta is what shows cause.

Rules (built in, not optional):
  - FACTS ONLY for buyer value: money actually paid minus refunds (and minus product cost where the business gave
    costs). Repeat purchases are counted only when they happened. No predicted lifetime value anywhere here.
  - Fair comparison: every customer is judged on the same window after their first purchase (e.g. 60 days), and
    only customers old enough to have had that whole window count.
  - Minimum evidence: a product, offer, channel or ad needs MIN_CUSTOMERS customers before it is compared, and a
    difference is only called real when its 95% interval excludes zero. Otherwise: "no clear difference".
  - Experiments are one change at a time, against a control, with a spend floor before any verdict and a time cap.
  - Every hypothesis, change, result and decision is kept, and rejected ideas are not suggested again.
  - Meta-native: the test runs as two normal Meta campaigns (or Meta's own A/B test); ProAds decides from real
    paid purchases, not Meta-reported conversions.
"""
from __future__ import annotations

import json
import math
import random
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .engine import Engine
from .identity import customer_key

MIN_CUSTOMERS = 15          # per segment before comparing
MIN_MATURE = 40             # customers with a full window before anything is shown
WINDOWS = (60, 30)
BEST_SHARE = 0.2            # "best buyers" = top 20% by value in the window
DIMENSIONS = {"product": "First product bought", "offer": "First offer", "channel": "Where they came from",
              "campaign": "Meta campaign", "creative": "Meta ad"}
META_SOURCES = ("meta", "facebook", "instagram", "fb", "ig")


def _offer(p) -> str:
    return f"Code {p.promo_code.upper()}" if p.promo_code else "Full price"


def _channel(p, promo_posts: dict) -> str:
    src, med = (p.utm_source or "").lower(), (p.utm_medium or "").lower()
    if med == "paid" and src in META_SOURCES:
        return "Meta ads"
    if med == "paid" and src:
        return f"{src.title()} ads"
    if src == "whatsapp" or (p.utm_content or "").startswith("wa:"):
        return "WhatsApp"
    if src:
        return f"{src.title()} posts"
    if p.promo_code and p.promo_code.upper() in promo_posts:
        return f"{promo_posts[p.promo_code.upper()].title()} posts"
    return "Direct / unknown"


def _product(p) -> Optional[str]:
    if not p.items:
        return None
    top = max(p.items, key=lambda i: i.unit_price * i.quantity)
    return top.name or top.sku or None


def build_customers(engine: Engine, promo_posts: dict, campaign_names: dict) -> tuple[list[dict], str, Optional[date]]:
    """One row per known customer with FACT-based value. Returns (customers, value_basis, last_day)."""
    cid = engine.client_id
    by: dict[str, list] = defaultdict(list)
    costs_known = total = 0
    last_day = None
    for p in engine.store.purchases(cid):
        key = customer_key(p.email, p.phone, p.customer_id)
        if not key:
            continue
        ref = min(engine.store.refunded_amount(cid, p.external_id), p.amount)
        cogs = p.cost_of_goods()
        total += 1
        costs_known += cogs is not None
        by[key].append((p, ref, cogs))
        d = p.occurred_at.date()
        last_day = d if last_day is None or d > last_day else last_day
    use_costs = total and costs_known / total >= 0.8
    basis = "net of refunds and product costs" if use_costs else "net of refunds (no product costs given)"
    out = []
    for key, orders in by.items():
        orders.sort(key=lambda x: x[0].occurred_at)
        first = orders[0][0]
        camp = (first.utm_campaign or "") if (first.utm_medium or "").lower() == "paid" else ""
        out.append({
            "key": key, "first": first.occurred_at.date(),
            "orders": [(o.occurred_at.date(), o.amount, r, (c if (use_costs and c is not None) else 0.0))
                       for o, r, c in orders],
            "product": _product(first), "offer": _offer(first), "channel": _channel(first, promo_posts),
            "campaign": campaign_names.get(camp, camp) or None,
            "creative": (first.utm_content or None) if camp else None,
        })
    return out, basis, last_day


def window_value(c: dict, days: int) -> dict:
    end = c["first"] + timedelta(days=days - 1)
    inside = [o for o in c["orders"] if o[0] <= end]
    gross = sum(o[1] for o in inside)
    refunds = sum(o[2] for o in inside)
    return {"value": gross - refunds - sum(o[3] for o in inside), "orders": len(inside), "gross": gross, "refunds": refunds}


BOOT = 2000


def boot_diff(a: list[float], b: list[float], n: int = BOOT, seed: int = 7) -> tuple[float, float, float]:
    """Customer-level bootstrap of mean(a) - mean(b): returns (2.5%, 97.5%, share of resamples above zero).
    Resampling whole customers keeps skewed values (a few big spenders) from faking certainty."""
    import numpy as np
    rng = np.random.default_rng(seed)
    xa, xb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    diffs = []
    for chunk in range(0, n, 250):                      # chunks keep memory small for big shops
        k = min(250, n - chunk)
        ma = xa[rng.integers(0, len(xa), size=(k, len(xa)))].mean(axis=1)
        mb = xb[rng.integers(0, len(xb), size=(k, len(xb)))].mean(axis=1)
        diffs.append(ma - mb)
    d = np.sort(np.concatenate(diffs))
    return float(d[int(0.025 * n)]), float(d[int(0.975 * n) - 1]), float((d > 0).mean())


def _stats(xs: list[float]) -> tuple[float, float]:
    m = statistics.fmean(xs)
    sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
    return m, sd


def analyse(customers: list[dict], last_day: Optional[date], currency: str) -> dict:
    if not customers or last_day is None:
        return {"ready": False, "needs": "Upload sales that include the customer's email or phone, so purchases can be linked to the same buyer."}
    window = next((w for w in WINDOWS if sum(1 for c in customers if c["first"] <= last_day - timedelta(days=w - 1)) >= MIN_MATURE), None)
    if window is None:
        mature30 = sum(1 for c in customers if c["first"] <= last_day - timedelta(days=29))
        return {"ready": False, "needs": f"Needs at least {MIN_MATURE} customers whose first purchase was 30+ days ago "
                                         f"(you have {mature30}). Keep the history coming in.",
                "customers": len(customers)}
    rows = []
    for c in customers:
        if c["first"] <= last_day - timedelta(days=window - 1):
            rows.append({**c, **window_value(c, window)})
    vals = [r["value"] for r in rows]
    mean_all, _ = _stats(vals)
    cut = sorted(vals, reverse=True)[max(0, int(len(vals) * BEST_SHARE) - 1)]
    best = [r for r in rows if r["value"] >= cut]
    out = {"ready": True, "currency": currency, "window_days": window, "customers": len(rows),
           "avg_value": round(mean_all, 2), "repeat_rate": round(sum(r["orders"] > 1 for r in rows) / len(rows), 3),
           "best": {"count": len(best), "avg_value": round(statistics.fmean([r["value"] for r in best]), 2),
                    "repeat_rate": round(sum(r["orders"] > 1 for r in best) / len(best), 3),
                    "share_of_value": round(sum(r["value"] for r in best) / sum(vals), 3) if sum(vals) > 0 else None},
           "dimensions": []}
    for dim, label in DIMENSIONS.items():
        groups = defaultdict(list)
        for r in rows:
            if r[dim]:
                groups[r[dim]].append(r)
        if len(groups) < 2 and dim not in ("campaign", "creative"):
            continue
        segs = []
        for name, members in groups.items():
            if len(members) < MIN_CUSTOMERS:
                continue
            rest = [r["value"] for r in rows if r[dim] != name]
            if len(rest) < MIN_CUSTOMERS:
                continue
            mine = [r["value"] for r in members]
            m, _ = _stats(mine)
            mr, _ = _stats(rest)
            diff = m - mr
            lo, hi, _ = boot_diff(mine, rest)
            # a higher average alone is never enough: the whole 95% range must be on one side of zero
            verdict = "better" if lo > 0 else "worse" if hi < 0 else "unclear"
            gross = sum(r["gross"] for r in members)
            in_best = sum(1 for r in members if r["value"] >= cut)
            segs.append({"name": name, "customers": len(members), "avg_value": round(m, 2),
                         "vs_rest": round(diff, 2), "lift": round(m / mr - 1, 3) if mr > 0 else None,
                         "median_value": round(statistics.median(mine), 2),
                         "interval": [round(lo, 2), round(hi, 2)], "verdict": verdict,
                         "repeat_rate": round(sum(r["orders"] > 1 for r in members) / len(members), 3),
                         "refund_rate": round(sum(r["refunds"] for r in members) / gross, 3) if gross else 0.0,
                         "share_of_best": round(in_best / len(best), 3), "share_of_all": round(len(members) / len(rows), 3)})
        small = sum(1 for n, m in groups.items() if len(m) < MIN_CUSTOMERS)
        if segs or small:
            out["dimensions"].append({"key": dim, "label": label, "segments": sorted(segs, key=lambda s: -s["avg_value"]),
                                      "too_small": small})
    return out


def suggest(analysis: dict, history: list[dict], economics: Optional[dict], daily_budget: Optional[float]) -> list[dict]:
    """Experiments from CONFIDENT findings only; never re-suggest a hypothesis that was already tried and rejected."""
    if not analysis.get("ready"):
        return []
    tried = {(h["dimension"], h["segment"]): h for h in history}
    cur = analysis["currency"]
    be = (economics or {}).get("break_even_cpa") or None
    out = []
    for d in analysis["dimensions"]:
        for s in d["segments"]:
            if s["verdict"] != "better":
                continue
            prev = tried.get((d["key"], s["name"]))
            if prev and (prev.get("decision") == "reject" or prev.get("status") in ("planned", "running")):
                continue                                  # rejected ideas never come back; open ones are listed below
            if d["key"] == "product":
                hyp = f"Ads that feature {s['name']} bring more valuable buyers than our current ads."
                change = f"New ad creative built around {s['name']} (same audience, budget and offer as the control)."
            elif d["key"] == "offer":
                hyp = (f"Ads at full price bring more valuable buyers than discount-led ads." if s["name"] == "Full price"
                       else f"Leading with {s['name']} brings more valuable buyers.")
                change = ("Remove the discount from the ad; keep everything else the same." if s["name"] == "Full price"
                          else f"Put {s['name']} in the ad's headline; keep everything else the same.")
            elif d["key"] == "channel":
                hyp = f"Turning what works in {s['name']} into ads brings more valuable buyers."
                change = f"New ad copied from the style of your best {s['name']} (hook and format), same audience and budget."
            elif d["key"] in ("campaign", "creative"):
                hyp = f"More of the angle in {s['name']} brings more valuable buyers."
                change = f"A fresh variation of {s['name']} (new image, same message) against the current best ad."
            else:
                continue
            min_spend = round(3 * be, 2) if be else None
            out.append({
                "dimension": d["key"], "segment": s["name"], "hypothesis": hyp, "change": change,
                "evidence": {"customers": s["customers"], "avg_value": s["avg_value"], "vs_rest": s["vs_rest"],
                             "lift": s["lift"], "interval": s["interval"], "window_days": analysis["window_days"],
                             "currency": cur, "repeat_rate": s["repeat_rate"], "refund_rate": s["refund_rate"]},
                "metric": f"Real paid purchases and net value per {cur} spent (after refunds), per arm",
                "design": {"arms": ["Control: your current best ad", "Variant: the change above"],
                           "split": "Equal daily budget per arm (Meta A/B test, or two campaigns)",
                           "daily_budget_per_arm": round((daily_budget or 0) / 2, 2) or None,
                           "min_spend_per_arm": min_spend, "max_days": 14},
                "tried_before": bool(prev),
            })
    out.sort(key=lambda x: -(x["evidence"]["lift"] or 0))
    return out[:5]


def value_per_spend_diff(c: dict, v: dict, n: int = BOOT, seed: int = 11) -> Optional[tuple[float, float, float]]:
    """Bootstrap of (variant - control) net value per unit spent. Each resample redraws HOW MANY purchases each
    arm got (Poisson) and WHICH ones (customer-level), so a single big order can't decide the test."""
    import numpy as np
    if c["spend"] <= 0 or v["spend"] <= 0 or not (c["values"] or v["values"]):
        return None
    rng = np.random.default_rng(seed)

    def draws(arm):
        vals = np.asarray(arm["values"] or [0.0], dtype=float)
        k = rng.poisson(len(arm["values"]), size=n)
        tot = np.array([vals[rng.integers(0, len(vals), size=x)].sum() if x and arm["values"] else 0.0 for x in k])
        return tot / arm["spend"]
    d = np.sort(draws(v) - draws(c))
    return float(d[int(0.025 * n)]), float(d[int(0.975 * n) - 1]), float((d > 0).mean())


def evaluate(prelim: dict, settled: dict, min_spend: Optional[float], started_at: int, max_days: int = 14,
             settle_days: int = 14, ended_at: Optional[int] = None) -> dict:
    """prelim / settled: {"control": {"spend", "values": [net value per real purchase]}, "variant": {...}}.
    Verdicts come only from SETTLED results (purchases old enough for refunds to have arrived). A verdict needs
    the whole 95% range of the difference on one side of zero; time running out never forces one."""
    floor = min_spend or 0
    days = (time.time() - started_at) / 86400

    def view(arms):
        c, v = arms["control"], arms["variant"]
        out = {"control": {"spend": round(c["spend"], 2), "purchases": len(c["values"]), "value": round(sum(c["values"]), 2)},
               "variant": {"spend": round(v["spend"], 2), "purchases": len(v["values"]), "value": round(sum(v["values"]), 2)}}
        if min(c["spend"], v["spend"]) < floor or c["spend"] <= 0 or v["spend"] <= 0:
            return {**out, "enough_spend": False, "verdict": None}
        r = value_per_spend_diff(c, v)
        if r is None:
            return {**out, "enough_spend": True, "verdict": None}
        lo, hi, p = r
        verdict = "keep" if lo > 0 else "reject" if hi < 0 else None
        return {**out, "enough_spend": True, "interval": [round(lo, 4), round(hi, 4)], "chance_variant_better": round(p, 3),
                "verdict": verdict}
    pv, sv = view(prelim), view(settled)
    res = {"preliminary": pv, "settled": sv, "settle_days": settle_days, "days_running": round(days, 1)}
    if sv["verdict"]:
        why = ("The variant brought more real net value per spend, beyond the 95% range of chance."
               if sv["verdict"] == "keep" else "The control brought more real net value per spend, beyond the 95% range of chance.")
        return {**res, "status": "evidence", "suggested": sv["verdict"], "why": why}
    if pv["verdict"]:
        return {**res, "status": "provisional", "suggested": None,
                "why": f"Early results lean to '{pv['verdict']}', but recent purchases can still be refunded. "
                       f"ProAds waits {settle_days} days after each purchase before trusting it."}
    if not pv["enough_spend"]:
        why = (f"Each side needs at least {floor:,.0f} spend before any judgement." if floor else "Both sides need spend first.")
    else:
        why = "No clear difference yet: the 95% range still includes zero."
    if days > max_days:
        why += f" It's past the planned {max_days} days, so it stays inconclusive so far: keep collecting, or stop and record it as inconclusive."
    return {**res, "status": "collecting", "suggested": None, "why": why}


AUDIENCE_KEYS = ("geo_locations", "age_min", "age_max", "genders", "custom_audiences", "excluded_custom_audiences",
                 "flexible_spec", "locales")
PLACEMENT_KEYS = ("publisher_platforms", "facebook_positions", "instagram_positions", "audience_network_positions",
                  "messenger_positions", "device_platforms")


def control_checks(control: list[dict], variant: list[dict], split_test_id: str = "") -> list[dict]:
    """Is it a fair test? Compares the two campaigns' ad sets as Meta has them."""
    def one(sets, f):
        vals = {json.dumps(f(a), sort_keys=True) for a in sets}
        return vals.pop() if len(vals) == 1 else ("mixed:" + "|".join(sorted(vals)))
    pick = lambda a, keys: {k: a.get("targeting", {}).get(k) for k in keys if a.get("targeting", {}).get(k) is not None}
    rows = []

    def check(key, label, f, fix):
        a, b = one(control, f), one(variant, f)
        rows.append({"key": key, "label": label, "ok": a == b, "fix": None if a == b else fix})
    check("event", "Same optimisation event",
          lambda a: [a.get("optimization_goal"), (a.get("promoted_object") or {}).get("custom_event_type")],
          "Both must optimise for the same event (e.g. Purchase), or results aren't comparable.")
    check("attribution", "Same attribution setting", lambda a: a.get("attribution_spec"),
          "Set the same attribution window on both.")
    check("audience", "Same audience", lambda a: pick(a, AUDIENCE_KEYS), "Use the same countries, ages and audiences.")
    check("placements", "Same placements", lambda a: pick(a, PLACEMENT_KEYS), "Use the same placements on both.")
    bc = sum(float(a.get("daily_budget") or 0) for a in control)
    bv = sum(float(a.get("daily_budget") or 0) for a in variant)
    same_budget = bool(bc and bv and abs(bc - bv) / max(bc, bv) <= 0.10)
    rows.append({"key": "budget", "label": "Equal budgets (within 10%)", "ok": same_budget,
                 "fix": None if same_budget else "Give both the same daily budget."})
    rows.append({"key": "split", "label": "People split between the two (no overlap)", "ok": bool(split_test_id),
                 "warning_only": True,
                 "fix": None if split_test_id else "Without Meta's A/B test, the same person can see both ads. Create the test in "
                                                   "Ads Manager → Experiments → A/B test and paste its ID for a clean split."})
    return rows


class ExperimentStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS experiments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, created_at INTEGER NOT NULL,
        dimension TEXT NOT NULL, segment TEXT NOT NULL, hypothesis TEXT NOT NULL, change TEXT NOT NULL,
        evidence TEXT NOT NULL, design TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
        control_campaign TEXT, variant_campaign TEXT, started_at INTEGER, result TEXT,
        decision TEXT, decided_at INTEGER, note TEXT)"""
    COLS = ("id", "client_id", "created_at", "dimension", "segment", "hypothesis", "change", "evidence", "design",
            "status", "control_campaign", "variant_campaign", "started_at", "result", "decision", "decided_at", "note",
            "checks", "split_test_id")

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(experiments)").fetchall()}
        for col in ("checks", "split_test_id"):
            if col not in cols:
                self.db.execute(f"ALTER TABLE experiments ADD COLUMN {col} TEXT")
        self.db.commit()

    def add(self, client_id: str, s: dict) -> int:
        cur = self.db.execute("INSERT INTO experiments (client_id, created_at, dimension, segment, hypothesis, change,"
                              " evidence, design) VALUES (?,?,?,?,?,?,?,?)",
                              (client_id, int(time.time()), s["dimension"], s["segment"], s["hypothesis"], s["change"],
                               json.dumps(s["evidence"]), json.dumps(s["design"])))
        self.db.commit()
        return cur.lastrowid

    def list(self, client_id: str) -> list[dict]:
        rows = self.db.execute(f"SELECT {','.join(self.COLS)} FROM experiments WHERE client_id=? ORDER BY id DESC",
                               (client_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(zip(self.COLS, r))
            for k in ("evidence", "design", "result", "checks"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return out

    def get(self, client_id: str, eid: int) -> Optional[dict]:
        return next((e for e in self.list(client_id) if e["id"] == eid), None)

    def start(self, client_id: str, eid: int, control: str, variant: str, checks: list, split_test_id: str = "") -> None:
        self.db.execute("UPDATE experiments SET status='running', control_campaign=?, variant_campaign=?, started_at=?,"
                        " checks=?, split_test_id=? WHERE id=? AND client_id=? AND status IN ('planned','running')",
                        (control, variant, int(time.time()), json.dumps(checks), split_test_id or None, eid, client_id))
        self.db.commit()

    def save_result(self, client_id: str, eid: int, result: dict) -> None:
        self.db.execute("UPDATE experiments SET result=? WHERE id=? AND client_id=?", (json.dumps(result), eid, client_id))
        self.db.commit()

    def decide(self, client_id: str, eid: int, decision: str, note: str = "") -> None:
        self.db.execute("UPDATE experiments SET status='decided', decision=?, decided_at=?, note=? WHERE id=? AND client_id=?",
                        (decision, int(time.time()), note[:500], eid, client_id))
        self.db.commit()

    def forget(self, client_id: str) -> None:
        self.db.execute("DELETE FROM experiments WHERE client_id=?", (client_id,))
        self.db.commit()
