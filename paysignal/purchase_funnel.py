"""Purchase Funnel Optimiser: where prospects drop out between the ad and a verified payment, and what to test next.

One engine for every business; only the stages differ.
  ProAds itself  ad clicks -> landing visits -> sign-ups -> active -> checkout started -> plan started -> first payment
  shops          ad clicks -> landing views  -> add to cart -> checkout -> verified purchases

Rules:
  * Stage counts are the real ones. Each says where it comes from (Meta, ProAds, Stripe) and from when it was counted.
  * A step is only called weak when the evidence says so: the upper end of its 90% range must sit below the reference.
  * The reference is a rate PROVEN in this account (another campaign) when one exists, otherwise a rough rule of thumb,
    and it is always labelled which.
  * The biggest opportunity = the most extra verified payments per period if that one step reached its reference,
    with every other step as it is now.
"""
from __future__ import annotations

import math
from typing import Optional

MIN_TOP = 30            # people at the top of a step before it is judged at all
MIN_PROVEN = 100        # people at the top of a step in another campaign before its rate counts as "proven here"
Z90 = 1.645

# (key, label) in order, then rule-of-thumb rate for each step INTO that stage (rough; always labelled)
SAAS_STAGES = [("clicks", "Ad clicks"), ("visits", "Landing visits"), ("signups", "Sign-ups"),
               ("active", "Active (brought in data)"), ("checkout", "Checkout started"),
               ("plan", "Plan started (card added)"), ("paid", "First payment collected")]
SAAS_RULES = {"visits": 0.75, "signups": 0.08, "active": 0.40, "checkout": 0.30, "plan": 0.60, "paid": 0.85}

SHOP_STAGES = [("clicks", "Ad clicks"), ("visits", "Landing page views"), ("cart", "Added to cart"),
               ("checkout", "Checkout started"), ("paid", "Verified purchases")]
SHOP_RULES = {"visits": 0.75, "cart": 0.08, "checkout": 0.45, "paid": 0.55}

# what to test when the step INTO this stage is the weak one
EXPERIMENTS = {
    "visits": {"title": "Make the page open before people give up",
               "change": "Serve a lighter landing page on phones (smaller images, no heavy scripts above the fold).",
               "keep": "Same ads, audience, budget and offer.", "where": "site",
               "why": "People click but never see the page: it loads too slowly or the link breaks in Meta's in-app browser."},
    "signups": {"title": "Repeat the ad's promise in the first screen",
                "change": "Test a headline and first line that say exactly what the ad promised, with one sign-up button above the fold.",
                "keep": "Same ads, audience, budget, price and offer.", "where": "site",
                "why": "Visitors arrive but don't sign up: the page doesn't match the ad, or it asks too much too soon."},
    "cart": {"title": "Show the product the ad showed",
             "change": "Send each ad to the product it shows, with price, delivery time and returns visible without scrolling.",
             "keep": "Same ads, audience and budget.", "where": "site",
             "why": "Visitors arrive but don't add to cart: wrong landing page, missing price or delivery details."},
    "active": {"title": "Get new sign-ups to their first result",
               "change": "Test a first-day guide: one step to connect or upload, then their first audit result shown right away.",
               "keep": "Same ads, landing page and price.", "where": "product",
               "why": "People sign up but never bring in data, so they never see what ProAds finds for them."},
    "checkout": {"title": "Show the value before asking for a plan",
                 "change": "When someone's first audit is ready, show what it found (in their own money) next to the plan button.",
                 "keep": "Same ads, landing page and price.", "where": "product",
                 "why": "Active users don't start a plan: they haven't yet seen a result worth paying for."},
    "plan": {"title": "Remove surprises at checkout",
             "change": "Show the full price with VAT, what happens after the trial, and how to cancel before the payment page.",
             "keep": "Same ads, landing page, price and trial.", "where": "site",
             "why": "People open the payment page and leave: unexpected price, tax or commitment."},
    "paid": {"title": "Make the first payment go through",
             "change": "Remind people before the trial ends, and retry failed cards with a clear update-card link.",
             "keep": "Same ads, landing page, price and trial.", "where": "billing",
             "why": "Plans start but the first payment isn't collected: trial cancellations or failed cards."},
}
SHOP_EXPERIMENTS = {**EXPERIMENTS,
                    "checkout": {"title": "Make the cart lead to checkout",
                                 "change": "Show delivery cost and time in the cart, and offer express checkout (Apple Pay, Google Pay).",
                                 "keep": "Same ads, audience, budget and prices.", "where": "site",
                                 "why": "People add to cart but don't check out: surprise delivery costs or a long form."},
                    "paid": {"title": "Remove friction from payment",
                             "change": "Cut the checkout form to the essentials and show trusted payment options early.",
                             "keep": "Same ads, audience, budget and prices.", "where": "site",
                             "why": "People start checkout but don't pay: long forms, few payment options, or failed payments."}}
AD_EXPERIMENT = {"title": "Get more of the right people to click",
                 "change": "Test a new opening line that names the customer's problem, with the same offer.",
                 "keep": "Same audience, budget and landing page.", "where": "meta",
                 "why": "Too few people click to fill the funnel."}


def wilson(k: float, n: float, z: float = Z90) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = min(1.0, k / n)
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - w) / d), min(1.0, (c + w) / d)


def per_arm(p: float, lift: float = 0.3) -> int:
    """People per version to see a relative lift in this step's rate (two proportions, 95% / 80% power)."""
    p2 = min(0.99, p * (1 + lift))
    if p <= 0 or p2 <= p:
        return 0
    return math.ceil((1.96 + 0.84) ** 2 * (p * (1 - p) + p2 * (1 - p2)) / (p2 - p) ** 2)


def analyse(stages: list[dict], kind: str = "saas", peers: Optional[list[list[dict]]] = None,
            period_days: int = 30, value_each: Optional[float] = None, currency: str = "",
            unit: Optional[str] = None) -> dict:
    """stages: [{key, label, count, source, partial?}] in order (a stage with count None is unknown and skipped).
    peers: the same stage lists for other campaigns, to find rates proven in this account."""
    rules = SAAS_RULES if kind == "saas" else SHOP_RULES
    exps = EXPERIMENTS if kind == "saas" else SHOP_EXPERIMENTS
    known = [s for s in stages if s.get("count") is not None]
    steps = []
    for a, b in zip(known, known[1:]):
        n, k = float(a["count"]), float(b["count"])
        mature = b.get("mature")                      # (top, reached) for people who have had time to get through
        if mature:
            n, k = float(mature[0]), float(mature[1])
        k = min(k, n) if n else k
        rate = (k / n) if n else None
        lo, hi = wilson(k, n)
        ref, ref_src = rules.get(b["key"]), "rule of thumb"
        proven = []                                   # a rate another campaign here has shown (its cautious lower end)
        for peer in peers or []:
            pa = next((s for s in peer if s["key"] == a["key"] and s.get("count") is not None), None)
            pb = next((s for s in peer if s["key"] == b["key"] and s.get("count") is not None), None)
            if pa and pb and pa["count"] >= MIN_PROVEN and not pa.get("partial") and not pb.get("partial"):
                proven.append((wilson(min(pb["count"], pa["count"]), pa["count"])[0], pa.get("campaign") or "another campaign"))
        if proven:
            plo, who = max(proven)
            if plo > 0:
                ref, ref_src = plo, f"proven here by {who}"
        judged = n >= MIN_TOP and not a.get("partial") and not b.get("partial")
        weak = bool(judged and ref is not None and hi < ref)
        steps.append({"from": a["key"], "to": b["key"], "label": f"{a['label']} → {b['label']}", "top": int(n), "reached": int(k),
                      "rate": round(rate, 4) if rate is not None else None, "range": [round(lo, 4), round(hi, 4)],
                      "reference": round(ref, 4) if ref is not None else None, "reference_source": ref_src,
                      "judged": judged, "weak": weak, "partial": bool(a.get("partial") or b.get("partial")),
                      "mature_note": b.get("mature_note") if mature else None})
    # extra final-stage results if one step reached its reference, the others unchanged
    def downstream(i):
        m = 1.0
        for s in steps[i + 1:]:
            r = s["rate"] if s["judged"] and s["rate"] is not None else s["reference"]
            m *= r if r is not None else 0.0
        return m
    for i, s in enumerate(steps):
        gain = 0.0
        if s["reference"] is not None and s["rate"] is not None and s["reference"] > s["rate"]:
            gain = s["top"] * (s["reference"] - s["rate"]) * downstream(i)
        s["extra_results"] = round(gain, 1)
        s["extra_value"] = round(gain * value_each, 2) if value_each else None
    final = known[-1] if known else None
    weak = sorted([s for s in steps if s["weak"]], key=lambda s: -s["extra_results"])
    out = {"kind": kind, "period_days": period_days, "currency": currency, "value_each": value_each,
           "stages": stages, "steps": steps, "final": final and {"label": final["label"], "count": final["count"]}}
    top = known[0]["count"] if known else 0
    if not known or top == 0:
        out.update(status="no_data", summary="No ad clicks in this period yet, so there's no funnel to read.")
        return out
    if not weak:
        unjudged = [s for s in steps if not s["judged"]]
        if unjudged:
            s = unjudged[0]
            need = max(0, MIN_TOP - s["top"])
            out.update(status="too_early", summary=(
                f"Not enough people yet to judge '{s['label']}'" + (f": about {need} more needed." if need else
                                                                       " (counting started recently, so it's partial).")),
                       opportunity=None, next_experiment=None)
        else:
            out.update(status="healthy", summary="No step is clearly below its reference. Grow the top of the funnel next.",
                       opportunity=None, next_experiment={**AD_EXPERIMENT, **(_sizing(steps[0], period_days) if steps else {})})
        return out
    best = weak[0]
    exp = dict(exps.get(best["to"], AD_EXPERIMENT))
    exp.update(_sizing(best, period_days))
    out.update(status="opportunity", opportunity=best, next_experiment=exp,
               summary=(f"Biggest opportunity: {best['label']}. {best['rate']:.1%} get through "
                        f"(reference {best['reference']:.1%}, {best['reference_source']}). Reaching it would bring about "
                        f"{best['extra_results']:g} more {unit or final['label'].lower()} every {period_days} days"
                        + (f", about {best['extra_value']:,.0f} {currency}." if best.get("extra_value") else ".")))
    return out


def _sizing(step: dict, period_days: int) -> dict:
    n = per_arm(step["rate"] or step["reference"] or 0.1)
    per_day = step["top"] / max(1, period_days)
    days = math.ceil(2 * n / per_day) if per_day > 0 and n else None
    return {"measure": f"{step['label']} rate, and verified payments", "people_per_version": n,
            "days_needed": days, "metric_step": step["to"],
            "success_when": "The new version's rate is clearly higher (95% confidence), and verified payments don't fall."}


class FunnelStore:
    """First-party funnel counts for ProAds' own site. Visits are counted per day and campaign only (no identifiers)."""
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS funnel_marks (client_id TEXT NOT NULL, stage TEXT NOT NULL, at INTEGER NOT NULL,
        PRIMARY KEY (client_id, stage));
    CREATE TABLE IF NOT EXISTS landing_visits (day TEXT NOT NULL, campaign TEXT NOT NULL, source TEXT NOT NULL,
        medium TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, campaign, source, medium));
    CREATE TABLE IF NOT EXISTS funnel_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """

    def __init__(self, db, now: Optional[int] = None):
        import time
        self.db = db
        self.db.executescript(self.SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO funnel_meta (key, value) VALUES ('started_at', ?)", (str(int(now or time.time())),))
        self.db.commit()

    def started_at(self) -> int:
        return int(self.db.execute("SELECT value FROM funnel_meta WHERE key='started_at'").fetchone()[0])

    def set_started_at(self, ts: int) -> None:            # tests
        self.db.execute("UPDATE funnel_meta SET value=? WHERE key='started_at'", (str(int(ts)),))
        self.db.commit()

    def mark(self, client_id: str, stage: str, at: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO funnel_marks (client_id, stage, at) VALUES (?,?,?)", (client_id, stage, int(at)))
        self.db.commit()

    def marks(self) -> dict:
        out: dict = {}
        for cid, stage, at in self.db.execute("SELECT client_id, stage, at FROM funnel_marks"):
            out.setdefault(cid, {})[stage] = at
        return out

    def first_mark(self, stage: str) -> Optional[int]:
        r = self.db.execute("SELECT MIN(at) FROM funnel_marks WHERE stage=?", (stage,)).fetchone()
        return r[0] if r and r[0] else None

    def visit(self, day: str, campaign: str, source: str, medium: str) -> None:
        self.db.execute("INSERT INTO landing_visits (day, campaign, source, medium, count) VALUES (?,?,?,?,1)"
                        " ON CONFLICT(day, campaign, source, medium) DO UPDATE SET count=count+1",
                        (day, campaign[:80], source[:40], medium[:40]))
        self.db.commit()

    def visits(self, since_day: str) -> list[tuple]:
        return self.db.execute("SELECT campaign, source, medium, SUM(count) FROM landing_visits WHERE day>=?"
                               " GROUP BY campaign, source, medium", (since_day,)).fetchall()

    def first_visit_day(self) -> Optional[str]:
        r = self.db.execute("SELECT MIN(day) FROM landing_visits").fetchone()
        return r[0] if r else None
