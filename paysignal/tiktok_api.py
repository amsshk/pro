"""TikTok Marketing API (ProAds' approved developer app): connect a business's TikTok ad account, read spend and
results per ad, and (with the owner's approval) change budgets or pause.

Setup on the server: TIKTOK_APP_ID and TIKTOK_APP_SECRET (from business-api.tiktok.com > My Apps).
Connect flow: ProAds -> TikTok's approval page -> /tiktok/callback?auth_code=...&state=... -> access token for
the ad accounts the owner ticked. Tokens are stored encrypted. Every call sends the token in the Access-Token header.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional
from urllib.parse import urlencode

API = "https://business-api.tiktok.com/open_api/v1.3"
AUTH = "https://business-api.tiktok.com/portal/auth"
STATE_TTL = 1800
METRICS = ("spend", "impressions", "clicks", "campaign_name", "campaign_id", "adgroup_name", "adgroup_id", "ad_name")


class TikTokAPIError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


def settings() -> dict:
    return {"app_id": os.environ.get("TIKTOK_APP_ID", "").strip(), "secret": os.environ.get("TIKTOK_APP_SECRET", "").strip()}


def available() -> bool:
    s = settings()
    return bool(s["app_id"] and s["secret"])


def authorize_url(state: str, redirect_uri: str) -> str:
    return AUTH + "?" + urlencode({"app_id": settings()["app_id"], "state": state, "redirect_uri": redirect_uri})


TOKEN_CODES = {40001, 40100, 40102, 40104, 40105, 40106}      # missing / invalid / expired / revoked token


def _check(r, what: str) -> dict:
    try:
        body = r.json()
    except ValueError:
        raise TikTokAPIError(f"TikTok sent an unreadable answer while {what} (HTTP {r.status_code}).")
    code = body.get("code")
    if r.status_code != 200 or code != 0:
        msg = body.get("message") or f"HTTP {r.status_code}"
        raise TikTokAPIError(f"TikTok refused {what}: {msg}", int(code or 0))
    return body.get("data") or {}


def exchange_code(http, auth_code: str) -> dict:
    s = settings()
    r = http.post(API + "/oauth2/access_token/", json={"app_id": s["app_id"], "secret": s["secret"], "auth_code": auth_code},
                  timeout=30)
    d = _check(r, "connecting the ad account")
    if not d.get("access_token"):
        raise TikTokAPIError("TikTok didn't return an access token.")
    exp = d.get("expires_in")
    return {"access_token": d["access_token"], "refresh_token": d.get("refresh_token") or "",
            "expires_at": int(time.time() + int(exp)) if exp else 0,
            "advertiser_ids": [str(a) for a in d.get("advertiser_ids") or []], "scope": d.get("scope") or []}


def refresh(http, refresh_token: str) -> dict:
    """Only used if TikTok gave a refresh token (most advertiser tokens don't expire until revoked)."""
    s = settings()
    r = http.post(API + "/oauth2/refresh_token/", json={"app_id": s["app_id"], "secret": s["secret"],
                                                       "refresh_token": refresh_token, "grant_type": "refresh_token"}, timeout=30)
    d = _check(r, "renewing the connection")
    exp = d.get("expires_in")
    return {"access_token": d["access_token"], "refresh_token": d.get("refresh_token") or refresh_token,
            "expires_at": int(time.time() + int(exp)) if exp else 0}


def _get(http, token: str, path: str, params: dict, what: str) -> dict:
    q = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in params.items()}
    r = http.get(f"{API}/{path}/", params=q, headers={"Access-Token": token}, timeout=60)
    return _check(r, what)


def _post(http, token: str, path: str, body: dict, what: str) -> dict:
    r = http.post(f"{API}/{path}/", json=body, headers={"Access-Token": token, "Content-Type": "application/json"}, timeout=60)
    return _check(r, what)


def advertisers(http, token: str, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    d = _get(http, token, "advertiser/info", {"advertiser_ids": ids, "fields": ["advertiser_id", "name", "currency", "timezone", "status"]},
             "reading the ad accounts")
    rows = d.get("list") if isinstance(d, dict) else d
    return [{"id": str(a.get("advertiser_id")), "name": a.get("name") or str(a.get("advertiser_id")),
             "currency": (a.get("currency") or "").upper(), "timezone": a.get("timezone") or "", "status": a.get("status") or ""}
            for a in rows or []]


MAX_SPAN_DAYS = 28          # TikTok: "max time span is 30 days when use stat_time_day" (kept under it)


def report(http, token: str, advertiser_id: str, start: str, end: str) -> list[dict]:
    """Daily spend, impressions and clicks per ad (with campaign / ad group names), in 30-day pieces."""
    from datetime import date, timedelta
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while a <= b:
        piece_end = min(b, a + timedelta(days=MAX_SPAN_DAYS - 1))
        out += _report_piece(http, token, advertiser_id, a.isoformat(), piece_end.isoformat())
        a = piece_end + timedelta(days=1)
    return out


def _report_piece(http, token: str, advertiser_id: str, start: str, end: str) -> list[dict]:
    out, page = [], 1
    while True:
        d = _get(http, token, "report/integrated/get", {
            "advertiser_id": advertiser_id, "report_type": "BASIC", "data_level": "AUCTION_AD",
            "dimensions": ["ad_id", "stat_time_day"], "metrics": list(METRICS),
            "start_date": start, "end_date": end, "page": page, "page_size": 1000}, "reading ad results")
        for row in d.get("list") or []:
            dim, m = row.get("dimensions") or {}, row.get("metrics") or {}
            def num(k):
                try:
                    return float(m.get(k) or 0)
                except (TypeError, ValueError):
                    return 0.0
            out.append({"date": str(dim.get("stat_time_day") or "")[:10], "ad_id": str(dim.get("ad_id") or ""),
                        "campaign_id": str(m.get("campaign_id") or ""), "campaign": m.get("campaign_name") or "",
                        "ad_group_id": str(m.get("adgroup_id") or ""), "ad_group": m.get("adgroup_name") or "",
                        "ad_name": m.get("ad_name") or "", "spend": num("spend"),
                        "impressions": int(num("impressions")), "clicks": int(num("clicks"))})
        info = d.get("page_info") or {}
        if page >= int(info.get("total_page") or 1) or page >= 50:
            break
        page += 1
    return [r for r in out if r["ad_id"] and r["date"]]


def set_campaign_status(http, token: str, advertiser_id: str, campaign_id: str, enable: bool) -> None:
    _post(http, token, "campaign/status/update", {"advertiser_id": advertiser_id, "campaign_ids": [campaign_id],
                                                  "operation_status": "ENABLE" if enable else "DISABLE"},
          "changing the campaign status")


class TikTokAdsStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS tiktok_ads_links (client_id TEXT PRIMARY KEY, token_enc TEXT NOT NULL,
        refresh_enc TEXT, expires_at INTEGER, advertisers TEXT NOT NULL, advertiser_id TEXT, currency TEXT,
        connected_at INTEGER NOT NULL, last_sync INTEGER, status TEXT, message TEXT)"""
    KEYS = ("client_id", "token_enc", "refresh_enc", "expires_at", "advertisers", "advertiser_id", "currency",
            "connected_at", "last_sync", "status", "message")

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def save(self, client_id: str, token_enc: str, refresh_enc: str, expires_at: int, advertisers: list[dict]) -> None:
        one = advertisers[0] if len(advertisers) == 1 else None
        self.db.execute("INSERT INTO tiktok_ads_links (client_id, token_enc, refresh_enc, expires_at, advertisers, advertiser_id,"
                        " currency, connected_at, status) VALUES (?,?,?,?,?,?,?,?,'connected') ON CONFLICT(client_id) DO UPDATE SET"
                        " token_enc=excluded.token_enc, refresh_enc=excluded.refresh_enc, expires_at=excluded.expires_at,"
                        " advertisers=excluded.advertisers, advertiser_id=excluded.advertiser_id, currency=excluded.currency,"
                        " connected_at=excluded.connected_at, status='connected', message=NULL",
                        (client_id, token_enc, refresh_enc or None, expires_at or None, json.dumps(advertisers),
                         one["id"] if one else None, one["currency"] if one else None, int(time.time())))
        self.db.commit()

    def get(self, client_id: str) -> Optional[dict]:
        r = self.db.execute(f"SELECT {', '.join(self.KEYS)} FROM tiktok_ads_links WHERE client_id=?", (client_id,)).fetchone()
        if not r:
            return None
        d = dict(zip(self.KEYS, r))
        d["advertisers"] = json.loads(d["advertisers"] or "[]")
        return d

    def all(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT client_id FROM tiktok_ads_links WHERE advertiser_id IS NOT NULL")]

    def choose(self, client_id: str, advertiser_id: str) -> bool:
        row = self.get(client_id)
        hit = next((a for a in (row or {}).get("advertisers", []) if a["id"] == advertiser_id), None)
        if not hit:
            return False
        self.db.execute("UPDATE tiktok_ads_links SET advertiser_id=?, currency=?, last_sync=NULL WHERE client_id=?",
                        (advertiser_id, hit["currency"], client_id))
        self.db.commit()
        return True

    def tokens(self, client_id: str, token_enc: str, refresh_enc: Optional[str], expires_at: int) -> None:
        self.db.execute("UPDATE tiktok_ads_links SET token_enc=?, refresh_enc=?, expires_at=? WHERE client_id=?",
                        (token_enc, refresh_enc, expires_at or None, client_id))
        self.db.commit()

    def result(self, client_id: str, status: str, message: Optional[str]) -> None:
        self.db.execute("UPDATE tiktok_ads_links SET last_sync=?, status=?, message=? WHERE client_id=?",
                        (int(time.time()), status, message, client_id))
        self.db.commit()

    def remove(self, client_id: str) -> None:
        self.db.execute("DELETE FROM tiktok_ads_links WHERE client_id=?", (client_id,))
        self.db.commit()

    @staticmethod
    def public(row: Optional[dict]) -> dict:
        if not row:
            return {"connected": False, "available": available()}
        return {"connected": True, "available": available(), "advertisers": row["advertisers"],
                "advertiser_id": row["advertiser_id"], "currency": row["currency"], "status": row["status"],
                "message": row["message"], "last_sync": row["last_sync"]}
