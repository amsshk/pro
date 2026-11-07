"""Meta Learning Accelerator.

1. Signal log: every delivery to Meta's Conversions API, what Meta accepted or rejected, and which match keys
   each purchase carried (Meta matches buyers on these; more keys = better matching = faster learning).
2. Double-count check: compares the Purchase events the business's Pixel received with the ones ProAds sent.
   Many more on the Pixel means something else also reports purchases (e.g. a shop's own Meta integration).
3. Learning status per ad set, straight from Meta (learning_stage_info), with plain advice:
   too few purchases a week, budgets split too thin, recent edits restarting learning.
ProAds cannot force Meta to learn; it can send cleaner signals and stop structures that slow learning down.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Optional

LEARNING_PER_WEEK = 50
KEY_LABELS = {"em": "Email", "ph": "Phone", "external_id": "Customer ID", "fbc": "Ad click ID", "fbp": "Browser ID",
              "client_ip_address": "IP + device", "fn": "First name", "ln": "Last name", "ct": "City", "zp": "Postcode",
              "country": "Country"}


class SignalLog:
    SCHEMA = """CREATE TABLE IF NOT EXISTS capi_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, at INTEGER NOT NULL, mode TEXT NOT NULL,
        purchases INTEGER, refunds INTEGER, sent INTEGER, received INTEGER, no_identifier INTEGER, too_old INTEGER,
        error TEXT, trace TEXT, keys TEXT)"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def log(self, client_id: str, mode: str, results: list[dict]) -> Optional[dict]:
        rep = next((r for r in results if r.get("report")), None)
        batches = [r for r in results if not r.get("report")]
        if not rep or not (rep["purchases"] or rep["refunds"] or rep["too_old"] or rep["no_identifier"]):
            return None
        sent = sum(int(r.get("count") or 0) for r in batches)
        received = sum(int((r.get("body") or {}).get("events_received", 0) or 0) for r in batches if not r.get("dry_run"))
        errors = [((r.get("body") or {}).get("error") or {}) for r in batches if not r.get("dry_run") and r.get("status") != 200]
        err = "; ".join((e.get("error_user_msg") or e.get("message") or "rejected")[:200] for e in errors) or None
        trace = next(((r.get("body") or {}).get("fbtrace_id") for r in batches if (r.get("body") or {}).get("fbtrace_id")), None)
        keys = Counter(k for ks in rep["keys"] for k in ks)
        row = (client_id, int(time.time()), mode, rep["purchases"], rep["refunds"], sent, received,
               rep["no_identifier"], rep["too_old"], err, trace, json.dumps({"events": len(rep["keys"]), "keys": keys}))
        self.db.execute("INSERT INTO capi_log (client_id, at, mode, purchases, refunds, sent, received, no_identifier,"
                        " too_old, error, trace, keys) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        self.db.execute("DELETE FROM capi_log WHERE client_id=? AND at < ?", (client_id, int(time.time()) - 90 * 86400))
        self.db.commit()
        return {"sent": sent, "received": received, "error": err}

    def summary(self, client_id: str, days: int = 7) -> dict:
        since = int(time.time()) - days * 86400
        rows = self.db.execute("SELECT at, mode, purchases, refunds, sent, received, no_identifier, too_old, error, keys"
                               " FROM capi_log WHERE client_id=? AND at>=? ORDER BY at", (client_id, since)).fetchall()
        live = [r for r in rows if r[1] == "live"]
        keys, events = Counter(), 0
        for r in live or rows:
            k = json.loads(r[9] or "{}")
            events += k.get("events", 0)
            keys.update(k.get("keys", {}))
        last_ok = max((r[0] for r in live if not r[8] and r[5]), default=None)
        last_err = next(((r[0], r[8]) for r in reversed(live) if r[8]), None)
        return {
            "days": days, "mode": "live" if live else ("practice" if rows else "none"),
            "purchases": sum(r[2] or 0 for r in rows), "refunds": sum(r[3] or 0 for r in rows),
            "sent": sum(r[4] or 0 for r in live), "accepted": sum(r[5] or 0 for r in live),
            "no_identifier": sum(r[6] or 0 for r in rows), "too_old": sum(r[7] or 0 for r in rows),
            "last_accepted_at": last_ok, "last_error": {"at": last_err[0], "message": last_err[1]} if last_err else None,
            "match_keys": [{"key": k, "label": KEY_LABELS[k], "share": round(keys.get(k, 0) / events, 3) if events else 0}
                           for k in KEY_LABELS],
            "events_with_keys": events,
        }

    def sent_purchases_since(self, client_id: str, since: int) -> int:
        r = self.db.execute("SELECT COALESCE(SUM(purchases),0) FROM capi_log WHERE client_id=? AND mode='live' AND at>=?"
                            " AND error IS NULL", (client_id, since)).fetchone()
        return int(r[0] or 0)

    def forget(self, client_id: str) -> None:
        self.db.execute("DELETE FROM capi_log WHERE client_id=?", (client_id,))
        self.db.commit()


def double_count_check(pixel_purchases: Optional[int], proads_sent: int) -> dict:
    """Pixel Purchase total vs what ProAds sent. Browser + server copies of the same sale are merged by Meta only
    when they share an event ID, which a shop's own integration never shares with ProAds."""
    if pixel_purchases is None:
        return {"status": "unknown", "message": "Couldn't read your Pixel's totals from Meta."}
    if proads_sent == 0:
        return {"status": "unknown", "pixel_purchases": pixel_purchases, "proads_sent": 0,
                "message": "ProAds hasn't sent purchases in the last 7 days, so there's nothing to compare yet."}
    ratio = pixel_purchases / proads_sent
    if ratio >= 1.6:
        return {"status": "warning", "pixel_purchases": pixel_purchases, "proads_sent": proads_sent,
                "message": f"Your Pixel recorded {pixel_purchases} purchases but ProAds sent {proads_sent}. Something else "
                           "also reports purchases (often the shop's own Meta app or a website pixel), so Meta may count "
                           "some sales twice and learn from inflated numbers.",
                "fix": "Keep one sender of Purchase: switch off purchase sharing in the shop's Meta app (Shopify: "
                       "Facebook & Instagram app > Data sharing), or tell us to stop ProAds sending."}
    return {"status": "ok", "pixel_purchases": pixel_purchases, "proads_sent": proads_sent,
            "message": "No sign of purchases being reported twice."}


def pixel_purchases(http, token: str, pixel_id: str, version: str, since: int) -> Optional[int]:
    """Purchase events the Pixel/dataset received since a time, from Meta's stats endpoint."""
    try:
        r = http.get(f"https://graph.facebook.com/{version}/{pixel_id}/stats",
                     params={"access_token": token, "aggregation": "event", "start_time": since}, timeout=20)
        body = r.json()
    except Exception:
        return None
    if r.status_code != 200 or "error" in body:
        return None
    total = 0
    for bucket in body.get("data") or []:
        for d in bucket.get("data") or []:
            if (d.get("value") or "").lower() == "purchase":
                total += int(d.get("count") or 0)
    return total


def learning_advice(adsets: list[dict], target_cpa: Optional[float], kind: str) -> list[dict]:
    """Plain advice from Meta's own learning status and how the budget is split."""
    out = []
    live = [a for a in adsets if a.get("effective_status") == "ACTIVE"]
    limited = [a for a in live if a["learning"] == "limited"]
    learning = [a for a in live if a["learning"] == "learning"]
    goal = "purchases" if kind == "purchase" else "sign-ups"
    if len(live) > 1 and all((a.get("conversions_week") or 0) < LEARNING_PER_WEEK for a in live):
        out.append({"level": "high", "title": "Budget split too thin",
                    "detail": f"{len(live)} live ad sets each get fewer than {LEARNING_PER_WEEK} {goal} a week, so none can "
                              "finish learning.",
                    "action": "Merge them into one ad set with the combined budget."})
    if limited:
        out.append({"level": "high", "title": "Learning limited",
                    "detail": f"Meta marked {len(limited)} ad set{'s' if len(limited) > 1 else ''} as unable to finish learning.",
                    "action": "Raise the budget, merge ad sets, or widen the audience; avoid narrow targeting."})
    recent = [a for a in live if a.get("last_edit") and time.time() - a["last_edit"] < 3 * 86400]
    if recent:
        out.append({"level": "medium", "title": "Recent big edits",
                    "detail": f"{len(recent)} ad set{'s were' if len(recent) > 1 else ' was'} changed in the last 3 days, "
                              "which restarts Meta's learning.",
                    "action": "Leave them alone for about a week; change budgets by at most 20% at a time."})
    weekly_budget = sum((a.get("daily_budget") or 0) for a in live) * 7
    if target_cpa and weekly_budget and weekly_budget < target_cpa * LEARNING_PER_WEEK:
        possible = weekly_budget / target_cpa
        out.append({"level": "medium", "title": "Budget too small to learn on purchases",
                    "detail": f"At your target cost, the weekly budget buys about {possible:.0f} {goal}; Meta usually wants "
                              f"about {LEARNING_PER_WEEK} a week per ad set.",
                    "action": "Keep a single ad set; expect slower learning. It still works, only judge it later."})
    if learning and not out:
        out.append({"level": "low", "title": "Learning", "detail": "Meta is still learning. This is normal for the first week.",
                    "action": "Don't change anything until it finishes."})
    return out
