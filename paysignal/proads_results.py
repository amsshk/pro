"""What ProAds did for a business since it signed up: only things ProAds itself did or measured.

The Home page shows this one picture instead of a wall of numbers the business already has in Shopify:
  campaigns made with ProAds -> spent on them -> clicks -> paid orders from ProAds-tracked ads -> money kept
plus the work ProAds does in the background: real paid sales delivered to Meta and TikTok so the ads learn who buys,
and the buyers it knows. Nothing here is estimated: a step with no data says so instead of showing a guess.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

AD_SOURCES = ("tiktok", "facebook", "instagram", "meta", "fb", "ig")


def from_ads(p) -> Optional[str]:
    """'tiktok' / 'meta' when the order came through a tracked ad click (ProAds tracking link or the platform's click id)."""
    src, med = (p.utm_source or "").lower(), (p.utm_medium or "").lower()
    if p.ttclid or (src == "tiktok" and med in ("paid", "cpc", "ads", "proads")):
        return "tiktok"
    if p.fbclid or (src in AD_SOURCES[1:] and med in ("paid", "cpc", "ads", "proads")):
        return "meta"
    return None


def build(purchases, refunded: Callable, since: Optional[datetime], campaigns: list[dict], spend: dict,
          sent: dict, buyers: Optional[int]) -> dict:
    """campaigns: [{platform, id, name, status}] made with ProAds. spend: {"spend", "clicks", "known"} for those
    campaigns since `since`. sent: {"meta": n, "tiktok": n} real paid sales accepted by each platform since `since`."""
    orders = {"tiktok": 0, "meta": 0}
    kept = {"tiktok": 0.0, "meta": 0.0}
    for p in purchases:
        if since and p.occurred_at < since:
            continue
        k = p.amount - refunded(p)
        src = from_ads(p)
        if src and k > 0.005:
            orders[src] += 1
            kept[src] += k
    n_orders, money = orders["tiktok"] + orders["meta"], kept["tiktok"] + kept["meta"]
    sp = spend.get("spend") if spend.get("known") else None
    return {
        "since": int(since.timestamp()) if since else None,
        "campaigns": len(campaigns), "live": sum(1 for c in campaigns if c.get("status") in ("ACTIVE", "ENABLE", "live")),
        "campaign_list": campaigns[:8],
        "spend": round(sp, 2) if sp is not None else None, "clicks": spend.get("clicks") if spend.get("known") else None,
        "orders": n_orders, "orders_by": orders, "money": round(money, 2),
        "back_per_1": round(money / sp, 2) if sp else None,
        "cost_per_order": round(sp / n_orders, 2) if sp and n_orders else None,
        "sent_meta": sent.get("meta") or 0, "sent_tiktok": sent.get("tiktok") or 0, "buyers": buyers,
    }


def parse_created(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def health_score(accepted_7d: int, sent_7d: int, matchable_share: Optional[float], split: Optional[int]) -> dict:
    """0-100 for one platform, from what ProAds measures itself:
    volume (40): accepted purchases in 7 days against the ~50 the platform wants to finish learning;
    match (30): share of recent paid orders carrying an email, phone or click id the platform can match to a person;
    delivery (15): accepted / sent;  focus (15): one purchase campaign at a time instead of several splitting buyers."""
    volume = 40 * min(accepted_7d / 50, 1)
    match = 30 * (matchable_share if matchable_share is not None else 0)
    delivery = 15 * (accepted_7d / sent_7d if sent_7d else 0)
    focus = 15 / split if split and split > 1 else 15
    score = round(volume + match + delivery + focus)
    weakest = min((("volume", volume / 40), ("match", match / 30), ("delivery", delivery / 15 if sent_7d else 1), ("focus", focus / 15)),
                  key=lambda x: x[1])[0]
    tip = {"volume": "Needs more paid purchases: one broad campaign with enough budget, and every sale sent.",
           "match": "Too many orders arrive without email or phone: give the ProAds Shopify app customer data access.",
           "delivery": "Some sales were refused: see the reason on Tracking.",
           "focus": "Several purchase campaigns split the same buyers: run one."}[weakest]
    return {"score": score, "label": "Good" if score >= 75 else "Okay" if score >= 50 else "Weak", "tip": tip,
            "parts": {"volume": round(volume), "match": round(match), "delivery": round(delivery), "focus": round(focus)}}
