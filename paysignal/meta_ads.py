"""Publish a campaign plan to Meta Ads Manager through the Marketing API.

Everything is created PAUSED: campaign -> ad set(s) -> image -> creative -> ad.
Nothing spends money until a person presses "Go live", which switches the
campaign, its ad sets and its ads to ACTIVE.

Settings (server environment, never the browser):
  PROADS_AD_ACCOUNT_ID   ad account ID (with or without "act_")
  PROADS_PAGE_ID         Facebook Page the ads run from
  PROADS_INSTAGRAM_ID    optional: Instagram account ID, so ads show as that account
  PROADS_ADS_TOKEN       system user token with ads_management, pages_manage_ads,
                         pages_read_engagement, business_management
                         (falls back to PROADS_META_TOKEN)
  PROADS_META_DATASET_ID the Pixel the ads optimise for (same as tracking)
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from typing import Optional

from .attribution import tag_link

log = logging.getLogger("proads.meta_ads")

EVENTS = {"signup": ("OUTCOME_LEADS", "OFFSITE_CONVERSIONS", "COMPLETE_REGISTRATION"),
          "purchase": ("OUTCOME_SALES", "VALUE", "PURCHASE")}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class PublishError(RuntimeError):
    pass


# currencies Meta counts without cents (offset 1); all others use 100
ZERO_DECIMAL = {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"}


def minor_units(amount: float, currency: str) -> int:
    return int(round(amount * (1 if currency.upper() in ZERO_DECIMAL else 100)))


def settings() -> dict:
    env = lambda k: os.environ.get(k, "").strip()
    acct = env("PROADS_AD_ACCOUNT_ID")
    if acct and not acct.startswith("act_"):
        acct = "act_" + acct
    return {"ad_account": acct, "page_id": env("PROADS_PAGE_ID"), "instagram_id": env("PROADS_INSTAGRAM_ID"),
            "token": env("PROADS_ADS_TOKEN") or env("PROADS_META_TOKEN"),
            "pixel_id": env("PROADS_META_DATASET_ID"), "version": env("PROADS_META_VERSION") or "v25.0"}


def missing(s: Optional[dict] = None) -> list[str]:
    s = s or settings()
    need = [("PROADS_AD_ACCOUNT_ID", s["ad_account"]), ("PROADS_PAGE_ID", s["page_id"]),
            ("PROADS_ADS_TOKEN", s["token"]), ("PROADS_META_DATASET_ID", s["pixel_id"])]
    return [name for name, v in need if not v]


def manager_url(ad_account: str, campaign_id: str) -> str:
    return (f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={ad_account.replace('act_', '')}"
            f"&selected_campaign_ids={campaign_id}")


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n] or "ad"


def decode_image(data: str) -> str:
    """Accept a data URL or bare base64; return clean base64 after checking it is a JPEG or PNG."""
    data = (data or "").split(",", 1)[-1].strip()
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise PublishError("an ad image could not be read") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise PublishError("an ad image is larger than 8 MB")
    if not (raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n"):
        raise PublishError("ad images must be JPEG or PNG")
    return base64.b64encode(raw).decode()


class Publisher:
    def __init__(self, http=None, s: Optional[dict] = None):
        self.s = s or settings()
        if http is None:
            import requests
            http = requests
        self.http = http

    # ---------- Graph API ----------
    def _url(self, path: str) -> str:
        return f"https://graph.facebook.com/{self.s['version']}/{path}"

    def _check(self, r, step: str) -> dict:
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code != 200 or "error" in body:
            e = body.get("error") or {}
            msg = e.get("error_user_msg") or e.get("message") or f"HTTP {r.status_code}"
            title = e.get("error_user_title")
            err = PublishError(f"Meta refused {step}: {title + ' - ' if title else ''}{msg}")
            err.code = e.get("code")
            raise err
        return body

    def _post(self, path: str, data: dict, step: str) -> dict:
        form = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in data.items()}
        form["access_token"] = self.s["token"]
        return self._check(self.http.post(self._url(path), data=form, timeout=60), step)

    def _get(self, path: str, fields: str, step: str) -> dict:
        r = self.http.get(self._url(path), params={"fields": fields, "access_token": self.s["token"]}, timeout=30)
        return self._check(r, step)

    def _delete(self, obj_id: str) -> None:
        try:
            self.http.delete(self._url(obj_id), params={"access_token": self.s["token"]}, timeout=30)
        except Exception as e:                              # best effort clean-up
            log.warning("could not delete %s: %s", obj_id, e)

    # ---------- publish ----------
    def account(self) -> dict:
        return self._get(self.s["ad_account"], "name,currency,account_status", "reading the ad account")

    def publish(self, plan: dict, images: dict[str, str], landing_url: str, currency: str = "") -> dict:
        """Create the whole campaign PAUSED. images: "<adset index>-<ad index>" -> base64 JPEG/PNG."""
        if missing(self.s):
            raise PublishError("publishing is not set up: add " + ", ".join(missing(self.s)))
        if not re.match(r"^https://", landing_url or ""):
            raise PublishError("the page the ads open must start with https://")
        conversion = plan.get("conversion") if plan.get("conversion") in EVENTS else "purchase"
        objective, goal, event = EVENTS[conversion]
        sets = [s for s in plan.get("ad_sets") or [] if s.get("ads")]
        if not sets:
            raise PublishError("the plan has no ads")
        for i, s in enumerate(sets):
            for j, _ in enumerate(s["ads"]):
                if f"{i}-{j}" not in images:
                    raise PublishError(f"ad {j + 1} in '{s.get('name', 'ad set')}' has no image")
        try:
            budget = float(plan.get("daily_budget"))
        except (TypeError, ValueError):
            raise PublishError("the plan has no daily budget") from None

        acct = self.account()
        if acct.get("account_status") not in (1, None):
            raise PublishError("the ad account is not active (check billing and status in Ads Manager)")
        # the ad account must be allowed to use the Pixel, or Meta builds the campaign and then refuses to run it
        try:
            r = self.http.get(self._url(f"{self.s['ad_account']}/adspixels"),
                              params={"fields": "id", "limit": 200, "access_token": self.s["token"]}, timeout=30)
            listed = {str(p.get("id")) for p in (r.json().get("data") or [])} if r.status_code == 200 else None
        except Exception:
            listed = None
        if listed is not None and str(self.s["pixel_id"]) not in listed:
            raise PublishError(f"ad account {self.s['ad_account'].replace('act_', '')} can't use Pixel {self.s['pixel_id']}. "
                               "Pick the ad account that's linked to this Pixel (Business settings > Datasets & pixels > "
                               "Connected assets), or link it there, then try again")
        # the budget is in the ad account's own currency (any currency works)
        currency = (acct.get("currency") or currency or "AED").upper()

        name = str(plan.get("name") or "ProAds campaign")[:200]
        campaign_slug = _slug(name, 30)
        t = plan.get("targeting") or {}
        targeting = {"geo_locations": {"countries": t.get("countries") or ["AE"]},
                     "age_min": int(t.get("age_min") or 18), "age_max": 65,
                     "targeting_automation": {"advantage_audience": 1}}

        camp = self._post(f"{self.s['ad_account']}/campaigns", {
            "name": name, "objective": objective, "status": "PAUSED", "special_ad_categories": [],
            "daily_budget": str(minor_units(budget, currency)), "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        }, "creating the campaign")
        campaign_id = camp["id"]
        created = {"campaign_id": campaign_id, "ad_sets": [], "ads": []}
        try:
            for i, s in enumerate(sets):
                adset = self._post(f"{self.s['ad_account']}/adsets", {
                    "name": str(s.get("name") or f"Ad set {i + 1}")[:200], "campaign_id": campaign_id,
                    "status": "PAUSED", "billing_event": "IMPRESSIONS", "optimization_goal": goal,
                    "destination_type": "WEBSITE",
                    "promoted_object": {"pixel_id": self.s["pixel_id"], "custom_event_type": event},
                    "targeting": targeting,
                }, f"creating ad set {i + 1}")
                created["ad_sets"].append(adset["id"])
                for j, ad in enumerate(s["ads"]):
                    label = f"ad {j + 1} in ad set {i + 1}"
                    up = self._post(f"{self.s['ad_account']}/adimages",
                                    {"bytes": decode_image(images[f"{i}-{j}"])}, f"uploading the image for {label}")
                    image_hash = next(iter((up.get("images") or {}).values()), {}).get("hash")
                    if not image_hash:
                        raise PublishError(f"Meta did not return an image for {label}")
                    link = tag_link(landing_url, "meta", f"{campaign_slug}-{i + 1}-{j + 1}",
                                    campaign=campaign_slug, medium="paid")
                    story = {"page_id": self.s["page_id"], "link_data": {
                        "link": link, "message": ad.get("primary_text", ""), "name": ad.get("headline", ""),
                        "image_hash": image_hash,
                        "call_to_action": {"type": ad.get("call_to_action") or "LEARN_MORE", "value": {"link": link}}}}
                    if self.s["instagram_id"]:
                        story["instagram_user_id"] = self.s["instagram_id"]
                    creative = self._post(f"{self.s['ad_account']}/adcreatives", {
                        "name": f"{name[:80]} - {label}", "object_story_spec": story}, f"creating the creative for {label}")
                    made = self._post(f"{self.s['ad_account']}/ads", {
                        "name": str(ad.get("headline") or label)[:200], "adset_id": adset["id"],
                        "creative": {"creative_id": creative["id"]}, "status": "PAUSED",
                    }, f"creating {label}")
                    created["ads"].append(made["id"])
        except Exception:
            self._delete(campaign_id)                       # no half-built campaigns left behind
            raise
        created.update(status="PAUSED", conversion=conversion, manager_url=manager_url(self.s["ad_account"], campaign_id),
                       ad_account=self.s["ad_account"], currency=currency, daily_budget=budget, name=name,
                       created_at=int(time.time()))
        return created

    def spend(self, campaign_ids: list[str], start: int, end: int) -> tuple[float, str]:
        """Money spent by these campaigns between two times (whole days, ad account time zone)."""
        since = time.strftime("%Y-%m-%d", time.gmtime(start))
        until = time.strftime("%Y-%m-%d", time.gmtime(max(start, end - 1)))
        total, currency = 0.0, ""
        for cid in campaign_ids:
            r = self.http.get(self._url(f"{cid}/insights"), params={
                "fields": "spend,account_currency", "time_range": json.dumps({"since": since, "until": until}),
                "access_token": self.s["token"]}, timeout=30)
            for row in self._check(r, f"reading spend of campaign {cid}").get("data") or []:
                total += float(row.get("spend") or 0)
                currency = row.get("account_currency") or currency
        return round(total, 2), currency

    def insights(self, campaign_ids: list[str], since: str, until: str, breakdowns: str = "",
                 daily: bool = False) -> list[dict]:
        """Raw insight rows for these campaigns (dates are YYYY-MM-DD, ad account time zone)."""
        rows = []
        for cid in campaign_ids:
            params = {"fields": "spend,impressions,reach,clicks,actions,account_currency",
                      "time_range": json.dumps({"since": since, "until": until}), "limit": 500,
                      "access_token": self.s["token"]}
            if breakdowns:
                params["breakdowns"] = breakdowns
            if daily:
                params["time_increment"] = 1
            r = self.http.get(self._url(f"{cid}/insights"), params=params, timeout=30)
            rows += self._check(r, f"reading results of campaign {cid}").get("data") or []
        return rows

    def set_status(self, launch: dict, status: str) -> None:
        """ACTIVE or PAUSED for the campaign and everything in it."""
        for obj_id in [launch["campaign_id"], *launch["ad_sets"], *launch["ads"]]:
            self._post(obj_id, {"status": status}, f"switching {obj_id} to {status.lower()}")

    def status_of(self, campaign_id: str) -> str:
        """What Meta says the campaign's own switch is (ACTIVE, PAUSED, DELETED, ARCHIVED)."""
        return (self._get(campaign_id, "status", f"checking campaign {campaign_id}").get("status") or "").upper()


class LaunchStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS ad_launches (
        campaign_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, data TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def add(self, client_id: str, launch: dict) -> None:
        self.db.execute("INSERT INTO ad_launches (campaign_id, client_id, data, status) VALUES (?,?,?,?)",
                        (launch["campaign_id"], client_id, json.dumps(launch), launch["status"]))
        self.db.commit()

    def get(self, client_id: str, campaign_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT data, status FROM ad_launches WHERE client_id=? AND campaign_id=?",
                              (client_id, campaign_id)).fetchone()
        return {**json.loads(row[0]), "status": row[1]} if row else None

    def set_status(self, client_id: str, campaign_id: str, status: str) -> None:
        self.db.execute("UPDATE ad_launches SET status=? WHERE client_id=? AND campaign_id=?",
                        (status, client_id, campaign_id))
        self.db.commit()

    def list(self, client_id: str) -> list[dict]:
        rows = self.db.execute("SELECT data, status, created_at FROM ad_launches WHERE client_id=?"
                               " ORDER BY created_at DESC, rowid DESC", (client_id,)).fetchall()
        return [{**json.loads(d), "status": st, "created_day": (ca or "")[:10]} for d, st, ca in rows]

    def clients(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT client_id FROM ad_launches").fetchall()]


class DraftStore:
    """Every campaign the builder made, so it can be reopened, published or compared later."""
    SCHEMA = """CREATE TABLE IF NOT EXISTS campaign_drafts (
        id TEXT PRIMARY KEY, client_id TEXT NOT NULL, data TEXT NOT NULL, campaign_id TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')))"""

    def __init__(self, db):
        self.db = db
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def add(self, client_id: str, result: dict) -> str:
        import secrets
        did = secrets.token_hex(6)
        self.db.execute("INSERT INTO campaign_drafts (id, client_id, data) VALUES (?,?,?)",
                        (did, client_id, json.dumps(result)))
        self.db.execute("DELETE FROM campaign_drafts WHERE client_id=? AND id NOT IN (SELECT id FROM campaign_drafts"
                        " WHERE client_id=? ORDER BY created_at DESC, rowid DESC LIMIT 100)", (client_id, client_id))
        self.db.commit()
        return did

    def get(self, client_id: str, did: str) -> Optional[dict]:
        row = self.db.execute("SELECT data, campaign_id, created_at FROM campaign_drafts WHERE client_id=? AND id=?",
                              (client_id, did)).fetchone()
        return {**json.loads(row[0]), "draft_id": did, "campaign_id": row[1], "created_at": row[2]} if row else None

    def link(self, client_id: str, did: str, campaign_id: str) -> None:
        self.db.execute("UPDATE campaign_drafts SET campaign_id=? WHERE client_id=? AND id=?", (campaign_id, client_id, did))
        self.db.commit()

    def delete(self, client_id: str, did: str) -> bool:
        cur = self.db.execute("DELETE FROM campaign_drafts WHERE client_id=? AND id=?", (client_id, did))
        self.db.commit()
        return cur.rowcount == 1

    def list(self, client_id: str) -> list[dict]:
        rows = self.db.execute("SELECT id, data, campaign_id, created_at FROM campaign_drafts WHERE client_id=?"
                               " ORDER BY created_at DESC, rowid DESC", (client_id,)).fetchall()
        out = []
        for did, data, camp, created in rows:
            r = json.loads(data)
            p = r.get("plan") or {}
            out.append({"draft_id": did, "created_at": created, "campaign_id": camp, "name": p.get("name"),
                        "daily_budget": p.get("daily_budget"), "currency": p.get("currency"),
                        "optimization": p.get("optimization"), "ads": sum(len(s.get("ads") or []) for s in p.get("ad_sets") or []),
                        "problems": bool(r.get("problems"))})
        return out
