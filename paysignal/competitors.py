"""Competitor X-ray (Premium): read a competitor's public ads from Meta's Ad Library, put the
longest-running first (a signal, not proof, of what works for them: Meta shows no sales or spend), and have the AI
build an ORIGINAL counter-campaign to compete with them. Their text, brand and claims are never copied.

Meta's Ad Library API returns every ad shown in the EU/UK; for other countries it can be limited,
so the client can also paste competitor ad texts by hand.
"""
from __future__ import annotations

import json
import time
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
         country: str = "AE", limit: int = 40, timeout: float = 25) -> list[dict]:
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
        r = http.get(f"https://graph.facebook.com/{version}/ads_archive", params=params, timeout=timeout)
    except Exception as e:
        if "timeout" in type(e).__name__.lower():
            raise ScanError("Meta's Ad Library took too long to answer. Try again, or paste their Facebook Page link "
                            "instead of a name (it's much faster).") from e
        raise ScanError(f"couldn't reach Meta's Ad Library ({type(e).__name__}). Try again in a minute.") from e
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code != 200 or "error" in body:
        e = body.get("error") or {}
        msg = e.get("error_user_msg") or e.get("message") or f"HTTP {r.status_code}"
        if "identity" in msg.lower() or e.get("code") in (10, 2332002):
            msg = ("Meta hasn't opened its Ad Library to this account yet (Meta says: " + msg[:160] + "). "
                   "Paste your competitor's ad texts below instead for now.")
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
    "by the client) and this business's own data. The Ad Library shows no sales, spend or results. "
    "How long an ad has run is only a signal: advertisers tend to keep ads that work, but it is not proof. Treat "
    "everything you say about their performance as an estimate and word it that way (\"likely\", \"seems\"), "
    "never as a fact.\n"
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


# ---------------------------------------------------------------- Win their customers (conquest plan)
CONQUEST_SYSTEM = (
    "You are a senior performance marketer in the GCC. A business wants to win customers that its competitors' ads "
    "have already made curious. You get the competitor names, optionally some of their live ads, and this business's "
    "own sales facts. Build a practical plan, honest and within platform rules:\n"
    "- edges: what THIS business can truthfully offer that buyers may not get from the competitors. Use only the "
    "facts given (products, prices, average order, repeat rate); never invent prices, discounts, awards or results.\n"
    "- search_keywords: searches people type when comparing or doubting a competitor (e.g. '<brand> alternative', "
    "'<brand> review', 'perfume like <brand>'), for TikTok Search ads and Google Search. Bidding on these searches is "
    "allowed; the ad TEXT must never contain the competitor's name or trademark.\n"
    "- tiktok_targeting: hashtags, interest categories and creator categories to target on TikTok.\n"
    "- meta_interests: interests on Meta (well-known brand or category interests are fine for targeting).\n"
    "- scripts: exactly 3 short TikTok video scripts (15-30 s) that win the comparison without naming or showing the "
    "competitor, their logo or packaging; compare scent types, buying experience or value honestly; no claims about "
    "the competitor's product; no fake reviews or staged 'customer' testimonials.\n"
    "- creator_brief: how to work with local creators whose audiences follow the competitors (who to pick, what to "
    "send, what to ask for, that each creator gets their own tracking link, and that paid posts must be disclosed as "
    "ads under UAE rules).\n"
    "- watch_out: 3-5 rules that keep the ad accounts safe.\n"
    "Plain English, short. Reply with only JSON: {\"edges\": [{\"edge\": str, \"proof\": str}], "
    "\"search_keywords\": [{\"keyword\": str, \"platform\": \"TikTok Search\"|\"Google\"|\"Both\", \"why\": str}], "
    "\"tiktok_targeting\": {\"hashtags\": [str], \"interests\": [str], \"creator_categories\": [str]}, "
    "\"meta_interests\": [str], \"scripts\": [{\"title\": str, \"hook\": str, \"script\": str, \"on_screen_text\": str, "
    "\"cta\": str}], \"creator_brief\": {\"who\": str, \"send\": str, \"ask\": str, \"deliverables\": [str], "
    "\"rules\": [str]}, \"watch_out\": [str]}"
)


def library_links(name: str, country: str = "AE") -> dict:
    from urllib.parse import quote
    q = quote(name.strip())
    return {"name": name.strip(),
            "meta": f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country={country}&q={q}&search_type=keyword_unordered",
            # TikTok's Ad Library only covers Europe; Creative Center's Top Ads has regions like the UAE and Saudi Arabia
            "tiktok": "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en"}


def conquest_plan(ai: MetaAI, names: list[str], facts: dict, ads: Optional[list[dict]] = None, notes: str = "") -> dict:
    comp = [{k: a.get(k) for k in ("page", "headline", "text", "days_running")} for a in (ads or [])][:20]
    reply = ai.chat("campaign_builder", [
        {"role": "developer", "content": CONQUEST_SYSTEM},
        {"role": "user", "content": json.dumps({"competitors": names, "notes": notes, "business": facts,
                                                "competitor_ads": comp}, ensure_ascii=False, default=str)}],
        max_completion_tokens=8000)
    try:
        out = _extract_json(reply.text)
    except (json.JSONDecodeError, CampaignError) as e:
        raise CampaignError(f"could not read the plan the model returned: {e}") from e
    out["scripts"] = (out.get("scripts") or [])[:3]
    return {"plan": out, "model": reply.model, "cost_usd": reply.cost_usd}


# ---------------------------------------------------------------- paying for plans
def conquest_prices() -> dict:
    """AED. Premium includes a few plans a month; anyone can buy more, one at a time."""
    import os

    def num(k, d):
        try:
            return float(os.environ.get(k, "") or d)
        except ValueError:
            return d
    return {"price": num("PROADS_CONQUEST_PRICE", 149), "premium_price": num("PROADS_CONQUEST_PREMIUM_PRICE", 99),
            "included": int(num("PROADS_CONQUEST_INCLUDED", 3)), "currency": "AED"}


class ConquestStore:
    """Bought plans (credits, one per payment, idempotent by Stripe session) and every plan made."""

    def __init__(self, db):
        self.db = db
        self.db.execute("CREATE TABLE IF NOT EXISTS conquest_credits (ref TEXT PRIMARY KEY, client_id TEXT NOT NULL,"
                        " delta INTEGER NOT NULL, reason TEXT NOT NULL, at INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS conquest_plans (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,"
                        " competitors TEXT NOT NULL, plan TEXT NOT NULL, paid_with TEXT NOT NULL, at INTEGER NOT NULL)")
        self.db.commit()

    def credit(self, client_id: str, ref: str, reason: str = "bought") -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO conquest_credits VALUES (?,?,?,?,?)", (ref, client_id, 1, reason, int(time.time())))
        self.db.commit()
        return cur.rowcount == 1

    def bought_left(self, client_id: str) -> int:
        r = self.db.execute("SELECT COALESCE(SUM(delta),0) FROM conquest_credits WHERE client_id=?", (client_id,)).fetchone()
        return int(r[0] or 0)

    def included_used(self, client_id: str) -> int:
        start = int(datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        r = self.db.execute("SELECT COUNT(*) FROM conquest_plans WHERE client_id=? AND paid_with='included' AND at>=?",
                            (client_id, start)).fetchone()
        return int(r[0] or 0)

    def save(self, client_id: str, names: list[str], plan: dict, paid_with: str) -> int:
        if paid_with == "bought":
            self.db.execute("INSERT INTO conquest_credits VALUES (?,?,?,?,?)",
                            (f"use:{client_id}:{time.time_ns()}", client_id, -1, "used", int(time.time())))
        cur = self.db.execute("INSERT INTO conquest_plans (client_id, competitors, plan, paid_with, at) VALUES (?,?,?,?,?)",
                              (client_id, ", ".join(names), json.dumps(plan, ensure_ascii=False), paid_with, int(time.time())))
        self.db.commit()
        return cur.lastrowid

    def plans(self, client_id: str, limit: int = 30) -> list[dict]:
        rows = self.db.execute("SELECT id, competitors, plan, paid_with, at FROM conquest_plans WHERE client_id=? ORDER BY id DESC LIMIT ?",
                               (client_id, limit)).fetchall()
        return [{"id": r[0], "competitors": r[1], "result": json.loads(r[2]), "paid_with": r[3], "at": r[4]} for r in rows]


def conquest_checkout(stripe, client_id: str, email: str, customer_id: Optional[str], return_base: str, amount: float) -> str:
    """Stripe's hosted page for one plan (cards, Apple Pay). An invoice is made so it lands in ProAds' revenue."""
    data = {"mode": "payment", "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "aed", "line_items[0][price_data][unit_amount]": str(int(round(amount * 100))),
            "line_items[0][price_data][product_data][name]": "ProAds: Win their customers (1 plan)",
            "success_url": return_base + "/app#/competitors?bought=1", "cancel_url": return_base + "/app#/competitors",
            "client_reference_id": client_id, "metadata[client_id]": client_id, "metadata[kind]": "conquest_plan",
            "invoice_creation[enabled]": "true", "invoice_creation[invoice_data][metadata][kind]": "conquest_plan",
            "invoice_creation[invoice_data][metadata][client_id]": client_id}
    if customer_id:
        data["customer"] = customer_id
    elif email:
        data["customer_email"] = email
    return stripe.post("checkout/sessions", data)["url"]
