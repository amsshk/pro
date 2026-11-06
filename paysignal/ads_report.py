"""Ads performance for Home: what Meta reports for the campaigns ProAds published, where the
results come from (countries, placements), and the truth check no ad tool shows: Meta's claimed
results next to the real sign-ups or paying customers ProAds measured, after refunds."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Callable, Optional

RESULT_TYPES = {
    "signup": ("offsite_conversion.fb_pixel_complete_registration", "complete_registration",
               "omni_complete_registration", "lead", "offsite_conversion.fb_pixel_lead"),
    "purchase": ("omni_purchase", "offsite_conversion.fb_pixel_purchase", "purchase"),
}
PLACEMENT_NAMES = {"feed": "Feed", "instagram_reels": "Reels", "facebook_reels": "Reels", "instagram_stories": "Stories",
                   "facebook_stories": "Stories", "instagram_explore": "Explore", "marketplace": "Marketplace",
                   "video_feeds": "Video feeds", "right_hand_column": "Right column", "search": "Search",
                   "an_classic": "Apps & sites", "rewarded_video": "Apps & sites", "messenger_inbox": "Messenger",
                   "instagram_profile_feed": "Profile feed", "facebook_profile_feed": "Profile feed"}


def results_of(row: dict, kind: str) -> float:
    """Meta returns the same conversion under several names; take the first one we recognise."""
    acts = {a.get("action_type"): float(a.get("value") or 0) for a in row.get("actions") or []}
    for t in RESULT_TYPES[kind]:
        if t in acts:
            return acts[t]
    return 0.0


def _num(row, k):
    try:
        return float(row.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def build_report(pub, campaign_ids: list[str], kind: str, days: int, today: date,
                 truth: Callable[[date, date], dict]) -> dict:
    """pub: meta_ads.Publisher for the client's ad account. truth(start, end) -> ProAds' own real numbers."""
    start = today - timedelta(days=days - 1)
    since, until = start.isoformat(), today.isoformat()
    daily = pub.insights(campaign_ids, since, until, daily=True)
    by_day = defaultdict(lambda: {"spend": 0.0, "results": 0.0, "clicks": 0.0})
    tot = defaultdict(float)
    currency = ""
    for r in daily:
        d = r.get("date_start", since)
        by_day[d]["spend"] += _num(r, "spend")
        by_day[d]["clicks"] += _num(r, "clicks")
        by_day[d]["results"] += results_of(r, kind)
        for k in ("spend", "impressions", "reach", "clicks"):
            tot[k] += _num(r, k)
        tot["results"] += results_of(r, kind)
        currency = r.get("account_currency") or currency
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    def grouped(breakdowns, key):
        out = defaultdict(lambda: {"spend": 0.0, "clicks": 0.0, "results": 0.0, "impressions": 0.0})
        for r in pub.insights(campaign_ids, since, until, breakdowns=breakdowns):
            g = out[key(r)]
            g["spend"] += _num(r, "spend"); g["clicks"] += _num(r, "clicks")
            g["impressions"] += _num(r, "impressions"); g["results"] += results_of(r, kind)
        rows = [{"name": k, **{x: round(v, 2) for x, v in g.items()}} for k, g in out.items() if k]
        return sorted(rows, key=lambda x: (-x["results"], -x["clicks"], -x["spend"]))[:8]

    countries = grouped("country", lambda r: (r.get("country") or "").upper())
    placements = grouped("publisher_platform,platform_position", lambda r: (
        (r.get("publisher_platform") or "").replace("_", " ").title() + " " +
        PLACEMENT_NAMES.get(r.get("platform_position") or "", (r.get("platform_position") or "").replace("_", " ").title())).strip())
    real = truth(start, today)
    spend = round(tot["spend"], 2)
    res = round(tot["results"])
    return {
        "kind": kind, "currency": currency, "dates": dates,
        "series": {"spend": [round(by_day[d]["spend"], 2) for d in dates],
                   "results": [round(by_day[d]["results"]) for d in dates],
                   "clicks": [round(by_day[d]["clicks"]) for d in dates]},
        "totals": {"spend": spend, "impressions": round(tot["impressions"]), "reach": round(tot["reach"]),
                   "clicks": round(tot["clicks"]), "results": res,
                   "ctr": round(tot["clicks"] / tot["impressions"], 4) if tot["impressions"] else 0.0,
                   "cost_per_result": round(spend / res, 2) if res else None},
        "countries": countries, "placements": placements,
        "truth": real,
    }
