"""Where a new TikTok setup goes: default to consolidation, not campaign creation.

New campaign only for campaign-level separation (another market, or the campaign cap for the budget is not reached).
Anything else (a new audience, creative angle, bid or budget test) is built as an ad group inside a campaign that
already exists. Small budgets get fewer campaigns, never more.
"""
from __future__ import annotations

import math
import re
from typing import Optional

LIVE = ("ACTIVE", "PAUSED")


def campaign_cap(total_daily_budget: float) -> int:
    """How many campaigns this much daily budget can feed without splitting it too thin."""
    b = float(total_daily_budget or 0)
    if b <= 75:
        return 1
    if b <= 150:
        return 2
    if b <= 300:
        return 3
    return min(4, math.floor(b / 75))


def eligible(existing: list[dict]) -> list[dict]:
    return [c for c in existing if c.get("campaign_id") and c.get("status") in LIVE]


def norm(x: Optional[str]) -> str:
    return re.sub(r"[^\w]+", " ", str(x or "").lower()).strip()


def decide(countries: list[str], budget: float, existing: list[dict], product_focus: str = "", offer: str = "",
           force_new: bool = False) -> dict:
    """{"action": "create_adgroup_in_existing_campaign" | "create_new_campaign", "campaign": {...} | None, "reason": str, "cap": int, "total_budget": float}
    existing: [{campaign_id, name, status, countries, daily_budget}] made with ProAds in this ad account."""
    pool = eligible(existing)
    live_now = [c for c in pool if c["status"] == "ACTIVE"]
    total = float(budget or 0) + sum(float(c.get("daily_budget") or 0) for c in live_now)
    cap = campaign_cap(total)
    want = sorted(c.upper() for c in countries or ["AE"])
    same_market = [c for c in pool if sorted(x.upper() for x in c.get("countries") or ["AE"]) == want]
    prod, off = norm(product_focus), norm(offer) or "none"
    # same campaign only when goal, market, product line AND offer all match (an older campaign with no product recorded never matches)
    same = [c for c in same_market if prod and norm(c.get("product_focus")) == prod and (norm(c.get("offer")) or "none") == off]

    def newest(items):
        return sorted(items, key=lambda c: (c["status"] == "ACTIVE", c.get("created_at") or 0), reverse=True)[0]

    base = {"cap": cap, "total_budget": round(total, 2)}
    if force_new:
        return {**base, "action": "create_new_campaign", "campaign": None, "reason": "New campaign because you asked for a separate one (campaign-level separation)."}
    if same:
        c = newest(same)
        return {**base, "action": "create_adgroup_in_existing_campaign", "campaign": c,
                "reason": f"Same goal, market, product ('{product_focus}') and offer as '{c['name']}', so the new ad group goes inside it."}
    if len(live_now) >= cap and pool:
        c = newest(same_market or live_now or pool)
        return {**base, "action": "create_adgroup_in_existing_campaign", "campaign": c,
                "reason": f"This budget supports {cap} campaign{'s' if cap != 1 else ''} and you have {len(live_now)} running: "
                          f"consolidating: the new ad group goes inside '{c['name']}' instead of splitting the budget."}
    return {**base, "action": "create_new_campaign", "campaign": None,
            "reason": _why_new(pool, same_market, prod, off, product_focus)}


def _why_new(pool, same_market, prod, off, product_focus) -> str:
    if not pool:
        return "First campaign: nothing to reuse yet."
    if not same_market:
        return "New campaign because the market is different from your existing campaigns."
    c = same_market[0]
    if not c.get("product_focus"):
        return f"New campaign: '{c['name']}' has no product recorded, so ProAds can't tell it sells the same thing as '{product_focus or 'this'}'."
    if norm(c.get("product_focus")) != prod:
        return f"New campaign because the product is different: '{product_focus}' vs '{c['product_focus']}' in '{c['name']}'."
    return "New campaign because the offer is different from your existing campaign."
