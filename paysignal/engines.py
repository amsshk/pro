"""Profit engines.

Zero-Waste Budget Engine: what ONE customer may cost before advertising loses money, judged on real numbers.
Ad Autopsy: for every campaign, where in the journey people drop off, the likely reason and ONE next experiment.

Honesty rules built in:
  - verdicts come with a confidence from a simple statistical model (Poisson), and "too early" is a real answer;
  - nothing is called a winner or loser on a tiny sample;
  - causes are "likely", never certain; Meta's numbers are labelled as Meta's.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from .ads_report import RESULT_TYPES

ACTION_TYPES = {
    "link_clicks": ("link_click",),
    "landing_views": ("landing_page_view", "omni_landing_page_view"),
    "add_to_cart": ("omni_add_to_cart", "offsite_conversion.fb_pixel_add_to_cart", "add_to_cart"),
    "checkout": ("omni_initiated_checkout", "offsite_conversion.fb_pixel_initiate_checkout", "initiate_checkout"),
    "purchase": RESULT_TYPES["purchase"],
    "signup": RESULT_TYPES["signup"],
}
LEARNING_RESULTS_PER_WEEK = 50          # Meta's usual threshold to leave the learning phase


# ---------------------------------------------------------------- unit economics
FIELDS = ("price", "product_cost", "delivery_cost", "payment_fee_pct", "other_costs", "desired_profit",
          "refund_rate", "orders_per_customer", "daily_budget")


def clean_inputs(raw: dict) -> dict:
    out = {}
    for k in FIELDS:
        v = raw.get(k)
        if v in (None, ""):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k.replace('_', ' ')} must be a number")
        if v < 0 or not math.isfinite(v):
            raise ValueError(f"{k.replace('_', ' ')} can't be negative")
        out[k] = v
    if out.get("payment_fee_pct", 0) > 50 or out.get("refund_rate", 0) > 0.95:
        raise ValueError("payment fee % or refund rate looks wrong")
    if "price" not in out or out["price"] <= 0:
        raise ValueError("add the selling price of one sale")
    return out


def economics(inp: dict) -> dict:
    """Max affordable cost per customer, from the business's own numbers."""
    price = inp["price"]
    fee = price * inp.get("payment_fee_pct", 0) / 100
    margin = price - inp.get("product_cost", 0) - inp.get("delivery_cost", 0) - fee - inp.get("other_costs", 0)
    refund = inp.get("refund_rate", 0)
    # a refunded sale returns the price but usually not the costs already paid; assume the margin is lost
    margin_after_refunds = margin * (1 - refund)
    break_even = round(margin_after_refunds, 2)
    target = round(break_even - inp.get("desired_profit", 0), 2)
    out = {"inputs": inp, "margin_per_sale": round(margin, 2), "break_even_cpa": break_even, "target_cpa": target,
           "viable": target > 0}
    opc = inp.get("orders_per_customer") or 0
    if opc > 1:
        out["lifetime_break_even_cpa"] = round(margin_after_refunds * opc, 2)
    if not out["viable"]:
        out["warning"] = ("At this price and cost there is no room to pay for advertising and keep your profit. "
                          "Raise the price, lower costs, or sell a bundle before spending on ads." if break_even <= 0 else
                          "Your desired profit is larger than the whole margin. Lower it, or the ads can never meet it.")
    budget = inp.get("daily_budget") or 0
    if budget and out["viable"]:
        per_week = budget * 7 / target
        out["budget"] = {
            "sales_per_week_at_target": round(per_week, 1),
            "ad_sets": max(1, min(5, int(per_week // LEARNING_RESULTS_PER_WEEK))),
            "days_to_first_verdict": math.ceil(3 * break_even / budget) if break_even > 0 else None,
            "survival_mode": per_week < 10,
        }
        if per_week < 10:
            out["budget"]["rules"] = [
                "One campaign, one ad set, at most 2 ads: splitting a small budget means nothing learns.",
                "Broad targeting in the right country: let Meta find buyers instead of narrowing it.",
                f"Judge only after spending about {fmt(3 * break_even)} (3x your break-even cost per sale).",
                "Pause anything that is 90% sure to be losing; never kill an ad after one bad day.",
                "Push your best organic post as the ad: proven content is the cheapest test there is.",
            ]
    return out


def fmt(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.2f}"


# ---------------------------------------------------------------- statistics
def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam)."""
    if lam <= 0:
        return 1.0
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def verdict(spend: float, sales: float, target: float, break_even: float) -> dict:
    """Is this campaign paying for itself? With how much certainty?
    Model: if the true cost per sale were exactly break-even, sales would be Poisson(spend / break_even)."""
    k = int(round(sales))
    if break_even <= 0:
        return {"verdict": "no_room", "label": "No room for ads", "confidence": None,
                "action": "Fix the price or costs first: every sale from ads loses money."}
    lam = spend / break_even
    cpa = spend / k if k else None
    if k == 0:
        conf = 1 - math.exp(-lam)                        # how unlikely zero sales would be at break-even
        if conf >= 0.9:
            return {"verdict": "losing", "label": "Losing money", "confidence": round(conf, 2), "cpa": None,
                    "action": "Pause it. Spending more is very unlikely to turn it around."}
        need = max(0.0, 3 * break_even - spend)
        return {"verdict": "too_early", "label": "Too early to tell", "confidence": round(conf, 2), "cpa": None,
                "action": f"Keep it running: about {fmt(need)} more spend before a fair verdict." if need else
                          "Keep it running a little longer before judging."}
    if cpa <= target:
        # if it were only break-even we'd expect lam sales; getting k or more would be this unlikely
        conf = poisson_cdf(k - 1, lam)
        return {"verdict": "profitable", "label": "Making money", "confidence": round(conf, 2), "cpa": round(cpa, 2),
                "action": ("Scale it: raise the budget about 20% every 3 days while the cost per sale holds."
                           if conf >= 0.8 else "Looks good. Let it collect a few more sales before scaling.")}
    if cpa <= break_even:
        return {"verdict": "break_even", "label": "Around break-even", "confidence": None, "cpa": round(cpa, 2),
                "action": "Keep it, and test one change (the opening line or the offer) to get under your target."}
    conf = 1 - poisson_cdf(k, lam)                       # getting only k sales if it were break-even
    return {"verdict": "losing" if conf >= 0.9 else "likely_losing",
            "label": "Losing money" if conf >= 0.9 else "Probably losing money", "confidence": round(conf, 2),
            "cpa": round(cpa, 2),
            "action": ("Pause it and move the budget to your best campaign." if conf >= 0.9 else
                       "Fix the biggest drop-off below, or pause it if it doesn't improve this week.")}


# ---------------------------------------------------------------- autopsy
def funnel(row: dict) -> dict:
    acts = {a.get("action_type"): float(a.get("value") or 0) for a in row.get("actions") or []}
    vals = {a.get("action_type"): float(a.get("value") or 0) for a in row.get("action_values") or []}
    out = {}
    for key, names in ACTION_TYPES.items():
        out[key] = next((acts[n] for n in names if n in acts), 0.0)
    out["purchase_value"] = next((vals[n] for n in ACTION_TYPES["purchase"] if n in vals), 0.0)
    num = lambda k: float(row.get(k) or 0)
    out.update(spend=num("spend"), impressions=num("impressions"), clicks=num("clicks"), frequency=num("frequency"),
               cpm=num("cpm"))
    if not out["link_clicks"]:
        out["link_clicks"] = num("inline_link_clicks")
    return out


def autopsy(f: dict, kind: str, econ: Optional[dict] = None, real_ratio: Optional[float] = None) -> dict:
    """Find the weakest step between seeing the ad and paying, with evidence, a likely cause and one experiment."""
    findings = []
    imp, clicks, lpv = f["impressions"], f["link_clicks"], f["landing_views"]
    atc, chk = f["add_to_cart"], f["checkout"]
    goal = f["signup"] if kind == "signup" else f["purchase"]
    pct = lambda a, b: (a / b) if b else None

    def add(stage, severity, evidence, cause, experiment):
        findings.append({"stage": stage, "severity": severity, "evidence": evidence, "likely_cause": cause,
                         "next_experiment": experiment})

    if imp < 1000:
        return {"status": "too_early", "summary": "Not enough people have seen it yet to diagnose anything.",
                "findings": [], "funnel": f, "steps": steps(f, kind)}
    ctr = pct(clicks, imp)
    if ctr is not None and ctr < 0.006:
        add("ad", 3, f"{ctr:.2%} of people who saw it clicked (healthy is about 1%).",
            "The first second doesn't stop the scroll, or the offer isn't clear.",
            "Test a new opening line that names the customer's problem, with the same offer.")
    if f["frequency"] >= 3.5:
        add("audience", 2, f"Each person saw it {f['frequency']:.1f} times on average.",
            "The same people keep seeing it: the audience is tired of this ad.",
            "Add a fresh ad, or widen the audience, before raising the budget.")
    if clicks >= 30 and lpv and lpv / clicks < 0.5:
        add("page_load", 3, f"Only {lpv / clicks:.0%} of clicks reached the page.",
            "The page is slow or the link breaks on phones.",
            "Open the ad's link on a phone with mobile data; fix speed or the link first.")
    if kind == "purchase":
        if lpv >= 50 and atc == 0 and goal == 0:
            add("page", 3, f"{fmt(lpv)} people reached the page and nobody added to cart.",
                "The page doesn't match the ad, the price surprises, or the button is hard to find.",
                "Send the ad straight to the product shown in it, with the price and button near the top.")
        if atc >= 5 and pct(chk, atc) is not None and chk / atc < 0.3:
            add("cart", 2, f"{chk / atc:.0%} of add-to-carts went to checkout.",
                "Delivery cost or delivery time shows up late and puts people off.",
                "Show delivery cost on the product page, or test free delivery above a basket size.")
        if chk >= 5 and goal / chk < 0.3:
            add("checkout", 3, f"{goal / chk:.0%} of checkouts became payments.",
                "A payment method people want is missing (e.g. Apple Pay, cash on delivery) or checkout fails.",
                "Try your own checkout on a phone; add the payment method customers ask for most.")
    else:
        if lpv >= 50 and goal == 0:
            add("page", 3, f"{fmt(lpv)} people reached the page and nobody signed up.",
                "The page asks for too much, or doesn't repeat the ad's promise.",
                "Put the sign-up button and the ad's exact promise at the top of the page.")
    if real_ratio is not None and goal >= 5 and real_ratio < 0.7:
        add("tracking", 2, f"Meta reports {fmt(goal)} results, but only about {real_ratio:.0%} show up as real paid sales.",
            "Meta counts some results that never became real money (duplicates, refunds, view-through).",
            "Trust ProAds' real numbers for decisions; check the Pixel isn't firing twice.")
    if econ and econ.get("viable") is False:
        add("economics", 3, "Your numbers leave no room for an ad cost per sale.",
            "Even perfect ads would lose money at this price and cost.",
            "Test a higher-value offer (bundle or package) before spending more on ads.")
    elif econ and goal and kind == "purchase" and f["spend"] / goal > econ["break_even_cpa"] > 0 and not findings:
        add("economics", 2, f"Each sale costs about {fmt(f['spend'] / goal)}, more than the {fmt(econ['break_even_cpa'])} you can afford.",
            "People buy, but the sale is too small to pay for the ad.",
            "Raise the basket: a bundle or add-on on the product page, then compare cost per sale.")
    findings.sort(key=lambda x: -x["severity"])
    summary = findings[0]["likely_cause"] if findings else "No weak step found: the journey looks healthy."
    return {"status": "diagnosed" if findings else "healthy", "summary": summary, "findings": findings[:3],
            "funnel": f, "steps": steps(f, kind)}


def steps(f: dict, kind: str) -> list[dict]:
    """The journey as a list for the heat-map: each step and how many moved on from the step before."""
    chain = [("Saw the ad", f["impressions"]), ("Clicked", f["link_clicks"]), ("Reached the page", f["landing_views"])]
    if kind == "purchase":
        chain += [("Added to cart", f["add_to_cart"]), ("Checkout", f["checkout"]), ("Paid", f["purchase"])]
    else:
        chain += [("Signed up", f["signup"])]
    out, prev = [], None
    for name, n in chain:
        out.append({"step": name, "count": n, "from_previous": round(n / prev, 4) if prev else None})
        prev = n if n else prev
    return out


class EconomicsStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS unit_economics (client_id TEXT PRIMARY KEY, data TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')))"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def get(self, client_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT data FROM unit_economics WHERE client_id=?", (client_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def save(self, client_id: str, inputs: dict) -> None:
        self.db.execute("INSERT INTO unit_economics (client_id, data) VALUES (?,?) ON CONFLICT(client_id) DO UPDATE"
                        " SET data=excluded.data, updated_at=datetime('now')", (client_id, json.dumps(inputs)))
        self.db.commit()
