"""ProAds Impact: the difference ProAds makes, in percentages, without claiming more than the data shows.

Three layers, each labelled with where its numbers come from:
1. What Meta says  - Meta's Dataset Quality API for the Purchase event: Additional Conversions Reported (ACR, the
                     share of purchases Meta measures because of the Conversions API), Event Match Quality (0-10)
                     and event coverage. Meta's own figures.
2. What ProAds sent - of the real paid orders in the last 30 days: how many reached Meta (and TikTok), how many carried
                     an email or phone for matching, and how many were paid outside the website checkout, where no
                     browser pixel can see them.
3. What changed     - the same length of time before and after ProAds started sending sales: real orders a day, money
                     really paid a day and, with ad spend, money back per 1 spent (really paid / spend). Shown as a
                     change with a 90% range from resampling days; it's a before/after comparison, not a controlled
                     test, so seasons, offers and ad changes also move it.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Optional

DAY = 86400
WINDOW_DAYS = 28
MIN_AFTER_DAYS = 7
OUTSIDE_SOURCES = ("csv", "upload")


def outside_checkout(p) -> bool:
    """Paid where a website pixel can't see it. Only sales marked as in-store/offline count: a Stripe, Tabby or Ziina
    payment is usually made in the shop's own website checkout, so ProAds doesn't guess that it wasn't."""
    return p.channel == "physical_store"


def _day(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def daily(purchases, refunded, start: datetime, days: int) -> list[tuple[int, float]]:
    """(orders, money kept) for each day from start."""
    keys = [_day(start + timedelta(days=i)) for i in range(days)]
    out = {k: [0, 0.0] for k in keys}
    for p in purchases:
        k = _day(p.occurred_at)
        if k in out:
            kept = max(p.amount - refunded(p), 0)
            if kept > 0.005:
                out[k][0] += 1
                out[k][1] += kept
    return [tuple(out[k]) for k in keys]


def _change(before: list[float], after: list[float], rng: random.Random, n: int = 2000) -> Optional[dict]:
    b, a = sum(before) / len(before), sum(after) / len(after)
    if b <= 0:
        return None
    sims = []
    for _ in range(n):
        bs = sum(rng.choice(before) for _ in before) / len(before)
        as_ = sum(rng.choice(after) for _ in after) / len(after)
        if bs > 0:
            sims.append(as_ / bs - 1)
    sims.sort()
    lo, hi = sims[int(0.05 * len(sims))], sims[int(0.95 * len(sims)) - 1]
    return {"before": round(b, 2), "after": round(a, 2), "change": round(a / b - 1, 4), "low": round(lo, 4), "high": round(hi, 4),
            "clear": lo > 0 or hi < 0}


def before_after(purchases, refunded, started_at: datetime, now: datetime,
                 spend_before: Optional[float] = None, spend_after: Optional[float] = None) -> dict:
    after_days = min(WINDOW_DAYS, (now.date() - started_at.date()).days)
    if after_days < MIN_AFTER_DAYS:
        return {"ready": False, "started_at": int(started_at.timestamp()), "days_after": max(after_days, 0),
                "needs_days": MIN_AFTER_DAYS - max(after_days, 0)}
    day0 = datetime(started_at.year, started_at.month, started_at.day, tzinfo=timezone.utc)
    before = daily(purchases, refunded, day0 - timedelta(days=after_days), after_days)
    after = daily(purchases, refunded, day0, after_days)
    if sum(o for o, _ in before) < 5:
        return {"ready": False, "started_at": int(started_at.timestamp()), "days_after": after_days, "too_few_before": True}
    rng = random.Random(7)
    out = {"ready": True, "started_at": int(started_at.timestamp()), "days": after_days,
           "orders": _change([o for o, _ in before], [o for o, _ in after], rng),
           "money": _change([m for _, m in before], [m for _, m in after], rng)}
    if spend_before and spend_after:
        rb = sum(m for _, m in before) / spend_before
        ra = sum(m for _, m in after) / spend_after
        out["return"] = {"before": round(rb, 2), "after": round(ra, 2), "change": round(ra / rb - 1, 4) if rb else None,
                         "spend_before": round(spend_before, 2), "spend_after": round(spend_after, 2)}
    return out


def coverage(purchases, refunded, since: datetime, sent_meta: int, match: dict, sent_tiktok: Optional[int] = None,
             meta_since: Optional[datetime] = None, tiktok_since: Optional[datetime] = None) -> dict:
    """Shares are counted only over the days ProAds was sending to each platform, so orders from before it was
    connected (or uploaded history) don't make the share look low."""
    paid = [p for p in purchases if p.amount - refunded(p) > 0.005]
    real = [p for p in paid if p.occurred_at >= since]
    n = len(real)
    outside = sum(1 for p in real if outside_checkout(p))
    def share(sent, start):
        if sent is None or start is None:
            return None, 0, None
        frm = max(since, start)
        base = sum(1 for p in paid if p.occurred_at >= frm)
        return (round(min(sent / base, 1), 4) if base else None), base, int(frm.timestamp())
    ms, mbase, mfrom = share(sent_meta, meta_since)
    ts, tbase, tfrom = share(sent_tiktok, tiktok_since)
    return {"orders": n, "sent_meta": sent_meta, "sent_meta_share": ms, "meta_orders": mbase, "meta_from": mfrom,
            "sent_tiktok": sent_tiktok, "sent_tiktok_share": ts, "tiktok_orders": tbase, "tiktok_from": tfrom,
            "outside_checkout": outside, "outside_share": round(outside / n, 4) if n else None,
            "with_email": match.get("em"), "with_phone": match.get("ph")}


def meta_quality(http, token: str, dataset_id: str, version: str) -> dict:
    """Meta's Dataset Quality API, Purchase event only."""
    fields = ("web{event_name,event_match_quality{composite_score},acr{percentage},event_coverage{percentage},"
              "data_freshness{upload_frequency}}")
    try:
        r = http.get(f"https://graph.facebook.com/{version}/dataset_quality",
                     params={"dataset_id": dataset_id, "access_token": token, "fields": fields}, timeout=20)
        body = r.json()
    except Exception as e:
        return {"error": f"Couldn't reach Meta ({type(e).__name__})."}
    if r.status_code != 200 or "error" in body:
        return {"error": ((body.get("error") or {}).get("message") or f"HTTP {r.status_code}")[:200]}
    rows = body.get("web") or (body.get("data") or [{}])[0].get("web") or []
    ev = next((x for x in rows if (x.get("event_name") or "").lower() == "purchase"), None)
    if not ev:
        return {"error": "Meta has no Purchase quality figures for this Pixel yet (it needs a few days of purchases)."}
    num = lambda v: float(v) if isinstance(v, (int, float, str)) and str(v).replace(".", "", 1).isdigit() else None
    return {"acr": num((ev.get("acr") or {}).get("percentage")),
            "match_quality": num((ev.get("event_match_quality") or {}).get("composite_score")),
            "coverage": num((ev.get("event_coverage") or {}).get("percentage")),
            "freshness": (ev.get("data_freshness") or {}).get("upload_frequency")}
