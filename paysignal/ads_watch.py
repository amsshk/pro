"""Which TikTok alert emails are due, from the ad groups that are switched on (pure: no network, no sending).

tt_balance        TikTok stopped delivery for low balance (status contains BALANCE_EXCEED). At most once a day.
tt_learning_off   an ad group was made with Skip Learning Phase on. Once per ad group.
tt_learning_on    a new ad group is learning (created after these emails were switched on). Once per ad group.
tt_learning_done  TikTok accepted about 50 ProAds purchases in 7 days, what learning needs. Once per ad group.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

LEARN_TARGET = 50


def _created(s: str) -> Optional[float]:
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def due(groups: list[dict], accepted_7d: int, installed_at: float, today: str) -> list[tuple[str, str, dict]]:
    """[(kind, ref, data)] - ref is what makes it 'already sent'."""
    out = []
    low = [g for g in groups if "BALANCE_EXCEED" in g["status"].upper()]
    if low:
        out.append(("tt_balance", today, {"campaign": low[0]["campaign"]}))
    for g in groups:
        if g.get("promote") or not g["id"]:
            continue
        if g["skip_learning"]:
            out.append(("tt_learning_off", g["id"], {"adgroup": g["name"]}))
            continue
        made = _created(g.get("created") or "")
        if made and made >= installed_at:
            out.append(("tt_learning_on", g["id"], {"adgroup": g["name"]}))
        if accepted_7d >= LEARN_TARGET:
            out.append(("tt_learning_done", g["id"], {"adgroup": g["name"], "purchases": accepted_7d}))
    return out
