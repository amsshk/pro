"""TikTok ad spend and results through Windsor.ai, for businesses that already connected TikTok to Windsor.

Windsor holds TikTok's reporting permission, so ProAds can read each TikTok ad's daily spend before ProAds' own
TikTok app is approved. ProAds puts that spend next to VERIFIED paid sales from the same ad (the ProAds TikTok link
tags each order with the ad ID), so every ad gets a real cost per paid order, not TikTok's own conversion count.

The business pastes its Windsor API key once (stored encrypted, never shown again). Read-only: ProAds never
changes anything through Windsor.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional

URL = "https://connectors.windsor.ai/tiktok"
FIELDS = ("date,account_id,account_name,currency,campaign,campaign_id,ad_group_name,ad_group_id,"
          "ad_name,ad_id,spend,impressions,clicks")
HISTORY_DAYS = 90
SYNC_EVERY = 3600


class WindsorError(Exception):
    pass


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch(http, key: str, date_from: str, date_to: str, accounts: Optional[str] = None) -> list[dict]:
    params = {"api_key": key, "fields": FIELDS, "date_from": date_from, "date_to": date_to}
    if accounts:
        params["select_accounts"] = accounts
    try:
        r = http.get(URL, params=params, timeout=60)
    except Exception as e:
        raise WindsorError(f"Couldn't reach Windsor.ai ({type(e).__name__}). ProAds will try again shortly.") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if r.status_code in (401, 403) or (isinstance(err, str) and "authori" in err.lower()):
        raise WindsorError("Windsor.ai didn't accept this API key. In Windsor.ai, copy the API key again (it's on your account page) and paste it here.")
    if r.status_code == 429:
        raise WindsorError("Windsor.ai asked ProAds to slow down. It will try again later.")
    if r.status_code != 200 or err:
        msg = (err.get("message") if isinstance(err, dict) else err) or f"HTTP {r.status_code}"
        raise WindsorError(f"Windsor.ai refused: {msg}")
    out = []
    for row in body.get("data") or []:
        if not row.get("ad_id") or not row.get("date"):
            continue
        out.append({"date": str(row["date"])[:10], "account_id": str(row.get("account_id") or ""),
                    "account_name": row.get("account_name") or "", "currency": (row.get("currency") or "").upper(),
                    "campaign_id": str(row.get("campaign_id") or ""), "campaign": row.get("campaign") or "",
                    "ad_group_id": str(row.get("ad_group_id") or ""), "ad_group": row.get("ad_group_name") or "",
                    "ad_id": str(row["ad_id"]), "ad_name": row.get("ad_name") or "",
                    "spend": _num(row.get("spend")), "impressions": int(_num(row.get("impressions"))),
                    "clicks": int(_num(row.get("clicks")))})
    return out


class WindsorStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS windsor_links (client_id TEXT PRIMARY KEY, key_enc TEXT NOT NULL, accounts TEXT,
        connected_at INTEGER NOT NULL, last_sync INTEGER, status TEXT, message TEXT, rows INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tiktok_spend (client_id TEXT NOT NULL, day TEXT NOT NULL, ad_id TEXT NOT NULL,
        account_id TEXT, currency TEXT, campaign_id TEXT, campaign TEXT, ad_group_id TEXT, ad_group TEXT, ad_name TEXT,
        spend REAL NOT NULL DEFAULT 0, impressions INTEGER NOT NULL DEFAULT 0, clicks INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (client_id, day, ad_id));
    """

    def __init__(self, db):
        self.db = db
        self.db.executescript(self.SCHEMA)
        self.db.commit()

    def save(self, client_id: str, key_enc: str, accounts: Optional[str]) -> None:
        self.db.execute("INSERT INTO windsor_links (client_id, key_enc, accounts, connected_at, status) VALUES (?,?,?,?,"
                        "'syncing') ON CONFLICT(client_id) DO UPDATE SET key_enc=excluded.key_enc, accounts=excluded.accounts,"
                        " connected_at=excluded.connected_at, last_sync=NULL, status='syncing', message=NULL",
                        (client_id, key_enc, accounts or None, int(time.time())))
        self.db.commit()

    def get(self, client_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT client_id, key_enc, accounts, connected_at, last_sync, status, message, rows"
                            " FROM windsor_links WHERE client_id=?", (client_id,)).fetchone()
        return dict(zip(("client_id", "key_enc", "accounts", "connected_at", "last_sync", "status", "message", "rows"), r)) if r else None

    def all(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT client_id FROM windsor_links")]

    def remove(self, client_id: str) -> None:
        self.db.execute("DELETE FROM windsor_links WHERE client_id=?", (client_id,))
        self.db.execute("DELETE FROM tiktok_spend WHERE client_id=?", (client_id,))
        self.db.commit()

    def result(self, client_id: str, ok: bool, message: Optional[str], rows: int = 0) -> None:
        if ok:
            self.db.execute("UPDATE windsor_links SET last_sync=?, status='ok', message=?, rows=? WHERE client_id=?",
                            (int(time.time()), message, rows, client_id))
        else:
            self.db.execute("UPDATE windsor_links SET last_sync=?, status='error', message=? WHERE client_id=?",
                            (int(time.time()), message, client_id))
        self.db.commit()

    def put(self, client_id: str, rows: list[dict]) -> None:
        self.db.executemany(
            "INSERT INTO tiktok_spend (client_id, day, ad_id, account_id, currency, campaign_id, campaign, ad_group_id,"
            " ad_group, ad_name, spend, impressions, clicks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(client_id, day, ad_id) DO UPDATE SET spend=excluded.spend, impressions=excluded.impressions,"
            " clicks=excluded.clicks, campaign=excluded.campaign, ad_group=excluded.ad_group, ad_name=excluded.ad_name,"
            " currency=excluded.currency",
            [(client_id, r["date"], r["ad_id"], r["account_id"], r["currency"], r["campaign_id"], r["campaign"],
              r["ad_group_id"], r["ad_group"], r["ad_name"], r["spend"], r["impressions"], r["clicks"]) for r in rows])
        self.db.commit()

    def spend_by_ad(self, client_id: str, since_day: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT ad_id, MAX(ad_name), MAX(campaign_id), MAX(campaign), MAX(ad_group), MAX(currency), SUM(spend),"
            " SUM(impressions), SUM(clicks), MIN(day), MAX(day) FROM tiktok_spend WHERE client_id=? AND day>=?"
            " GROUP BY ad_id", (client_id, since_day)).fetchall()
        keys = ("ad_id", "ad_name", "campaign_id", "campaign", "ad_group", "currency", "spend", "impressions", "clicks",
                "first_day", "last_day")
        return [dict(zip(keys, r)) for r in rows]

    @staticmethod
    def public(row: Optional[dict]) -> dict:
        if not row:
            return {"connected": False}
        return {"connected": True, "status": row["status"], "message": row["message"], "last_sync": row["last_sync"],
                "rows": row["rows"], "accounts": row["accounts"]}


def window(days: int, today: Optional[date] = None) -> tuple[str, str]:
    today = today or date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()
