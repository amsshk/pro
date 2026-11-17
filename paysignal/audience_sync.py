"""Auto buyer audiences (Premium): ProAds keeps a "Buyers" customer list in the client's Meta ad account up to date,
plus a 1% lookalike, so ads can exclude people who already bought and find new people like them.

- Same buyers as the downloadable list (audiences.buyers): paid orders only, fully refunded customers left out,
  email and phone SHA-256 hashed in ProAds. Nothing readable leaves ProAds.
- Each sync REPLACES the audience's members (Meta's usersreplace), so refunded customers drop out too.
- Runs when turned on, then once a day. Meta needs the ad account's Custom Audience terms accepted once.
- The lookalike is created once the list has META_MIN buyers; Meta keeps it updated from the source list.
"""
from __future__ import annotations

import json
import random
import time
from typing import Optional

from .audiences import META_MIN

BATCH = 10_000
DAY = 86400
CURRENCY_COUNTRY = {"AED": "AE", "SAR": "SA", "QAR": "QA", "KWD": "KW", "BHD": "BH", "OMR": "OM", "EGP": "EG", "JOD": "JO",
                    "THB": "TH", "USD": "US", "GBP": "GB", "EUR": "DE", "INR": "IN", "MAD": "MA", "TRY": "TR"}


class SyncError(RuntimeError):
    pass


def country_for(currency: str) -> str:
    return CURRENCY_COUNTRY.get((currency or "").upper(), "AE")


class AudienceSyncStore:
    COLS = ("client_id", "ad_account", "audience_id", "lookalike_id", "country", "enabled", "last_sync", "last_count", "last_error")

    def __init__(self, db):
        self.db = db
        self.db.execute("""CREATE TABLE IF NOT EXISTS meta_audience_sync (client_id TEXT PRIMARY KEY, ad_account TEXT,
            audience_id TEXT, lookalike_id TEXT, country TEXT, enabled INTEGER NOT NULL DEFAULT 0, last_sync INTEGER,
            last_count INTEGER, last_error TEXT)""")
        self.db.commit()

    def get(self, client_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT " + ",".join(self.COLS) + " FROM meta_audience_sync WHERE client_id=?", (client_id,)).fetchone()
        return dict(zip(self.COLS, r)) if r else None

    def save(self, client_id: str, **f) -> dict:
        row = self.get(client_id) or {c: None for c in self.COLS}
        row.update(f, client_id=client_id)
        row["enabled"] = int(bool(row["enabled"]))
        self.db.execute("INSERT OR REPLACE INTO meta_audience_sync VALUES (?,?,?,?,?,?,?,?,?)", tuple(row[c] for c in self.COLS))
        self.db.commit()
        return row

    def due(self, now: Optional[float] = None) -> list[str]:
        now = now or time.time()
        return [r[0] for r in self.db.execute(
            "SELECT client_id FROM meta_audience_sync WHERE enabled=1 AND (last_sync IS NULL OR last_sync < ?)", (int(now - DAY),))]


class MetaAudiences:
    def __init__(self, http, token: str, version: str = "v21.0"):
        if http is None:
            import requests
            http = requests
        self.http, self.token, self.version = http, token, version

    def _call(self, method: str, path: str, data: dict, step: str) -> dict:
        url = f"https://graph.facebook.com/{self.version}/{path}"
        form = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in data.items()}
        form["access_token"] = self.token
        r = self.http.post(url, data=form, timeout=90) if method == "POST" else self.http.get(url, params=form, timeout=30)
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code != 200 or "error" in body:
            e = body.get("error") or {}
            msg = e.get("error_user_msg") or e.get("message") or f"HTTP {r.status_code}"
            err = SyncError(f"Meta refused {step}: {msg}")
            err.code, err.subcode = e.get("code"), e.get("error_subcode")
            err.terms = "terms" in msg.lower() or e.get("error_subcode") in (1870034, 1870047, 1870090)
            err.gone = e.get("code") == 100 and "does not exist" in msg.lower()
            raise err
        return body

    def create_list(self, ad_account: str, name: str) -> str:
        return self._call("POST", f"act_{ad_account}/customaudiences", {
            "name": name, "subtype": "CUSTOM", "customer_file_source": "USER_PROVIDED_ONLY",
            "description": "Real buyers from ProAds: paid orders, refunds removed. Updated daily."}, "creating the buyer audience")["id"]

    def replace_members(self, audience_id: str, rows: list[dict]) -> int:
        data = [[r["email"] or "", r["phone"] or ""] for r in rows if r["email"] or r["phone"]]
        session = random.randint(10**9, 2**53)
        batches = [data[i:i + BATCH] for i in range(0, len(data), BATCH)] or [[]]
        received = 0
        for n, chunk in enumerate(batches, 1):
            res = self._call("POST", f"{audience_id}/usersreplace", {
                "session": {"session_id": session, "batch_seq": n, "last_batch_flag": n == len(batches),
                            "estimated_num_total": len(data)},
                "payload": {"schema": ["EMAIL", "PHONE"], "data": chunk}}, "updating the buyer audience")
            received += int(res.get("num_received") or len(chunk))
        return received

    def create_lookalike(self, ad_account: str, source_id: str, country: str, name: str) -> str:
        return self._call("POST", f"act_{ad_account}/customaudiences", {
            "name": name, "subtype": "LOOKALIKE", "origin_audience_id": source_id,
            "lookalike_spec": {"type": "similarity", "ratio": 0.01, "country": country}}, "creating the lookalike")["id"]


def terms_link(ad_account: str) -> str:
    return f"https://business.facebook.com/ads/manage/customaudiences/tos/?act={ad_account}"


def sync(api: MetaAudiences, store: AudienceSyncStore, client_id: str, ad_account: str, rows: list[dict],
         business: str, country: str) -> dict:
    """Create the audience if needed, replace its members with today's buyers, add the lookalike when big enough."""
    row = store.get(client_id) or {}
    audience = row.get("audience_id") if row.get("ad_account") == ad_account else None
    lookalike = row.get("lookalike_id") if audience else None
    note = None
    try:
        if not audience:
            audience = api.create_list(ad_account, f"ProAds buyers · {business}"[:100])
        try:
            sent = api.replace_members(audience, rows)
        except SyncError as e:
            if not e.gone:
                raise
            audience, lookalike = api.create_list(ad_account, f"ProAds buyers · {business}"[:100]), None   # deleted in Meta
            sent = api.replace_members(audience, rows)
        if not lookalike and len(rows) >= META_MIN:
            try:
                lookalike = api.create_lookalike(ad_account, audience, country, f"ProAds lookalike 1% {country} · {business}"[:100])
            except SyncError as e:                       # e.g. the list is still being matched: try again tomorrow
                note = str(e)
    except SyncError as e:
        msg = (f"Accept Meta's Custom Audience terms for this ad account once, then press Sync now: {terms_link(ad_account)}"
               if getattr(e, "terms", False) else str(e))
        store.save(client_id, ad_account=ad_account, audience_id=audience, lookalike_id=lookalike, country=country,
                   enabled=True, last_sync=int(time.time()), last_error=msg)
        return {"ok": False, "error": msg, "terms_link": terms_link(ad_account) if getattr(e, "terms", False) else None}
    store.save(client_id, ad_account=ad_account, audience_id=audience, lookalike_id=lookalike, country=country, enabled=True,
               last_sync=int(time.time()), last_count=sent, last_error=note)
    return {"ok": True, "sent": sent, "audience_id": audience, "lookalike_id": lookalike, "note": note}


def view(row: Optional[dict], buyers: int) -> dict:
    row = row or {}
    acct = row.get("ad_account")
    link = lambda aid: f"https://adsmanager.facebook.com/adsmanager/audiences?act={acct}&ids={aid}" if aid and acct else None
    return {"enabled": bool(row.get("enabled")), "buyers": buyers, "country": row.get("country"),
            "audience_id": row.get("audience_id"), "lookalike_id": row.get("lookalike_id"),
            "audience_link": link(row.get("audience_id")), "lookalike_link": link(row.get("lookalike_id")),
            "last_sync": row.get("last_sync"), "last_count": row.get("last_count"), "last_error": row.get("last_error"),
            "lookalike_min": META_MIN}
