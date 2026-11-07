"""Purchase Event Reliability: did every eligible event actually reach Meta, and was it accepted?

ProAds' own events (sign-ups, trials, first payments, refunds) are recorded with Meta's answer:
  accepted    Meta replied 200 and counted the event (events_received >= 1)
  retrying    the send failed for a reason that can be fixed: network, Meta down, rate limits, an expired or
              cancelled token, missing access to the dataset, or ProAds not configured yet. Retried with backoff for as
              long as Meta still accepts the event (7 days after it happened; ProAds stops at 6.5 to be safe).
  rejected    Meta refused the event itself (bad data). Not retried; the reason is kept so it can be fixed.
  expired     still failing when it became too old for Meta to accept.
Retries are safe: every event keeps its event_id, which Meta uses to drop duplicates.

Health turns this into plain alerts, e.g. "3 sign-ups since 21:10 and none reached Meta: the token was cancelled".
"""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Optional

MAX_AGE = int(6.5 * 86400)
BACKOFF = [300, 1800, 7200, 21600, 43200, 86400]      # 5 min, 30 min, 2 h, 6 h, 12 h, then daily
TOKEN_CODES = {190, 102, 463, 467}
RETRY_CODES = {1, 2, 4, 17, 32, 341, 613, 80004} | TOKEN_CODES | {10, 200, 294}   # 10/200/294: permission problems
KEY_LABELS = {"em": "Email", "ph": "Phone", "external_id": "Customer ID", "fbc": "Ad click ID", "fbp": "Browser ID",
              "client_ip_address": "IP address", "client_user_agent": "Device", "fn": "First name", "ln": "Last name",
              "ct": "City", "zp": "Postcode", "country": "Country"}


def classify(result: dict) -> tuple[str, Optional[str], Optional[int], Optional[str]]:
    """(status, error text, Meta error code, trace id) from growth.send()'s result."""
    if result.get("skipped"):
        return "retrying", "ProAds isn't set up to send events yet: " + result["skipped"], None, None
    if result.get("error"):
        return "retrying", "Couldn't reach Meta: " + str(result["error"])[:200], None, None
    status, body = result.get("status"), result.get("body") or {}
    if status == 200 and int(body.get("events_received") or 0) >= 1:
        return "accepted", None, None, body.get("fbtrace_id")
    e = body.get("error") or {}
    code = e.get("code")
    msg = (e.get("error_user_msg") or e.get("message") or f"HTTP {status}")[:300]
    if code in TOKEN_CODES:
        msg = "Facebook cancelled ProAds' token (PROADS_META_TOKEN). Put a new System User token in Render. Meta said: " + msg
    elif code in (10, 200, 294) or (code == 100 and e.get("error_subcode") == 33):
        msg = "The token can't send to this dataset. Give the System User access to it in Business Settings. Meta said: " + msg
        return "retrying", msg, code, e.get("fbtrace_id")
    if status is None or status >= 500 or code in RETRY_CODES or status == 200:
        return "retrying", msg, code, e.get("fbtrace_id")
    return "rejected", msg, code, e.get("fbtrace_id")


class DeliveryLog:
    SCHEMA = """CREATE TABLE IF NOT EXISTS meta_delivery (
        event_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, name TEXT NOT NULL, created_at INTEGER NOT NULL,
        event_time INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_try INTEGER,
        next_try INTEGER, accepted_at INTEGER, error TEXT, code INTEGER, trace TEXT, keys TEXT, event TEXT)"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.execute("CREATE INDEX IF NOT EXISTS meta_delivery_due ON meta_delivery (status, next_try)")
        self.db.commit()

    def queued(self, client_id: str, event: dict, now: Optional[int] = None) -> bool:
        """Record an event before it is sent. False if this event_id is already known (never send twice)."""
        now = int(now or time.time())
        keys = sorted(k for k, v in (event.get("user_data") or {}).items() if v)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO meta_delivery (event_id, client_id, name, created_at, event_time, status, next_try, keys,"
            " event) VALUES (?,?,?,?,?, 'sending', ?, ?, ?)",
            (event["event_id"], client_id, event["event_name"], now, int(event.get("event_time") or now), now,
             json.dumps(keys), json.dumps(event)))
        self.db.commit()
        return cur.rowcount == 1

    def result(self, event_id: str, result: dict, now: Optional[int] = None) -> str:
        now = int(now or time.time())
        r = self.db.execute("SELECT attempts, event_time FROM meta_delivery WHERE event_id=?", (event_id,)).fetchone()
        if not r:
            return "unknown"
        attempts = r[0] + 1
        status, err, code, trace = classify(result)
        nxt = None
        if status == "retrying":
            nxt = now + BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
            if nxt > r[1] + MAX_AGE:
                status, nxt = "expired", None
        self.db.execute("UPDATE meta_delivery SET status=?, attempts=?, last_try=?, next_try=?, error=?, code=?, trace=?,"
                        " accepted_at=CASE WHEN ?='accepted' THEN ? ELSE accepted_at END,"
                        " event=CASE WHEN ?='accepted' THEN NULL ELSE event END WHERE event_id=?",
                        (status, attempts, now, nxt, err, code, trace, status, now, status, event_id))
        self.db.commit()
        return status

    def due(self, now: Optional[int] = None, limit: int = 50) -> list[tuple[str, dict]]:
        """Events to retry now. Stuck 'sending' rows (the process died mid-send) count too after 10 minutes."""
        now = int(now or time.time())
        self.db.execute("UPDATE meta_delivery SET status='expired', next_try=NULL WHERE status IN ('retrying','sending')"
                        " AND event_time < ?", (now - MAX_AGE,))
        self.db.commit()
        rows = self.db.execute(
            "SELECT event_id, event FROM meta_delivery WHERE (status='retrying' AND next_try<=?)"
            " OR (status='sending' AND created_at<=?) ORDER BY event_time LIMIT ?", (now, now - 600, limit)).fetchall()
        return [(eid, json.loads(ev)) for eid, ev in rows if ev]

    def status_of(self, event_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT status, attempts, error, accepted_at FROM meta_delivery WHERE event_id=?", (event_id,)).fetchone()
        return dict(zip(("status", "attempts", "error", "accepted_at"), r)) if r else None

    def health(self, client_id: str, days: int = 7, now: Optional[int] = None) -> dict:
        now = int(now or time.time())
        rows = self.db.execute("SELECT name, status, created_at, accepted_at, error, keys, attempts FROM meta_delivery"
                               " WHERE client_id=? AND created_at>=? ORDER BY created_at", (client_id, now - days * 86400)).fetchall()
        by = {}
        keys, n_keys = Counter(), 0
        for name, status, created, accepted, err, k, attempts in rows:
            b = by.setdefault(name, Counter())
            b[status] += 1
            b["total"] += 1
            if status == "accepted" and attempts > 1:
                b["recovered"] += 1
            for x in json.loads(k or "[]"):
                keys[x] += 1
            n_keys += 1
        accepted = [r[3] for r in rows if r[1] == "accepted"]
        failing = [r for r in rows if r[1] in ("retrying", "sending")]
        lost = [r for r in rows if r[1] in ("rejected", "expired")]
        problem = next((r[4] for r in reversed(rows) if r[1] in ("retrying", "rejected", "expired") and r[4]), None)
        alerts = []
        if failing:
            first = min(r[2] for r in failing)
            alerts.append({"level": "bad", "text": f"{len(failing)} event{'s' if len(failing) != 1 else ''} since "
                           f"{time.strftime('%d %b %H:%M', time.gmtime(first))} UTC haven't reached Meta yet. ProAds keeps "
                           "retrying for up to 6.5 days, so nothing is lost if this is fixed in time.",
                           "fix": problem})
        if lost:
            alerts.append({"level": "bad", "text": f"{len(lost)} event{'s' if len(lost) != 1 else ''} could not be delivered "
                           "(refused by Meta or too old to send).", "fix": next((r[4] for r in reversed(lost) if r[4]), None)})
        return {"days": days, "total": len(rows), "accepted": len(accepted), "failing": len(failing), "lost": len(lost),
                "recovered": sum(b["recovered"] for b in by.values()),
                "last_accepted": max(accepted) if accepted else None, "by_event": {k: dict(v) for k, v in by.items()},
                "match_keys": [{"key": k, "label": KEY_LABELS.get(k, k), "share": round(c / n_keys, 3)}
                               for k, c in keys.most_common()] if n_keys else [],
                "alerts": alerts, "healthy": not alerts}
