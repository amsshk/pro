"""Month-end settlement of the ad spend fee.

Rules (also shown to clients):
  - A billing period runs from its start up to (not including) its end, as Stripe defines it.
  - A day of spend belongs to the period in which that day starts, in the AD ACCOUNT's time zone
    (Meta reports daily spend in that time zone). So every day belongs to exactly one period, DST included.
  - SETTLE_HOURS after a period ends (Meta keeps revising recent days), ProAds reads the spend once more and
    settles the period. A settled period is locked: its numbers never change.
  - As soon as a period settles, its fee is billed on its own invoice (charged to the saved card).
  - Rounding: fee = spend (in the invoice currency) x percent, rounded half-up to the smallest unit (e.g. fils).
  - Corrections: exactly once, RECHECK_AFTER_DAYS after billing, ProAds compares with Meta's latest numbers.
    A difference of at least the larger of 1.00 (in the invoice currency) or 1% of the original fee is billed
    (or credited) as its own "correction" line referencing the original FP number. After that check the period
    is final: later changes by Meta are ignored.
Statuses:
  settling   period ended less than SETTLE_HOURS ago
  pending    some spend still can't be read (e.g. Facebook not connected); retried, never treated as zero
  ready      settled and locked, waiting for an invoice      (completeness: complete / incomplete)
  nothing    settled, no fee to charge
  billed     on a Stripe invoice
"incomplete" = a campaign was deleted on Meta before every day of the period was read; only the spend that was
recorded is charged (never an estimate), and the invoice line says so.
Every period keeps the full calculation (campaigns, days, time zones, exchange rates) for audit.
"""
from __future__ import annotations

import json
import math
import time
from decimal import ROUND_HALF_UP, Decimal
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

SETTLE_HOURS = 72
RECHECK_AFTER_DAYS = 7          # look for Meta corrections once, a week after billing
MATERIAL_MINOR = 100            # corrections smaller than 1.00 (or 1% of the fee) are ignored


def local_days(start: int, end: int, tz: str) -> tuple[str, str]:
    """The ad-account-local dates whose midnight falls in [start, end). until < since means none."""
    z = ZoneInfo(tz or "UTC")
    s = datetime.fromtimestamp(start, timezone.utc).astimezone(z)
    e = datetime.fromtimestamp(end, timezone.utc).astimezone(z)
    since = s.date() if (s.hour, s.minute, s.second, s.microsecond) == (0, 0, 0, 0) else s.date() + timedelta(days=1)
    until = e.date() if (e.hour, e.minute, e.second, e.microsecond) != (0, 0, 0, 0) else e.date() - timedelta(days=1)
    return since.isoformat(), until.isoformat()


class FeePeriods:
    SCHEMA = """CREATE TABLE IF NOT EXISTS fee_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, customer TEXT NOT NULL,
        invoice_currency TEXT NOT NULL, period_start INTEGER NOT NULL, period_end INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'period', parent_id INTEGER, status TEXT NOT NULL DEFAULT 'settling',
        completeness TEXT, reason TEXT, fee_minor INTEGER, detail TEXT, invoice_id TEXT, item_id TEXT,
        created_at INTEGER NOT NULL, settled_at INTEGER, billed_at INTEGER, rechecked INTEGER NOT NULL DEFAULT 0,
        UNIQUE (client_id, kind, period_start, parent_id))"""
    COLS = ("id", "client_id", "customer", "invoice_currency", "period_start", "period_end", "kind", "parent_id",
            "status", "completeness", "reason", "fee_minor", "detail", "invoice_id", "item_id", "created_at",
            "settled_at", "billed_at", "rechecked")

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def _rows(self, where: str, args=()) -> list[dict]:
        rows = self.db.execute(f"SELECT {','.join(self.COLS)} FROM fee_periods WHERE {where} ORDER BY id", args).fetchall()
        out = []
        for r in rows:
            d = dict(zip(self.COLS, r))
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    def open(self, client_id: str, customer: str, currency: str, start: int, end: int) -> None:
        if not (customer and currency and start and end and end > start):
            return
        self.db.execute("INSERT OR IGNORE INTO fee_periods (client_id, customer, invoice_currency, period_start,"
                        " period_end, parent_id, created_at) VALUES (?,?,?,?,?,0,?)",
                        (client_id, customer, currency.upper(), int(start), int(end), int(time.time())))
        self.db.commit()

    def get(self, pid: int) -> Optional[dict]:
        rows = self._rows("id=?", (pid,))
        return rows[0] if rows else None

    def due(self, now: Optional[float] = None) -> list[dict]:
        now = now or time.time()
        return [p for p in self._rows("status IN ('settling','pending')") if now >= p["period_end"] + SETTLE_HOURS * 3600]

    def ready(self, client_id: Optional[str] = None) -> list[dict]:
        return self._rows("status='ready'" + (" AND client_id=?" if client_id else ""), (client_id,) if client_id else ())

    def for_client(self, client_id: str) -> list[dict]:
        return list(reversed(self._rows("client_id=?", (client_id,))))[:36]

    def to_recheck(self, now: Optional[float] = None) -> list[dict]:
        now = now or time.time()
        return [p for p in self._rows("status='billed' AND kind='period' AND rechecked=0")
                if p["billed_at"] and now >= p["billed_at"] + RECHECK_AFTER_DAYS * 86400]

    def set_pending(self, pid: int, reason: str) -> None:
        self.db.execute("UPDATE fee_periods SET status='pending', reason=? WHERE id=? AND status IN ('settling','pending')",
                        (reason[:300], pid))
        self.db.commit()

    def settle(self, pid: int, fee_minor: int, completeness: str, detail: dict) -> None:
        """Lock the result. Only settling/pending periods can be settled, so a settled one never changes."""
        self.db.execute("UPDATE fee_periods SET status=?, fee_minor=?, completeness=?, detail=?, reason=NULL,"
                        " settled_at=? WHERE id=? AND status IN ('settling','pending')",
                        ("ready" if fee_minor else "nothing", fee_minor, completeness, json.dumps(detail),
                         int(time.time()), pid))
        self.db.commit()

    def billed(self, pid: int, invoice_id: Optional[str], item_id: Optional[str]) -> None:
        self.db.execute("UPDATE fee_periods SET status='billed', invoice_id=?, item_id=?, billed_at=? WHERE id=?",
                        (invoice_id, item_id, int(time.time()), pid))
        self.db.commit()

    def adjustment(self, parent: dict, fee_minor: int, detail: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO fee_periods (client_id, customer, invoice_currency, period_start, period_end,"
                        " kind, parent_id, status, completeness, fee_minor, detail, created_at, settled_at)"
                        " VALUES (?,?,?,?,?,'adjustment',?,'ready','complete',?,?,?,?)",
                        (parent["client_id"], parent["customer"], parent["invoice_currency"], parent["period_start"],
                         parent["period_end"], parent["id"], fee_minor, json.dumps(detail), int(time.time()), int(time.time())))
        self.db.execute("UPDATE fee_periods SET rechecked=1 WHERE id=?", (parent["id"],))
        self.db.commit()

    def mark_rechecked(self, pid: int) -> None:
        self.db.execute("UPDATE fee_periods SET rechecked=1 WHERE id=?", (pid,))
        self.db.commit()


def calculate(period: dict, campaigns: list[dict], ledger, percent: float,
              rate: Callable[[str, str], float], connected: bool) -> dict:
    """Work out one period from the spend ledger. Returns {"status": "pending", "reason"} or the settled result."""
    start, end = period["period_start"], period["period_end"]
    out_cur = period["invoice_currency"]
    lines, missing, incomplete = [], [], False
    for c in campaigns:
        st = ledger.state(c["campaign_id"]) or {}
        if c.get("created_ts") and c["created_ts"] >= end:
            continue                                        # made after this period
        tz = ledger.tz(st.get("ad_account") or "")
        if not tz:
            if not (st.get("gone") or st.get("closed")) and connected:
                missing.append(c["campaign_id"])            # wait until the ad account's time zone is known
                continue
            tz = "UTC"
        since, until = local_days(start, end, tz)
        if until < since:
            continue
        need = until
        if st.get("closed") and st.get("closed_day"):
            need = min(until, st["closed_day"])             # verified paused at disconnect: nothing after that
        covered = bool(st.get("synced_through")) and st["synced_through"] >= need
        if covered and not st.get("closed"):
            covered = (st.get("last_ok") or 0) >= end + SETTLE_HOURS * 3600 - 3600   # read after the settling window
        days = ledger.days(c["campaign_id"], since, until)
        if not covered:
            if st.get("gone"):
                incomplete = True                           # Meta can no longer tell us; charge only what we have
            else:
                missing.append(c["campaign_id"])
                continue
        spend = round(sum(d["spend"] for d in days), 2)
        cur = next((d["currency"] for d in days if d["currency"]), "")
        lines.append({"campaign_id": c["campaign_id"], "name": c.get("name", ""), "ad_account": st.get("ad_account") or "",
                      "time_zone": tz, "since": since, "until": until, "days": days, "spend": spend,
                      "currency": cur, "complete": covered})
    if missing:
        n = f"{len(missing)} campaign{'s' if len(missing) > 1 else ''}"
        return {"status": "pending", "reason": (f"Meta hasn't reported the final spend of {n} yet" if connected else
                                                f"Facebook isn't connected, so the spend of {n} can't be read")}
    fx, converted = [], 0.0
    for cur in sorted({l["currency"] for l in lines if l["currency"] and l["spend"]}):
        r = 1.0 if cur == out_cur else rate(cur, out_cur)
        sub = round(sum(l["spend"] for l in lines if l["currency"] == cur), 2)
        fx.append({"from": cur, "to": out_cur, "rate": r, "spend": sub, "converted": round(sub * r, 2),
                   "source": "same currency" if r == 1.0 else "open.er-api.com daily rate", "at": int(time.time())})
        converted += sub * r
    converted = round(converted, 2)
    fee_minor = fee_of(converted, percent)
    return {"status": "settled", "fee_minor": fee_minor, "completeness": "incomplete" if incomplete else "complete",
            "detail": {"percent": percent, "rule": "days are ad-account-local; a day belongs to the period it starts in",
                       "campaigns": lines, "fx": fx, "spend_in_invoice_currency": converted,
                       "fee": fee_minor / 100, "currency": out_cur}}


def fee_of(amount: float, percent: float) -> int:
    """amount x percent%, in minor units, rounded half-up (6000.50 x 5% = 300.025 -> 300.03)."""
    return int((Decimal(str(amount)) * Decimal(str(percent))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def correction_threshold(original_minor: int) -> int:
    """Smallest difference (minor units) that becomes a correction: the larger of 1.00 or 1% of the original fee."""
    return max(MATERIAL_MINOR, math.ceil(abs(original_minor or 0) / 100))


INCOMPLETE_NOTE = ("This fee is calculated using the spend records available to ProAds. Some historical campaign "
                   "spend could not be retrieved from Meta, so the actual spend may have been higher than shown.")


def describe(p: dict) -> str:
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%d %b %Y")
    d = p["detail"] or {}
    when = f"{fmt(p['period_start'])} - {fmt(p['period_end'])}"
    if p["kind"] == "adjustment":
        return f"Ad spend fee correction for {when} (Meta revised the spend after billing) · ref FP-{p['parent_id']}"
    tail = " · recorded spend only: some campaign spend could not be retrieved from Meta" \
        if p["completeness"] == "incomplete" else ""
    return (f"Ad spend fee: {d.get('percent', 0):g}% of {p['invoice_currency']} {d.get('spend_in_invoice_currency', 0):,.2f} "
            f"spent through ProAds ({when}) · ref FP-{p['id']}{tail}")
