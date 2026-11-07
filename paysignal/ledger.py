"""Local ledger of the daily ad spend of every ProAds campaign.

Billing reads this ledger, not Meta directly, so:
  - spend is recorded however a campaign was switched on (ProAds' Go live or Ads Manager),
  - spend stays on record if a campaign is later deleted in Ads Manager or Facebook is disconnected,
  - a billing period is only charged when every campaign is known to be synced through its last day.
    A gap is "unknown", never zero.

Meta revises recent days for a while, so each sync re-reads the last few days. Days already billed stay as billed.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

RESYNC_DAYS = 3                 # Meta may still revise the last few days
FRESH_FOR = 6 * 3600            # background sync of a campaign at most every 6 hours
RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80004}
GONE_CODES = {100}              # object doesn't exist (deleted for good)


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class SpendLedger:
    SCHEMA = [
        """CREATE TABLE IF NOT EXISTS spend_ledger (
            campaign_id TEXT NOT NULL, day TEXT NOT NULL, client_id TEXT NOT NULL, ad_account TEXT,
            spend REAL NOT NULL, currency TEXT, synced_at INTEGER NOT NULL, PRIMARY KEY (campaign_id, day))""",
        """CREATE TABLE IF NOT EXISTS spend_sync (
            campaign_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, synced_through TEXT, last_ok INTEGER,
            fails INTEGER NOT NULL DEFAULT 0, next_try INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            gone INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0)""",
        "CREATE TABLE IF NOT EXISTS ad_account_tz (ad_account TEXT PRIMARY KEY, tz TEXT NOT NULL)",
    ]

    def __init__(self, db):
        self.db = db
        for q in self.SCHEMA:
            self.db.execute(q)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(spend_sync)").fetchall()}
        for col, typ in (("ad_account", "TEXT"), ("closed_day", "TEXT")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE spend_sync ADD COLUMN {col} {typ}")
        self.db.commit()

    def set_tz(self, ad_account: str, tz: str) -> None:
        if ad_account and tz:
            self.db.execute("INSERT INTO ad_account_tz (ad_account, tz) VALUES (?,?) ON CONFLICT(ad_account)"
                            " DO UPDATE SET tz=excluded.tz", (ad_account, tz))
            self.db.commit()

    def tz(self, ad_account: str) -> Optional[str]:
        r = self.db.execute("SELECT tz FROM ad_account_tz WHERE ad_account=?", (ad_account,)).fetchone()
        return r[0] if r else None

    def days(self, campaign_id: str, since: str, until: str) -> list[dict]:
        rows = self.db.execute("SELECT day, spend, currency FROM spend_ledger WHERE campaign_id=? AND day>=? AND day<=?"
                               " ORDER BY day", (campaign_id, since, until)).fetchall()
        return [{"day": d, "spend": sp, "currency": c or ""} for d, sp, c in rows]

    # ---- writing
    def record(self, client_id: str, campaign_id: str, ad_account: str, rows: list[dict]) -> None:
        now = int(time.time())
        for r in rows:
            d = r.get("date_start") or r.get("day")
            if not d:
                continue
            self.db.execute(
                "INSERT INTO spend_ledger (campaign_id, day, client_id, ad_account, spend, currency, synced_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(campaign_id, day) DO UPDATE SET spend=excluded.spend,"
                " currency=excluded.currency, synced_at=excluded.synced_at",
                (campaign_id, d, client_id, ad_account, round(float(r.get("spend") or 0), 2),
                 r.get("account_currency") or "", now))
        self.db.commit()

    def synced(self, client_id: str, campaign_id: str, through: str, ad_account: str = "") -> None:
        now = int(time.time())
        self.db.execute(
            "INSERT INTO spend_sync (campaign_id, client_id, synced_through, last_ok, fails, next_try, last_error, closed,"
            " closed_day, ad_account) VALUES (?,?,?,?,0,?,NULL,0,NULL,?) ON CONFLICT(campaign_id) DO UPDATE SET"
            " synced_through=excluded.synced_through, last_ok=excluded.last_ok, fails=0, next_try=excluded.next_try,"
            " last_error=NULL, closed=0, closed_day=NULL, ad_account=COALESCE(NULLIF(excluded.ad_account,''), ad_account)",
            (campaign_id, client_id, through, now, now + FRESH_FOR, ad_account))
        self.db.commit()

    def failed(self, client_id: str, campaign_id: str, error: str, rate_limited: bool = False) -> None:
        st = self.state(campaign_id) or {}
        fails = (st.get("fails") or 0) + 1
        wait = min(24 * 3600, (3600 if rate_limited else 900) * 2 ** min(fails - 1, 6))
        self.db.execute(
            "INSERT INTO spend_sync (campaign_id, client_id, fails, next_try, last_error) VALUES (?,?,?,?,?)"
            " ON CONFLICT(campaign_id) DO UPDATE SET fails=excluded.fails, next_try=excluded.next_try,"
            " last_error=excluded.last_error", (campaign_id, client_id, fails, int(time.time()) + wait, error[:300]))
        self.db.commit()

    def mark(self, client_id: str, campaign_id: str, gone: bool = False, closed: bool = False,
             closed_day: Optional[str] = None) -> None:
        """gone: deleted on Meta for good. closed: verified paused when Facebook was disconnected (on closed_day)."""
        self.db.execute("INSERT INTO spend_sync (campaign_id, client_id, gone, closed, closed_day) VALUES (?,?,?,?,?)"
                        " ON CONFLICT(campaign_id) DO UPDATE SET gone=MAX(gone, excluded.gone),"
                        " closed=MAX(closed, excluded.closed), closed_day=COALESCE(excluded.closed_day, closed_day)",
                        (campaign_id, client_id, int(gone), int(closed), closed_day))
        self.db.commit()

    def forget_client(self, client_id: str) -> None:
        self.db.execute("DELETE FROM spend_ledger WHERE client_id=?", (client_id,))
        self.db.execute("DELETE FROM spend_sync WHERE client_id=?", (client_id,))
        self.db.commit()

    # ---- reading
    def state(self, campaign_id: str) -> Optional[dict]:
        keys = ("client_id", "synced_through", "last_ok", "fails", "next_try", "last_error", "gone", "closed",
                "closed_day", "ad_account")
        r = self.db.execute(f"SELECT {','.join(keys)} FROM spend_sync WHERE campaign_id=?", (campaign_id,)).fetchone()
        return dict(zip(keys, r)) if r else None

    def uncovered(self, campaign_ids: list[str], until_day: str) -> list[str]:
        """Campaigns whose spend isn't known through until_day (and weren't deleted or verified paused)."""
        out = []
        for cid in campaign_ids:
            st = self.state(cid)
            if st and (st["gone"] or st["closed"]):
                continue
            if not st or not st["synced_through"] or st["synced_through"] < until_day:
                out.append(cid)
        return out

    def total(self, campaign_ids: list[str], since_day: str, until_day: str) -> tuple[float, str]:
        if not campaign_ids:
            return 0.0, ""
        marks = ",".join("?" * len(campaign_ids))
        rows = self.db.execute(f"SELECT spend, currency FROM spend_ledger WHERE campaign_id IN ({marks})"
                               " AND day >= ? AND day <= ?", (*campaign_ids, since_day, until_day)).fetchall()
        currencies = {c for _, c in rows if c}
        if len(currencies) > 1:
            raise ValueError(f"campaigns in several currencies: {sorted(currencies)}")
        return round(sum(s for s, _ in rows), 2), (currencies.pop() if currencies else "")


def sync(pub, ledger: SpendLedger, client_id: str, launches: list[dict], ad_account: str,
         force: bool = False, today: Optional[str] = None, error_cls=Exception) -> dict:
    """Read new daily spend from Meta into the ledger. Respects back-off unless force (used at billing time)."""
    today = today or day(int(time.time()))
    now = int(time.time())
    done, skipped, failed = [], [], []
    if ad_account and not ledger.tz(ad_account) and hasattr(pub, "account"):
        try:
            ledger.set_tz(ad_account, (pub.account() or {}).get("timezone_name") or "")
        except error_cls:
            pass                                            # tried again next sync; settlement waits for it
    for l in launches:
        cid = l["campaign_id"]
        st = ledger.state(cid) or {}
        if st.get("gone") or (not force and st.get("next_try", 0) > now):
            skipped.append(cid)
            continue
        if st.get("synced_through"):
            since = (date.fromisoformat(st["synced_through"]) - timedelta(days=RESYNC_DAYS)).isoformat()
        else:
            since = (l.get("created_day") or (date.fromisoformat(today) - timedelta(days=90)).isoformat())
        since = min(since, today)
        try:
            rows = pub.insights([cid], since, today, daily=True)
        except error_cls as e:
            code = getattr(e, "code", None)
            if code in GONE_CODES or "does not exist" in str(e).lower():
                ledger.mark(client_id, cid, gone=True)
            else:
                ledger.failed(client_id, cid, str(e), rate_limited=code in RATE_LIMIT_CODES)
                failed.append(cid)
            continue
        ledger.record(client_id, cid, ad_account, rows)
        ledger.synced(client_id, cid, today, ad_account)
        done.append(cid)
    return {"synced": done, "skipped": skipped, "failed": failed}
