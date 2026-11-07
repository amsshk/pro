"""Clients connect their own Facebook & Instagram business with Facebook Login for Business.

After they approve on Facebook's own screen, ProAds keeps their access token
(encrypted) and the ad account, Page, Instagram account and Pixel they pick.
That lets ProAds:
  - publish campaigns into the client's own ad account (paused until they go live)
  - send the client's real sale values to the client's own Pixel (Conversions API)

Setup (once, by the ProAds owner) in the Meta app at developers.facebook.com:
  1. Add "Facebook Login for Business". Valid OAuth redirect URI:
         https://<your domain>/meta/callback
  2. Create a Configuration (User access token) with: ads_management, ads_read,
     business_management, pages_show_list, pages_read_engagement, pages_manage_ads.
  3. Server settings: META_APP_ID, META_APP_SECRET, META_LOGIN_CONFIG_ID,
     PROADS_SECRET_KEY, PROADS_PUBLIC_URL.
  Until Meta approves those permissions for Advanced Access (App Review +
  Business Verification), only people with a role on the app can connect.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional
from urllib.parse import urlencode

from .shopify_app import decrypt, encrypt

log = logging.getLogger("proads.meta_connect")

STATE_TTL = 900
SCOPES = "ads_management,ads_read,business_management,pages_show_list,pages_read_engagement,pages_manage_ads"


class ConnectError(Exception):
    pass


def settings() -> dict:
    env = lambda k: os.environ.get(k, "").strip()
    return {"app_id": env("META_APP_ID"), "app_secret": env("META_APP_SECRET"),
            "config_id": env("META_LOGIN_CONFIG_ID"), "version": env("PROADS_META_VERSION") or "v25.0"}


def available() -> bool:
    s = settings()
    return bool(s["app_id"] and s["app_secret"] and s["config_id"] and os.environ.get("PROADS_SECRET_KEY"))


def authorize_url(state: str, redirect_uri: str) -> str:
    s = settings()
    q = {"client_id": s["app_id"], "redirect_uri": redirect_uri, "state": state, "response_type": "code",
         "config_id": s["config_id"], "override_default_response_type": "true"}
    return f"https://www.facebook.com/{s['version']}/dialog/oauth?" + urlencode(q)


class Graph:
    def __init__(self, http=None, version: Optional[str] = None):
        if http is None:
            import requests
            http = requests
        self.http, self.version = http, version or settings()["version"]

    def get(self, path: str, params: dict) -> dict:
        r = self.http.get(f"https://graph.facebook.com/{self.version}/{path}", params=params, timeout=30)
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code != 200 or "error" in body:
            e = body.get("error") or {}
            raise ConnectError(e.get("error_user_msg") or e.get("message") or f"HTTP {r.status_code}")
        return body

    def pages(self, path: str, params: dict, limit: int = 300) -> list[dict]:
        out, body = [], self.get(path, {**params, "limit": 100})
        while True:
            out += body.get("data") or []
            nxt = (body.get("paging") or {}).get("next")
            if not nxt or len(out) >= limit:
                return out[:limit]
            r = self.http.get(nxt, timeout=30)
            body = r.json() if r.status_code == 200 else {}


def exchange_code(http, code: str, redirect_uri: str) -> dict:
    """Code -> access token, upgraded to a long-lived one when Meta allows it."""
    s, g = settings(), Graph(http)
    tok = g.get("oauth/access_token", {"client_id": s["app_id"], "client_secret": s["app_secret"],
                                         "redirect_uri": redirect_uri, "code": code})
    try:
        long = g.get("oauth/access_token", {"grant_type": "fb_exchange_token", "client_id": s["app_id"],
                                            "client_secret": s["app_secret"], "fb_exchange_token": tok["access_token"]})
        tok = long if long.get("access_token") else tok
    except ConnectError:
        pass                                    # business tokens can't be exchanged and don't need it
    exp = tok.get("expires_in")
    return {"access_token": tok["access_token"], "expires_at": int(time.time() + int(exp)) if exp else None}


def discover(http, token: str) -> dict:
    """What this person can advertise with: ad accounts, Pages (+ their Instagram), their name."""
    g = Graph(http)
    me = g.get("me", {"fields": "id,name", "access_token": token})
    acct_fields, page_fields = "id,name,currency,account_status", "id,name,instagram_business_account{id,username}"
    accounts = g.pages("me/adaccounts", {"fields": acct_fields, "access_token": token})
    pages = g.pages("me/accounts", {"fields": page_fields, "access_token": token})
    # Pages and ad accounts owned by a Business portfolio often don't show up under /me: ask each business too
    try:
        businesses = g.pages("me/businesses", {"fields": "id,name", "access_token": token}, limit=20)
    except ConnectError:
        businesses = []
    for b in businesses:
        for edge, into, fields in (("owned_pages", pages, page_fields), ("client_pages", pages, page_fields),
                                   ("owned_ad_accounts", accounts, acct_fields),
                                   ("client_ad_accounts", accounts, acct_fields)):
            try:
                into += g.pages(f"{b['id']}/{edge}", {"fields": fields, "access_token": token}, limit=100)
            except ConnectError:
                continue
    def dedupe(xs):
        seen = {}
        for x in xs:                     # keep the most complete copy (e.g. the one with its Instagram)
            if x["id"] not in seen or (x.get("instagram_business_account") and not seen[x["id"]].get("instagram_business_account")):
                seen[x["id"]] = x
        return list(seen.values())
    accounts, pages = dedupe(accounts), dedupe(pages)
    try:
        perms = g.pages("me/permissions", {"access_token": token}, limit=100)
        granted = {p["permission"] for p in perms if p.get("status") == "granted"}
    except ConnectError:
        granted = None
    return {
        "user": me.get("name", ""),
        "businesses": [b["id"] for b in businesses],
        "missing_permissions": sorted(set(SCOPES.split(",")) - granted) if granted is not None else [],
        "ad_accounts": [{"id": a["id"], "name": a.get("name") or a["id"], "currency": a.get("currency", ""),
                         "active": a.get("account_status") == 1} for a in accounts],
        "pages": [{"id": p["id"], "name": p.get("name") or p["id"],
                   "instagram_id": (p.get("instagram_business_account") or {}).get("id", ""),
                   "instagram": (p.get("instagram_business_account") or {}).get("username", "")} for p in pages],
    }


def pixels(http, token: str, ad_account: str, businesses: Optional[list] = None) -> list[dict]:
    """Pixels of the ad account, plus the ones owned by or shared with the person's businesses."""
    g, rows, errors = Graph(http), [], []
    for path in [f"{ad_account}/adspixels"] + [f"{b}/{e}" for b in businesses or [] for e in ("owned_pixels", "client_pixels")]:
        try:
            rows += g.pages(path, {"fields": "id,name", "access_token": token}, limit=50)
        except ConnectError as e:
            errors.append(str(e))
    if not rows and errors and not businesses:
        raise ConnectError(errors[0])
    return list({p["id"]: {"id": p["id"], "name": p.get("name") or p["id"]} for p in rows}.values())


def lookup_page(http, token: str, page_id: str) -> dict:
    """A Page typed by ID: check this person can use it."""
    p = Graph(http).get(page_id, {"fields": "id,name,instagram_business_account{id,username}", "access_token": token})
    ig = p.get("instagram_business_account") or {}
    return {"id": p["id"], "name": p.get("name") or p["id"], "instagram_id": ig.get("id", ""), "instagram": ig.get("username", "")}


class ConnectionStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS meta_connections (
        client_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at REAL, user_name TEXT,
        assets TEXT NOT NULL, ad_account TEXT, page_id TEXT, instagram_id TEXT, pixel_id TEXT,
        send_sales INTEGER NOT NULL DEFAULT 1, connected_at TEXT NOT NULL DEFAULT (datetime('now')))"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def save(self, client_id: str, token: dict, found: dict) -> dict:
        """New connection; picks the ad account / Page automatically when there is only one."""
        pick = lambda xs: xs[0] if len(xs) == 1 else None
        acct = pick([a for a in found["ad_accounts"] if a["active"]] or found["ad_accounts"])
        page = pick(found["pages"])
        self.db.execute("DELETE FROM meta_connections WHERE client_id=?", (client_id,))
        self.db.execute("INSERT INTO meta_connections (client_id, token, expires_at, user_name, assets, ad_account,"
                        " page_id, instagram_id) VALUES (?,?,?,?,?,?,?,?)",
                        (client_id, encrypt(token["access_token"]), token.get("expires_at"), found["user"],
                         json.dumps({"ad_accounts": found["ad_accounts"], "pages": found["pages"],
                                     "businesses": found.get("businesses", []),
                                     "missing_permissions": found.get("missing_permissions", [])}),
                         acct["id"] if acct else None, page["id"] if page else None,
                         (page or {}).get("instagram_id") or None))
        self.db.commit()
        return self.get(client_id)

    def mark_expired(self, client_id: str) -> None:
        """Facebook killed the session: show 'Reconnect Facebook' instead of failing on every read."""
        self.db.execute("UPDATE meta_connections SET expires_at=? WHERE client_id=?", (time.time() - 1, client_id))
        self.db.commit()

    def get(self, client_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT token, expires_at, user_name, assets, ad_account, page_id, instagram_id,"
                              " pixel_id, send_sales, connected_at FROM meta_connections WHERE client_id=?",
                              (client_id,)).fetchone()
        if not row:
            return None
        keys = ("token", "expires_at", "user_name", "assets", "ad_account", "page_id", "instagram_id", "pixel_id",
                "send_sales", "connected_at")
        c = dict(zip(keys, row))
        c["assets"] = json.loads(c["assets"])
        c["send_sales"] = bool(c["send_sales"])
        return c

    def token(self, client_id: str) -> Optional[str]:
        c = self.get(client_id)
        return decrypt(c["token"]) if c else None

    def add_page(self, client_id: str, page: dict) -> None:
        c = self.get(client_id)
        if c and page["id"] not in {p["id"] for p in c["assets"]["pages"]}:
            c["assets"]["pages"].append(page)
            self.db.execute("UPDATE meta_connections SET assets=? WHERE client_id=?", (json.dumps(c["assets"]), client_id))
            self.db.commit()

    def select(self, client_id: str, ad_account: str, page_id: str, pixel_id: str, send_sales: bool) -> None:
        c = self.get(client_id)
        if not c:
            raise ConnectError("Facebook isn't connected")
        accts = {a["id"] for a in c["assets"]["ad_accounts"]}
        pages = {p["id"]: p for p in c["assets"]["pages"]}
        if ad_account and ad_account not in accts:
            raise ConnectError("that ad account isn't one you connected")
        if page_id and page_id not in pages:
            raise ConnectError("that Page isn't one you connected")
        ig = pages[page_id]["instagram_id"] if page_id else None
        self.db.execute("UPDATE meta_connections SET ad_account=?, page_id=?, instagram_id=?, pixel_id=?, send_sales=?"
                        " WHERE client_id=?", (ad_account or None, page_id or None, ig or None, pixel_id or None,
                                               1 if send_sales else 0, client_id))
        self.db.commit()

    def delete(self, client_id: str) -> None:
        self.db.execute("DELETE FROM meta_connections WHERE client_id=?", (client_id,))
        self.db.commit()

    def all_clients(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT client_id FROM meta_connections").fetchall()]


def public_view(c: Optional[dict]) -> dict:
    """What the browser may see: names and IDs, never the token."""
    if not c:
        return {"connected": False, "available": available()}
    accts = {a["id"]: a for a in c["assets"]["ad_accounts"]}
    pages = {p["id"]: p for p in c["assets"]["pages"]}
    expired = bool(c["expires_at"] and c["expires_at"] < time.time())
    return {"connected": True, "available": available(), "user": c["user_name"], "connected_at": c["connected_at"],
            "expires_at": c["expires_at"], "expired": expired,
            "ad_accounts": c["assets"]["ad_accounts"], "pages": c["assets"]["pages"],
            "missing_permissions": c["assets"].get("missing_permissions", []),
            "ad_account": c["ad_account"], "page_id": c["page_id"], "instagram_id": c["instagram_id"],
            "pixel_id": c["pixel_id"], "send_sales": c["send_sales"],
            "ad_account_name": (accts.get(c["ad_account"]) or {}).get("name"),
            "currency": (accts.get(c["ad_account"]) or {}).get("currency"),
            "page_name": (pages.get(c["page_id"]) or {}).get("name"),
            "ready": bool(c["ad_account"] and c["page_id"] and c["pixel_id"] and not expired)}


def ads_settings(c: dict, token: str) -> dict:
    """Settings for meta_ads.Publisher from a client's connection."""
    return {"ad_account": c["ad_account"] or "", "page_id": c["page_id"] or "", "instagram_id": c["instagram_id"] or "",
            "token": token, "pixel_id": c["pixel_id"] or "", "version": settings()["version"]}
