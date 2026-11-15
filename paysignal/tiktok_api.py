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


# ---------- publishing (everything created with the campaign OFF until the owner presses Go live) ----------
# TikTok uses GeoNames IDs for countries
COUNTRY_IDS = {"AE": "290557", "SA": "102358", "KW": "285570", "QA": "289688", "BH": "290291", "OM": "286963",
               "EG": "357994", "JO": "248816", "LB": "272103", "MA": "2542007", "US": "6252001", "GB": "2635167",
               "DE": "2921044", "FR": "3017382", "IN": "1269750", "PK": "1168579", "TH": "1605651", "TR": "298795"}
EVENTS = {"add_to_cart": "ON_WEB_CART", "checkout": "INITIATE_ORDER", "purchase": "SHOPPING"}
CTAS = ("SHOP_NOW", "ORDER_NOW", "LEARN_MORE", "BUY_NOW", "GET_QUOTE", "SIGN_UP", "CONTACT_US", "BOOK_NOW", "APPLY_NOW")
AGE_GROUPS = ("AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100")
MIN_ADGROUP_DAILY_USD = 20.0            # TikTok's minimum daily budget per ad group


def pixels(http, token: str, advertiser_id: str) -> list[dict]:
    try:
        d = _get(http, token, "pixel/list", {"advertiser_id": advertiser_id, "page": 1, "page_size": 20}, "reading the Pixels")
    except TikTokAPIError:
        d = _get(http, token, "pixel/list", {"advertiser_id": advertiser_id}, "reading the Pixels")
    out = []
    for p in d.get("pixels") or d.get("list") or []:
        out.append({"pixel_id": str(p.get("pixel_id") or ""), "code": p.get("pixel_code") or "", "name": p.get("pixel_name") or p.get("name") or ""})
    return [p for p in out if p["pixel_id"]]


def _first(d, *keys):
    """TikTok answers some calls with an object and others with a one-item list: take the value either way."""
    items = d if isinstance(d, list) else (d.get("list") if isinstance(d, dict) and isinstance(d.get("list"), list) else [d])
    for it in items or []:
        for k in keys:
            if isinstance(it, dict) and it.get(k):
                return it[k]
    return None


def upload_video(http, token: str, advertiser_id: str, name: str, data: Optional[bytes] = None, url: Optional[str] = None) -> str:
    import hashlib
    fname = f"proads-{int(time.time() * 1000)}-{(name or 'video')[:40]}"
    if data is not None:
        r = http.post(f"{API}/file/video/ad/upload/", headers={"Access-Token": token}, timeout=300,
                      data={"advertiser_id": advertiser_id, "upload_type": "UPLOAD_BY_FILE", "file_name": fname,
                            "video_signature": hashlib.md5(data).hexdigest()},
                      files={"video_file": (fname + ".mp4", data, "video/mp4")})
        d = _check(r, "uploading the video")
    else:
        d = _post(http, token, "file/video/ad/upload", {"advertiser_id": advertiser_id, "upload_type": "UPLOAD_BY_URL",
                                                        "file_name": fname, "video_url": url}, "uploading the video")
    vid = _first(d, "video_id")
    if not vid:
        raise TikTokAPIError("TikTok didn't return a video ID after the upload.")
    return vid


def video_cover(http, token: str, advertiser_id: str, video_id: str) -> str:
    """TikTok video ads need a cover image: use the frame TikTok made, uploaded as an ad image."""
    info = {}
    for _ in range(6):                                   # TikTok needs a moment to process a fresh upload
        d = _get(http, token, "file/video/ad/info", {"advertiser_id": advertiser_id, "video_ids": [video_id]}, "reading the video")
        info = (d.get("list") or [{}])[0] if isinstance(d, dict) else {}
        if info.get("video_cover_url") or info.get("poster_url"):
            break
        time.sleep(5)
    cover = info.get("video_cover_url") or info.get("poster_url")
    if not cover:
        raise TikTokAPIError("TikTok is still processing the video. Try publishing again in a minute.")
    d = _post(http, token, "file/image/ad/upload", {"advertiser_id": advertiser_id, "upload_type": "UPLOAD_BY_URL",
                                                    "file_name": f"proads-cover-{video_id}-{int(time.time())}", "image_url": cover},
              "uploading the video cover")
    img = _first(d, "image_id")
    if not img:
        raise TikTokAPIError("TikTok didn't return an image ID for the video cover.")
    return img


def identity(http, token: str, advertiser_id: str, display_name: str) -> str:
    """The name shown on the ads: reuse the business's custom identity, or create one with its name."""
    try:
        d = _get(http, token, "identity/get", {"advertiser_id": advertiser_id, "identity_type": "CUSTOMIZED_USER"}, "reading identities")
        for it in d.get("identity_list") or d.get("list") or []:
            if it.get("identity_id"):
                return str(it["identity_id"])
    except TikTokAPIError:
        pass
    d = _post(http, token, "identity/create", {"advertiser_id": advertiser_id, "display_name": (display_name or "Shop")[:40]},
              "creating the ad identity")
    if not d.get("identity_id"):
        raise TikTokAPIError("TikTok didn't create an ad identity. Create one in TikTok Ads Manager (any ad's 'Identity'), then try again.")
    return str(d["identity_id"])


def publish(http, token: str, advertiser_id: str, plan: dict, videos: dict, landing_url: str, pixel_id: str,
            event: str, display_name: str) -> dict:
    """Campaign (OFF) -> one ad group per plan ad set -> one video ad per plan ad. On any failure, the half-made
    campaign is deleted so nothing is left behind. videos: "<set>-<ad>" -> {"data": bytes} or {"url": str}."""
    from datetime import datetime, timezone
    if event not in EVENTS:
        raise TikTokAPIError("Choose what TikTok should optimise for: add to cart, checkout or purchase.")
    countries = [c for c in (plan.get("targeting") or {}).get("countries") or ["AE"] if c in COUNTRY_IDS] or ["AE"]
    sets = plan.get("ad_sets") or []
    total = float(plan.get("daily_budget") or 0)
    created = {"campaign_id": None, "adgroups": [], "ads": []}
    try:
        d = _post(http, token, "campaign/create", {"advertiser_id": advertiser_id, "campaign_name": str(plan.get("name") or "ProAds")[:500],
                                                   "objective_type": "WEB_CONVERSIONS", "budget_mode": "BUDGET_MODE_INFINITE",
                                                   "operation_status": "DISABLE"}, "creating the campaign")
        created["campaign_id"] = str(d.get("campaign_id"))
        ident = identity(http, token, advertiser_id, display_name)
        age_min = int((plan.get("targeting") or {}).get("age_min") or 18)
        ages = [a for a in AGE_GROUPS if int(a.split("_")[1]) >= age_min or a == "AGE_55_100"] if age_min > 18 else None
        for i, s in enumerate(sets):
            share = float(s.get("budget_share") or (1 / max(len(sets), 1)))
            share = share / 100 if share > 1 else share
            body = {"advertiser_id": advertiser_id, "campaign_id": created["campaign_id"],
                    "adgroup_name": str(s.get("name") or f"Ad group {i + 1}")[:500], "promotion_type": "WEBSITE",
                    "placement_type": "PLACEMENT_TYPE_NORMAL", "placements": ["PLACEMENT_TIKTOK"],
                    "location_ids": [COUNTRY_IDS[c] for c in countries], "pixel_id": pixel_id,
                    "optimization_goal": "CONVERT", "optimization_event": EVENTS[event], "billing_event": "OCPM",
                    "bid_type": "BID_TYPE_NO_BID", "budget_mode": "BUDGET_MODE_DAY", "budget": round(total * share, 2),
                    "schedule_type": "SCHEDULE_FROM_NOW",
                    "schedule_start_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "pacing": "PACING_MODE_SMOOTH", "operation_status": "ENABLE"}
            if ages:
                body["age_groups"] = ages
            g = _post(http, token, "adgroup/create", body, f"creating ad group {i + 1}")
            gid = str(g.get("adgroup_id"))
            created["adgroups"].append({"id": gid, "name": body["adgroup_name"], "budget": body["budget"]})
            creatives = []
            for j, ad in enumerate(s.get("ads") or []):
                v = videos.get(f"{i}-{j}")
                if not v:
                    raise TikTokAPIError(f"Ad {j + 1} in '{body['adgroup_name']}' has no video. TikTok ads need a video: choose or upload one.")
                cta = str(ad.get("call_to_action") or "SHOP_NOW").upper()
                if v.get("spark"):                       # the business's own TikTok post, promoted as it is
                    sp = v["spark"]
                    c = {"ad_name": str(ad.get("headline") or f"Ad {j + 1}")[:500], "identity_type": sp["identity_type"],
                         "identity_id": sp["identity_id"], "ad_format": "SINGLE_VIDEO", "tiktok_item_id": sp["item_id"],
                         "call_to_action": cta if cta in CTAS else "SHOP_NOW", "landing_page_url": landing_url}
                    if sp.get("bc_id"):
                        c["identity_authorized_bc_id"] = sp["bc_id"]
                    creatives.append(c)
                    continue
                vid = v.get("video_id") or upload_video(http, token, advertiser_id, ad.get("headline") or "ad", v.get("data"), v.get("url"))
                cover = video_cover(http, token, advertiser_id, vid)
                creatives.append({"ad_name": str(ad.get("headline") or f"Ad {j + 1}")[:500], "identity_type": "CUSTOMIZED_USER",
                                  "identity_id": ident, "ad_format": "SINGLE_VIDEO", "video_id": vid, "image_ids": [cover],
                                  "ad_text": str(ad.get("primary_text") or "")[:100], "call_to_action": cta if cta in CTAS else "SHOP_NOW",
                                  "landing_page_url": landing_url})
            a = _post(http, token, "ad/create", {"advertiser_id": advertiser_id, "adgroup_id": gid, "creatives": creatives},
                      f"creating the ads in ad group {i + 1}")
            created["ads"] += [str(x) for x in a.get("ad_ids") or []]
    except Exception:
        if created["campaign_id"]:
            try:                                          # leave nothing half-made in the account
                _post(http, token, "campaign/status/update", {"advertiser_id": advertiser_id, "campaign_ids": [created["campaign_id"]],
                                                              "operation_status": "DELETE"}, "cleaning up")
            except Exception:
                pass
        raise
    return {"platform": "tiktok", "campaign_id": created["campaign_id"], "advertiser_id": advertiser_id, "status": "PAUSED",
            "name": plan.get("name"), "daily_budget": total, "ad_groups": created["adgroups"], "ads": created["ads"],
            "event": event, "manager_url": f"https://ads.tiktok.com/i18n/perf/campaign?aadvid={advertiser_id}"}


class TikTokLaunchStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS tiktok_launches (client_id TEXT NOT NULL, campaign_id TEXT NOT NULL, data TEXT NOT NULL,
        status TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (client_id, campaign_id))"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def add(self, client_id: str, launch: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO tiktok_launches VALUES (?,?,?,?,?)",
                        (client_id, launch["campaign_id"], json.dumps(launch), launch["status"], int(time.time())))
        self.db.commit()

    def get(self, client_id: str, campaign_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT data, status FROM tiktok_launches WHERE client_id=? AND campaign_id=?", (client_id, campaign_id)).fetchone()
        return {**json.loads(r[0]), "status": r[1]} if r else None

    def set_status(self, client_id: str, campaign_id: str, status: str) -> None:
        self.db.execute("UPDATE tiktok_launches SET status=? WHERE client_id=? AND campaign_id=?", (status, client_id, campaign_id))
        self.db.commit()


def library_videos(http, token: str, advertiser_id: str, limit: int = 100) -> list[dict]:
    """Videos already in the business's TikTok ad account (from past ads): reusable as-is, no re-upload."""
    out, page = [], 1
    while len(out) < limit:
        d = _get(http, token, "file/video/ad/search", {"advertiser_id": advertiser_id, "page": page, "page_size": 50},
                 "reading the TikTok video library")
        for v in d.get("list") or []:
            if v.get("video_id"):
                out.append({"video_id": str(v["video_id"]), "name": v.get("file_name") or "", "cover": v.get("video_cover_url") or v.get("poster_url") or "",
                            "preview": v.get("preview_url") or "", "width": v.get("width"), "height": v.get("height"),
                            "duration": v.get("duration")})
        info = d.get("page_info") or {}
        if page >= int(info.get("total_page") or 1):
            break
        page += 1
    return out[:limit]


# ---------- the business's own TikTok posts (Spark Ads: the real post is promoted, with its likes and comments) ----------
POST_IDENTITIES = ("BC_AUTH_TT", "TT_USER", "AUTH_CODE")


def tiktok_posts(http, token: str, advertiser_id: str, limit: int = 60) -> tuple[list[dict], list[str]]:
    """Posts of the TikTok accounts this ad account may promote. Returns (posts, problems)."""
    posts, problems, idents = [], [], []
    for t in POST_IDENTITIES:
        try:
            d = _get(http, token, "identity/get", {"advertiser_id": advertiser_id, "identity_type": t}, "reading the TikTok accounts")
            idents += [dict(i, identity_type=i.get("identity_type") or t) for i in d.get("identity_list") or d.get("list") or []]
        except TikTokAPIError as e:
            problems.append(str(e))
    if not idents:
        return [], problems or ["No TikTok account is linked to this ad account."]
    for ident in idents:
        params = {"advertiser_id": advertiser_id, "identity_type": ident["identity_type"], "identity_id": ident.get("identity_id"), "count": 20}
        if ident.get("identity_authorized_bc_id"):
            params["identity_authorized_bc_id"] = ident["identity_authorized_bc_id"]
        cursor = None
        for _ in range(4):
            if cursor:
                params["cursor"] = cursor
            try:
                d = _get(http, token, "identity/video/get", params, "reading the TikTok posts")
            except TikTokAPIError as e:
                problems.append(str(e))
                break
            for v in d.get("video_list") or d.get("list") or []:
                info = v.get("video_info") or {}
                if v.get("item_id"):
                    posts.append({"item_id": str(v["item_id"]), "identity_type": ident["identity_type"],
                                  "identity_id": str(ident.get("identity_id") or ""), "bc_id": str(ident.get("identity_authorized_bc_id") or ""),
                                  "account": ident.get("display_name") or "", "text": (v.get("text") or "")[:120],
                                  "cover": info.get("poster_url") or info.get("cover_url") or v.get("cover_url") or ""})
            cursor = d.get("cursor")
            if not d.get("has_more") or not cursor or len(posts) >= limit:
                break
    return posts[:limit], problems
