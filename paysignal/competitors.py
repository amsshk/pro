"""Competitor X-ray (Premium): read a competitor's public ads from Meta's Ad Library, find which
ones they keep paying for (long-running ads are the ones that make them money), and have the AI
build an ORIGINAL counter-campaign that beats them. Their text, brand and claims are never copied.

Meta's Ad Library API returns every ad shown in the EU/UK; for other countries it can be limited,
so the client can also paste competitor ad texts by hand.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from .ai import MetaAI
from .assistant import CTAS, CampaignError, _extract_json, tidy_plan, validate_campaign

FIELDS = ("id,page_id,page_name,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,"
          "ad_delivery_start_time,ad_delivery_stop_time,publisher_platforms,languages,ad_snapshot_url")


class ScanError(RuntimeError):
    pass


def scan(http, token: str, version: str, query: str = "", page_ids: Optional[list[str]] = None,
         country: str = "AE", limit: int = 60) -> list[dict]:
    """Active ads from the Ad Library for a search term or Page IDs, newest data first."""
    params = {"access_token": token, "fields": FIELDS, "ad_active_status": "ACTIVE", "ad_type": "ALL",
              "ad_reached_countries": json.dumps([country.upper()]), "limit": min(limit, 100)}
    if page_ids:
        params["search_page_ids"] = json.dumps(page_ids[:10])
    elif query:
        params["search_terms"] = query[:100]
    else:
        raise ScanError("type a competitor's name or Page link")
    if http is None:
        import requests
        http = requests
    try:
        r = http.get(f"https://graph.facebook.com/{version}/ads_archive", params=params, timeout=40)
    except Exception as e:
        raise ScanError(f"couldn't reach Meta's Ad Library ({type(e).__name__}). Try again in a minute.") from e
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code != 200 or "error" in body:
        e = body.get("error") or {}
        msg = e.get("error_user_msg") or e.get("message") or f"HTTP {r.status_code}"
        if "identity" in msg.lower() or e.get("code") in (10, 2332002):
            msg = ("Meta needs you to confirm your identity once before ProAds can read the Ad Library "
                   "(facebook.com/ID). Or paste your competitor's ad texts below instead.")
        raise ScanError(msg)
    return [normalize(a) for a in body.get("data") or []]


def normalize(a: dict) -> dict:
    started = a.get("ad_delivery_start_time")
    days = None
    if started:
        try:
            days = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(started.replace("Z", "+00:00"))
                           .astimezone(timezone.utc)).days)
        except ValueError:
            days = None
    first = lambda k: ((a.get(k) or [""])[0] or "").strip()
    return {"id": str(a.get("id", "")), "page": a.get("page_name") or "", "page_id": str(a.get("page_id") or ""),
            "text": first("ad_creative_bodies")[:600], "headline": first("ad_creative_link_titles")[:120],
            "description": first("ad_creative_link_descriptions")[:200], "days_running": days,
            "platforms": a.get("publisher_platforms") or [], "languages": a.get("languages") or [],
            # Meta's snapshot link carries the access token, so link to the public Ad Library page instead
            "snapshot_url": f"https://www.facebook.com/ads/library/?id={a.get('id')}" if a.get("id") else ""}


def normalize_client(a: dict) -> dict:
    """An ad sent back by the browser: keep only known fields, as plain short values."""
    days = a.get("days_running")
    return {"id": str(a.get("id", ""))[:40], "page": str(a.get("page") or "")[:120],
            "page_id": str(a.get("page_id") or "")[:40], "text": str(a.get("text") or "")[:600],
            "headline": str(a.get("headline") or "")[:120], "description": str(a.get("description") or "")[:200],
            "days_running": int(days) if isinstance(days, (int, float)) and days >= 0 else None,
            "platforms": [str(p)[:30] for p in (a.get("platforms") or [])][:6] if isinstance(a.get("platforms"), list) else [],
            "languages": [], "snapshot_url": ""}


def from_pasted(text: str) -> list[dict]:
    """Competitor ads typed or pasted by hand: one ad per block, separated by a blank line."""
    blocks = [b.strip() for b in (text or "").replace("\r", "").split("\n\n") if b.strip()]
    return [{"id": f"pasted-{i + 1}", "page": "", "page_id": "", "text": b[:600], "headline": "", "description": "",
             "days_running": None, "platforms": [], "languages": [], "snapshot_url": ""} for i, b in enumerate(blocks[:30])]


def summarize(ads: list[dict]) -> dict:
    """What the numbers say before any AI: who advertises, how much, and what they keep running."""
    pages = Counter(a["page"] for a in ads if a["page"])
    platforms = Counter(p for a in ads for p in a["platforms"])
    running = sorted([a for a in ads if a["days_running"] is not None], key=lambda a: -a["days_running"])
    texts = Counter(a["text"][:80] for a in ads if a["text"])
    return {
        "ads": len(ads), "advertisers": [{"page": p, "ads": n} for p, n in pages.most_common(6)],
        "platforms": [{"name": p.replace("_", " ").title(), "ads": n} for p, n in platforms.most_common()],
        "longest_running": [{k: a[k] for k in ("id", "page", "headline", "text", "days_running", "snapshot_url")}
                            for a in running[:8]],
        "repeated_messages": sum(1 for _, n in texts.items() if n > 1),
    }


SYSTEM = (
    "You are a senior Meta ads strategist. You get a competitor's live ads (from Meta's public Ad Library or pasted "
    "by the client) and this business's own data. Ads that have run for many days are the ones the competitor keeps "
    "paying for, so they most likely work.\n"
    "1) Explain what is working for the competitor: hooks, offers, angles, formats, who they speak to.\n"
    "2) Find their weak spots and what customers are not being told.\n"
    "3) Build an ORIGINAL counter-campaign for THIS business that wins against them. Never copy or closely paraphrase "
    "their text, never use their brand, product names, prices or claims, never imply you are them, and never "
    "mention the competitor in the ads. Never invent results, prices or discounts that aren't in the brief.\n"
    "Ads are single-image ads, headline max 40 characters, primary_text max 300, English only, call_to_action one of: "
    + ", ".join(CTAS) + ". Stay within the daily budget. Optimise for the brief's conversion ('purchase' = OUTCOME_SALES "
    "with value, 'signup' = OUTCOME_LEADS CompleteRegistration).\n"
    "Reply with only a JSON object: {\"insights\": {\"whats_working\": [str], \"their_offers\": [str], "
    "\"weak_spots\": [str], \"how_we_beat_them\": [str]}, \"plan\": {\"name\": str, \"objective\": str, "
    "\"optimization\": str, \"daily_budget\": number, \"currency\": str, \"rationale\": str, "
    "\"targeting\": {\"countries\": [str], \"age_min\": number}, \"ad_sets\": [{\"name\": str, \"audience\": str, "
    "\"budget_share\": number, \"placements\": str, \"ads\": [{\"format\": \"single image\", \"hook\": str, "
    "\"offer\": str, \"primary_text\": str, \"headline\": str, \"call_to_action\": str, \"why\": str}]}], "
    "\"organic_plan\": [{\"day\": str, \"platform\": str, \"format\": str, \"hook\": str, \"caption\": str, \"why\": str}], "
    "\"tests\": [str], \"kill_rules\": [str], \"what_to_watch\": [str]}}"
)


def counter_campaign(ai: MetaAI, ads: list[dict], context: dict, daily_budget: float, conversion: str,
                     product: str, landing_url: str, currency: str, notes: str = "") -> dict:
    conversion = "signup" if conversion == "signup" else "purchase"
    picked = sorted(ads, key=lambda a: -(a["days_running"] or 0))[:25]
    brief = {"daily_budget": daily_budget, "currency": currency, "conversion": conversion, "product": product,
             "landing_page": landing_url, "notes": notes}
    comp = [{k: a[k] for k in ("page", "headline", "text", "description", "days_running", "platforms")} for a in picked]
    reply = ai.chat("campaign_builder", [
        {"role": "developer", "content": SYSTEM},
        {"role": "user", "content": "Brief:\n" + json.dumps(brief, ensure_ascii=False) +
         "\n\nCompetitor ads:\n" + json.dumps(comp, ensure_ascii=False) +
         "\n\nThis business's data:\n" + json.dumps(context, ensure_ascii=False, default=str)}],
        max_completion_tokens=16000)
    try:
        out = _extract_json(reply.text)
    except (json.JSONDecodeError, CampaignError) as e:
        raise CampaignError(f"could not read the plan the model returned: {e}") from e
    plan = out.get("plan") or {}
    problems = validate_campaign(plan, daily_budget)
    if not problems:
        plan = tidy_plan(plan, conversion)
        plan["currency"] = currency
    return {"plan": plan, "insights": out.get("insights") or {}, "problems": problems, "model": reply.model,
            "landing_url": landing_url, "cost_usd": reply.cost_usd, "status": "draft - review before launching"}
