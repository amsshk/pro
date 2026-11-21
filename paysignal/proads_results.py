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
