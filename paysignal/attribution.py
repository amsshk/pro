"""Which post, story, reel or ad brought each payment.

Organic first: every post gets its own tagged link and/or promo code, so a
payment can be traced to the exact piece of content, even when it comes in
later by cash or WhatsApp (staff log the code or post in the CSV).
"""
from __future__ import annotations

import re
import secrets
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from .models import Payment


# ---------- creating trackable posts ----------

def tag_link(url: str, platform: str, post_id: str, campaign: str = "organic",
             medium: str = "organic") -> str:
    """Add UTM tags to a link for a bio, story sticker or post caption."""
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q.update(utm_source=platform.lower(), utm_medium=medium, utm_campaign=campaign, utm_content=post_id)
    return urlunparse(parts._replace(query=urlencode(q)))


def make_promo_code(post_id: str, prefix: str = "") -> str:
    """Short, unique, easy-to-say code for a post, e.g. GLOW-7K2F."""
    base = re.sub(r"[^A-Z0-9]", "", (prefix or post_id).upper())[:6] or "POST"
    return f"{base}-{secrets.token_hex(2).upper()}"


# ---------- reading where a payment came from ----------

@dataclass
class Touch:
    kind: str       # "paid", "organic", "unattributed"
    platform: str   # "instagram", "facebook", "tiktok", "unknown"
    content: str    # post / ad id, or "-"
    method: str     # how we knew: "utm", "promo_code", "click_id", "none"


def resolve(p: Payment, promo_map: dict[str, tuple[str, str]] | None = None) -> Touch:
    """promo_map: code -> (platform, post_id)."""
    promo_map = {k.upper(): v for k, v in (promo_map or {}).items()}
    medium = (p.utm_medium or "").lower()
    paid = medium in ("paid", "cpc", "paid_social", "ads")

    if p.utm_content or p.utm_source:
        return Touch("paid" if paid else "organic", (p.utm_source or "unknown").lower(),
                     p.utm_content or "-", "utm")
    if p.promo_code and p.promo_code.upper() in promo_map:
        platform, post = promo_map[p.promo_code.upper()]
        return Touch("organic", platform, post, "promo_code")
    if p.fbclid:   # Meta adds fbclid to organic link clicks too, so this is "a Meta click"
        return Touch("organic", "meta", "-", "click_id")
    if p.ttclid:
        return Touch("paid", "tiktok", "-", "click_id")
    return Touch("unattributed", "unknown", "-", "none")


def content_report(rows: list[tuple[Payment, float, float]],
                   promo_map: dict[str, tuple[str, str]] | None = None) -> list[dict]:
    """rows: (purchase, refunded_amount, real_value). One line per post, best first."""
    agg: dict[tuple, dict] = defaultdict(lambda: dict(orders=0, gross=0.0, refunds=0.0, real_value=0.0))
    for p, refunded, real in rows:
        t = resolve(p, promo_map)
        a = agg[(t.kind, t.platform, t.content)]
        a["orders"] += 1
        a["gross"] += p.amount
        a["refunds"] += refunded
        a["real_value"] += real
    out = []
    for (kind, platform, content), a in agg.items():
        out.append(dict(kind=kind, platform=platform, content=content, **a,
                        net=a["gross"] - a["refunds"],
                        refund_rate=(a["refunds"] / a["gross"]) if a["gross"] else 0.0))
    return sorted(out, key=lambda r: r["real_value"], reverse=True)
