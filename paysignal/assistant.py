"""AI features on top of the engine, powered by Meta's best model (router tasks
'chat' and 'campaign_builder').

  ask()             answer questions about a client's results in plain language
  build_campaign()  draft a full Meta ad campaign from what organically made money

Privacy: the AI only ever sees aggregated numbers (totals, per-post results,
patterns). Customer names, emails, phones and individual payments are never sent.
Campaign plans are drafts; publishing to Meta (meta_ads.py) creates them paused, and only
a person's explicit 'go live' starts spending.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Callable, Optional

from .ai import MetaAI, Reply
from .engine import Engine
from .organic import OrganicEngine

log = logging.getLogger("paysignal.ai_features")


def business_context(engine: Engine, organic: Optional[OrganicEngine] = None, currency: str = "AED") -> dict:
    """Aggregated facts about one client. Safe to send to the AI: no personal data."""
    ctx = {"currency": currency, "totals": engine.store.totals(engine.client_id)}
    if organic is not None:
        results = organic.scoreboard()
        ctx["posts"] = [{"post_id": r.post.post_id, "platform": r.post.platform, "format": r.post.format,
                         "hook": r.post.hook, "offer": r.post.offer,
                         "posted": r.post.posted_at.strftime("%a %H:%M"), "views": r.post.views,
                         "engagement_rate": round(r.post.engagement_rate, 4), "orders": r.orders,
                         "refund_rate": round(r.refunds / r.gross, 3) if r.gross else 0.0,
                         "real_value": round(r.real_value), "est_extra_value": round(r.halo_value),
                         "value_per_1k_views": round(r.value_per_1k), "tags": r.tags} for r in results[:40]]
        ctx["patterns"] = {a: {k: {"lift": round(v["lift"], 2), "posts": v["posts"], "confident": v["confident"]}
                               for k, v in t.items()} for a, t in organic.patterns(results).items()}
        ctx["boost_candidates"] = [r.post.post_id for r in organic.boost_candidates(results)]
        ctx["post_next"] = organic.recommend()
    else:
        ctx["content"] = engine.content_report()[:20]
    return ctx


SYSTEM_CHAT = (
    "You are ProAds's analyst for a marketing agency. You answer questions about one business's "
    "results using ONLY the data provided. Key ideas: 'real value' is profit after refunds (unless profit_note says the "
    "product cost is unknown: then never call it profit); posts are "
    "judged by real value per 1,000 views, not likes; 'est_extra_value' is an estimate of sales that came "
    "without a link. benchmarks_from_other_businesses are anonymous medians from many businesses; use them only for "
    "comparison and say so. Patterns marked confident=false are not proven yet: say so instead of presenting them "
    "as fact. For questions about ads or campaigns, use tiktok_ad_account_live and ads_last_30_days: they hold the "
    "live ad accounts with every campaign, not only those published through ProAds. Name the campaign, its spend, what the "
    "platform reports and what was really paid, then give the likely reason and one next step. If the data can't answer the "
    "question, say what data is missing. Be short and concrete, give "
    "numbers, and reply in the language the question was asked in."
)

CTAS = ("SIGN_UP", "LEARN_MORE", "SHOP_NOW", "BOOK_NOW", "CONTACT_US", "GET_OFFER", "ORDER_NOW",
        "SUBSCRIBE", "GET_QUOTE", "APPLY_NOW", "DOWNLOAD", "WHATSAPP_MESSAGE")

SYSTEM_CAMPAIGN = (
    "You are a senior Meta ads strategist. Plan a Meta (Facebook/Instagram) campaign for this business "
    "AND the organic posts that go with it. The campaign will be published to Meta as written, so be exact.\n"
    "Rules:\n"
    "- Optimise for the brief's conversion: 'purchase' means objective OUTCOME_SALES, optimisation "
    "'Purchase with value' (real profit values arrive through the Conversions API); 'signup' means objective "
    "OUTCOME_LEADS, optimisation 'CompleteRegistration' (website sign-ups, the product is free).\n"
    "- Prefer broad targeting and 1-2 ad sets: Meta's delivery system finds the right people when given good signals.\n"
    "- Build ads on the boost_candidates and confident winning patterns. Anything not proven is labelled a test.\n"
    "- Avoid what lost money (high refunds, watchers-only hooks).\n"
    "- Ads are published as single-image ads with a link: format must be 'single image'. 2-3 ads per ad set, "
    "each a different angle. headline max 40 characters, primary_text max 300 characters.\n"
    "- benchmarks_from_other_businesses (if present) are anonymous medians from many businesses: use them only "
    "where this business's own data is thin, and say they are general benchmarks, not this business's results.\n"
    "- Use the product description in the brief for what the business offers. Never invent prices, results, "
    "reviews, discounts or numbers that are not in the data.\n"
    "- If the data has market_research (web research per country), use it to choose audience, interests, angles and "
    "proof points for each country. Use only facts in it.\n"
    "- Write every ad and post in English only.\n"
    "- call_to_action must be one of: " + ", ".join(CTAS) + ".\n"
    "- Stay within the daily budget. Respect Meta advertising policies; no sensitive-trait targeting.\n"
    "- organic_plan: 5-7 posts for the next 7 days on Instagram and TikTok that feed the ads: if posts that made "
    "money exist, repeat their winning pattern; otherwise test clearly different hooks so the winners can be "
    "boosted later. Each post asks people to use its own link or code (ProAds creates them).\n"
    "Reply with only a JSON object in this shape:\n"
    '{"name": str, "objective": "OUTCOME_SALES" or "OUTCOME_LEADS", "optimization": str, "daily_budget": number, '
    '"currency": str, "rationale": str, "targeting": {"countries": [two-letter codes], "age_min": number}, '
    '"ad_sets": [{"name": str, "audience": str, "budget_share": number, "placements": str, '
    '"ads": [{"source_post_id": str or null, "format": "single image", "hook": str, "offer": str, '
    '"primary_text": str, "headline": str, "call_to_action": str, "why": str}]}], '
    '"organic_plan": [{"day": str, "platform": "instagram" or "tiktok", "format": str, "hook": str, '
    '"caption": str, "why": str}], '
    '"tests": [str], "kill_rules": [str], "what_to_watch": [str]}'
)

SYSTEM_TIKTOK = (
    "You are a TikTok campaign BUILDER, not an analyst. Output the execution spec only: what to build, nothing about why. "
    "Plan a TikTok campaign (TikTok placement only, website conversions) for this business. It will be published as written.\n"
    "Rules:\n"
    "- Build EXACTLY what the notes ask. If the notes give a number of campaigns, ad groups, ads or videos, a budget, a bid "
    "method, an audience or an age range, use them as written and add nothing: no extra ads, angles or audiences. Only "
    "when the notes say nothing: ONE ad group for new buyers (all the budget, broad targeting; a second ad group only for "
    "retargeting, with a bigger budget, and its name must start with 'RT') and 3-5 clearly different ads, plus the "
    "business's best organic posts as Spark Ads when they exist (source_post_id).\n"
    "- CONSOLIDATE: default to the structure the business already has, not new campaigns. If the brief has a 'placement' with "
    "action create_adgroup_in_existing_campaign, build ONLY the new ad group (one, unless the notes ask for more) and its ads for that campaign: "
    "use the campaign's name as 'name', give the ad group a name different from the ones it already has, and use only the "
    "budget in the brief for it. A new audience, bid or budget test is a new ad group; only a different creative angle is "
    "new ads. Never split a small budget across more ad groups or campaigns, and budget changes stay within 30% each time, never frequent, and campaigns use ad-group budgets (not CBO), so each ad group keeps its own budget.\n"
    "- Write about what THIS business actually sells (its real product names and category) from the product description "
    "and data. Never describe it as something else. Never invent prices, results, reviews, discounts or numbers.\n"
    "- Ads are short vertical videos. Per ad: hook = ad name, what is said or shown in the first 2 seconds (max 40 "
    "characters); primary_text = caption under the video (max 100 characters, at most 2 hashtags); headline; "
    "video_brief = creative direction, what the video shows; call_to_action one of SHOP_NOW, ORDER_NOW, LEARN_MORE. "
    "Native TikTok style, no TV-ad tone. When the data has the price, free delivery or instalments (Tabby/Tamara), put "
    "them in the first seconds or the caption.\n"
    "- optimization: 'purchase' if the notes ask for purchases; otherwise 'purchase' when the business has about 20+ "
    "verified purchases a week or the daily budget is at least budget_needed_for_learning in the data, else 'checkout'.\n"
    "- bid_strategy: 'Maximum Delivery' with no bid cap and no cost cap, unless the notes say otherwise. Never put a "
    "target CPA or cost cap in the setup.\n"
    "- If the data has market_research (web research per country), use it to choose audience, interests, creative direction "
    "and proof points (price, payment methods, delivery) for each country. Use only facts in it. Never copy it into the output.\n"
    "- Learning is ON (never 'skip learning'). Stay within the daily budget. Respect TikTok advertising policies; no "
    "sensitive-trait targeting. English unless the notes ask for Arabic.\n"
    "- Output boundary: the reply is the JSON object and nothing else: no intro line, no summary of the brief, no text before or after. Must-build items are the plan fields; notes are optional and never needed to build.\n"
    "- Keep a line only if it changes what gets built. NO explanations, strategy, rationale, learning commentary, "
    "performance interpretation, QA language ('checks passed'), and no 'why', 'tests', 'stop rules' or 'what to watch'. "
    "Leave rationale empty, tests, kill_rules, what_to_watch and organic_plan as [] and every ad 'why' empty.\n"
    "- notes: at most 3 short lines, each under 120 characters, none explaining setup: (1) a verified-fact context line "
    "only if the data shows it (e.g. '2 verified paid orders in the last 7 days'); (2) the guardrail 'Don't change "
    "settings for 5-7 days unless delivery is clearly broken'; (3) if the notes give a break-even/CPA figure: "
    "'<figure> is internal only, not a bid cap'. Otherwise leave notes out.\n"
    "Reply with only a JSON object in this shape:\n"
    '{"name": str, "platform": "TikTok", "objective": "WEB_CONVERSIONS", "conversion_location": "Website", '
    '"optimization": "purchase" or "checkout" or "add_to_cart", "bid_strategy": str, "daily_budget": number, '
    '"currency": str, "rationale": "", "targeting": {"countries": [two-letter codes], "age_min": number}, '
    '"ad_sets": [{"name": str, "audience": "broad" or "interest" or "retargeting", "interests": [str], '
    '"budget_share": number, "placements": "TikTok", '
    '"ads": [{"source_post_id": str or null, "format": "video", "hook": str, "video_brief": str, '
    '"primary_text": str, "headline": str, "call_to_action": str, "why": ""}]}], '
    '"organic_plan": [], "tests": [], "kill_rules": [], "what_to_watch": [], "notes": [str]}'
)

PROADS_PRODUCT = (
    "ProAds (pro.ezdevsoft.net) is free software for UAE businesses and agencies that advertise on Instagram, "
    "Facebook and TikTok. A business uploads its sales list (Excel/CSV) or connects Shopify and gets a free audit "
    "in its first week: discount codes that lose money, products that get refunded most, posts that bring buyers "
    "versus posts that only get likes, real profit after refunds, repeat customers, and a to-do list. Every post "
    "gets its own tracking link or code (works for WhatsApp, cash and walk-in sales). ProAds sends Meta the real "
    "value of each sale so ads find people who pay, not just viewers. Has an AI assistant for questions about "
    "your own numbers. Free during early access, no card needed. Sign-up takes a minute."
)


def ask(ai: MetaAI, question: str, context: dict, history: Optional[list[dict]] = None) -> Reply:
    messages = [{"role": "developer", "content": SYSTEM_CHAT},
                {"role": "user", "content": "Business data (JSON):\n" + json.dumps(context, ensure_ascii=False, default=str)},
                {"role": "assistant", "content": "Got it. What would you like to know?"}]
    messages += history or []
    messages.append({"role": "user", "content": question})
    return ai.chat("chat", messages, max_completion_tokens=8192)


class CampaignError(ValueError):
    pass


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise CampaignError("the model did not return a JSON campaign")
    return json.loads(text[start:end + 1])


def validate_campaign(plan: dict, daily_budget: float) -> list[str]:
    """Checks a person would do before trusting a plan. Returns problems found."""
    problems = []
    for key in ("name", "objective", "daily_budget", "ad_sets"):
        if key not in plan:
            problems.append(f"missing {key}")
    if problems:
        return problems
    if float(plan["daily_budget"]) > daily_budget * 1.001:
        problems.append(f"budget {plan['daily_budget']} is above the limit {daily_budget}")
    shares = [float(s.get("budget_share", 0)) for s in plan["ad_sets"]]
    if plan["ad_sets"] and abs(sum(shares) - 1.0) > 0.05 and abs(sum(shares) - 100) > 5:
        problems.append(f"ad set budget shares add up to {sum(shares)}, not 100%")
    if not any(s.get("ads") for s in plan["ad_sets"]):
        problems.append("no ads in any ad set")
    for s in plan["ad_sets"]:
        for ad in s.get("ads") or []:
            if not (ad.get("headline") and ad.get("primary_text")):
                problems.append(f"an ad in '{s.get('name', 'ad set')}' has no headline or text")
    return problems


def tidy_plan(plan: dict, conversion: str) -> dict:
    """Make the plan safe to publish: known call-to-action buttons, the right objective, sane ages."""
    plan["objective"] = "OUTCOME_LEADS" if conversion == "signup" else "OUTCOME_SALES"
    plan["conversion"] = conversion
    t = plan.get("targeting") if isinstance(plan.get("targeting"), dict) else {}
    countries = [c.upper() for c in t.get("countries") or [] if isinstance(c, str) and len(c) == 2] or ["AE"]
    try:
        age_min = int(t.get("age_min") or 18)
    except (TypeError, ValueError):
        age_min = 18
    plan["targeting"] = {"countries": countries[:25], "age_min": max(18, min(age_min, 25))}
    default_cta = "SIGN_UP" if conversion == "signup" else "SHOP_NOW"
    for s in plan.get("ad_sets") or []:
        for ad in s.get("ads") or []:
            cta = str(ad.get("call_to_action") or "").upper().replace(" ", "_")
            ad["call_to_action"] = cta if cta in CTAS and cta != "WHATSAPP_MESSAGE" else default_cta
            ad["format"] = "single image"
            ad["headline"] = str(ad.get("headline") or "")[:40]
            ad["primary_text"] = str(ad.get("primary_text") or "")[:600]
    plan["organic_plan"] = [p for p in plan.get("organic_plan") or [] if isinstance(p, dict)][:10]
    return plan


NOTE_BANNED = ("why", "because", "learning", "expect", "monitor", "watch", "test", "volatil", "should perform",
               "checks passed", "threshold", "hypothes", "optimi", "strategy", "recommend")


def tidy_tiktok(plan: dict) -> dict:
    plan["objective"] = "WEB_CONVERSIONS"
    plan["platform"] = "tiktok"
    o = str(plan.get("optimization")).lower()
    plan["optimization"] = o if o in ("purchase", "checkout", "add_to_cart") else "purchase"
    t = plan.get("targeting") if isinstance(plan.get("targeting"), dict) else {}
    countries = [c.upper() for c in t.get("countries") or [] if isinstance(c, str) and len(c) == 2] or ["AE"]
    try:
        age_min = int(t.get("age_min") or 18)
    except (TypeError, ValueError):
        age_min = 18
    plan["targeting"] = {"countries": countries[:10], "age_min": max(18, min(age_min, 25))}
    for s in plan.get("ad_sets") or []:
        s["placements"] = "TikTok"
        for ad in s.get("ads") or []:
            cta = str(ad.get("call_to_action") or "").upper().replace(" ", "_")
            ad["call_to_action"] = {"BUY_NOW": "SHOP_NOW"}.get(cta, cta) if cta in ("SHOP_NOW", "ORDER_NOW", "LEARN_MORE", "BUY_NOW") else "SHOP_NOW"
            ad["format"] = "video"
            ad["headline"] = str(ad.get("headline") or ad.get("hook") or "")[:40]
            ad["primary_text"] = str(ad.get("primary_text") or "")[:100]
    plan["organic_plan"] = [p for p in plan.get("organic_plan") or [] if isinstance(p, dict)][:10]
    # execution spec only: anything outside the build fields is dropped, whatever the model wrote
    plan.setdefault("conversion_location", "Website")
    plan["bid_strategy"] = str(plan.get("bid_strategy") or "Maximum Delivery")
    plan["name"] = str(plan.get("name") or "")[:100]
    for s in plan.get("ad_sets") or []:
        s["interests"] = [str(i)[:60] for i in (s.get("interests") or []) if str(i).strip()][:15] if isinstance(s.get("interests"), list) else []
        for ad in s.get("ads") or []:
            ad["why"] = ""
            ad["video_brief"] = str(ad.get("video_brief") or "")[:280]
            ad["hook"] = str(ad.get("hook") or "")[:40]
        s_keep = ("name", "audience", "interests", "budget_share", "placements", "ads")
        for k in [k for k in s if k not in s_keep]:
            del s[k]
        ad_keep = ("source_post_id", "format", "hook", "video_brief", "primary_text", "headline", "call_to_action", "why")
        for ad in s.get("ads") or []:
            for k in [k for k in ad if k not in ad_keep]:
                del ad[k]
    notes = plan.get("notes") if isinstance(plan.get("notes"), list) else []
    plan["notes"] = [str(n).strip()[:120] for n in notes
                     if str(n).strip() and not any(w in str(n).lower() for w in NOTE_BANNED)][:3]
    keep = ("name", "platform", "objective", "conversion_location", "optimization", "bid_strategy", "daily_budget",
            "currency", "targeting", "ad_sets", "notes")
    for k in [k for k in plan if k not in keep]:
        del plan[k]
    plan["rationale"] = ""                      # empty, not missing, so screens that read these keys keep working
    plan["organic_plan"], plan["tests"], plan["kill_rules"], plan["what_to_watch"] = [], [], [], []
    return plan


COUNTRY_NAMES = {"AE": "United Arab Emirates", "SA": "Saudi Arabia", "KW": "Kuwait", "QA": "Qatar", "BH": "Bahrain",
                 "OM": "Oman", "EG": "Egypt", "JO": "Jordan", "US": "United States", "GB": "United Kingdom",
                 "TH": "Thailand", "IN": "India"}

MARKET_QUERIES = (
    "{product} best selling online {country} {year}",
    "{country} online shopping buying habits payment methods cash on delivery Tabby Tamara",
    "{product} TikTok ads {country} creatives that drive purchases",
    "{product} competitors ads offers price {country}",
    "{country} peak shopping days seasons demand {product}",
)

SYSTEM_RESEARCH = (
    "You summarise web search results about ONE country market for an ads builder. Use ONLY the snippets given: every "
    "item must be supported by a snippet. Never invent numbers, prices or sources; if the snippets don't support "
    "something, leave that list empty. Reply with only a JSON object: "
    '{"country": str, "what_sells": [str], "buyer_habits": [str], "payment_methods": [str], "price_range": str or null, '
    '"peak_times": [str], "creative_styles_that_convert": [str], "competitor_angles": [str], "sources": [url]}. '
    "Keep every item under 140 characters."
)


def brave_search(query: str, count: int = 8) -> str:
    """Web search through the Brave Search API (key in PAYSIGNAL_SEARCH_KEY). Returns plain-text snippets."""
    import requests
    key = os.environ.get("PAYSIGNAL_SEARCH_KEY", "").strip()
    if not key:
        return ""
    r = requests.get("https://api.search.brave.com/res/v1/web/search", timeout=30,
                     params={"q": query, "count": count},
                     headers={"X-Subscription-Token": key, "Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"search failed: HTTP {r.status_code}")
    rows = ((r.json() or {}).get("web") or {}).get("results") or []
    return "\n".join(f"- {x.get('title', '')}: {x.get('description', '')} ({x.get('url', '')})" for x in rows[:count])


def research_markets(ai: MetaAI, countries: list[str], product: str, search: Optional[Callable[[str], str]] = None,
                     max_countries: int = 12) -> tuple[dict, list[str]]:
    """Deep web research per target country: what sells, how people pay, what creatives drive purchases.
    Returns ({country: summary}, problems). Needs a search function (default: Brave, PAYSIGNAL_SEARCH_KEY)."""
    search = search or (brave_search if os.environ.get("PAYSIGNAL_SEARCH_KEY") else None)
    if search is None:
        return {}, ["Market research skipped: no search API is set up (add PAYSIGNAL_SEARCH_KEY in Render)."]
    out, problems = {}, []
    for code in [c.upper() for c in countries][:max_countries]:
        name = COUNTRY_NAMES.get(code, code)
        queries = [q.format(product=product or "", country=name, year=date.today().year).strip() for q in MARKET_QUERIES]
        with ThreadPoolExecutor(max_workers=5) as pool:
            def one(q):
                try:
                    return search(q) or ""
                except Exception as e:
                    log.warning("market search failed (%s): %s", q, e)
                    return ""
            snippets = [t for t in pool.map(one, queries) if t]
        if not snippets:
            problems.append(f"Market research for {name}: no search results.")
            continue
        try:
            reply = ai.chat("campaign_builder", [
                {"role": "developer", "content": SYSTEM_RESEARCH},
                {"role": "user", "content": f"Country: {name}\nProduct: {product}\n\nSearch results:\n" + "\n\n".join(snippets)}],
                max_completion_tokens=3000, reasoning_effort="medium")
            out[code] = _extract_json(reply.text)
        except Exception as e:
            log.warning("market research summary failed for %s: %s", code, e)
            problems.append(f"Market research for {name} failed: {str(e)[:120]}")
    return out, problems


def build_campaign(ai: MetaAI, context: dict, daily_budget: float, goal: str = "",
                   extra_instructions: str = "", conversion: str = "purchase", product: str = "",
                   landing_url: str = "", budget_currency: str = "", platform: str = "meta",
                   countries: Optional[list] = None, research: bool = True,
                   search: Optional[Callable[[str], str]] = None, placement: Optional[dict] = None) -> dict:
    conversion = "signup" if conversion == "signup" else "purchase"
    brief = {"daily_budget": daily_budget, "currency": budget_currency or context.get("currency", "AED"),
             "conversion": conversion,
             "goal": goal or ("sign-ups from businesses that go on to use the product" if conversion == "signup"
                              else "more paying customers who keep what they buy"),
             "product": product, "landing_page": landing_url, "notes": extra_instructions}
    tiktok = platform == "tiktok"
    countries = [c.upper() for c in (countries or []) if isinstance(c, str) and len(c) == 2] or ["AE"]
    brief["countries"] = countries
    if placement and placement.get("action") == "create_adgroup_in_existing_campaign" and tiktok:
        c = placement["campaign"]
        brief["placement"] = {"action": "create_adgroup_in_existing_campaign", "campaign": c["name"],
                              "existing_ad_groups": [g.get("name") for g in c.get("ad_groups") or []][:6]}
    market, research_problems = (research_markets(ai, countries, product, search) if research else ({}, []))
    if market:
        context = {**context, "market_research": market}
    reply = ai.chat("campaign_builder", [
        {"role": "developer", "content": SYSTEM_TIKTOK if tiktok else SYSTEM_CAMPAIGN},
        {"role": "user", "content": "Brief:\n" + json.dumps(brief, ensure_ascii=False) +
                                    "\n\nBusiness data:\n" + json.dumps(context, ensure_ascii=False, default=str)}],
        max_completion_tokens=16000)
    try:
        plan = _extract_json(reply.text)
    except (json.JSONDecodeError, CampaignError) as e:
        raise CampaignError(f"could not read the campaign the model returned: {e}") from e
    if tiktok:
        for s_ in plan.get("ad_sets") or []:             # TikTok ads have a hook, not a headline: fill it first
            for ad in s_.get("ads") or []:
                ad.setdefault("headline", ad.get("hook") or "")
    notes = list(research_problems)
    try:                                                   # the budget box is the limit: an AI that copied a bigger
        if daily_budget and float(plan.get("daily_budget") or 0) > daily_budget * 1.001:   # number from the notes is
            notes.append(f"Budget set to {daily_budget:g} a day, the amount in the budget box (the plan asked for "   # brought back,
                         f"{float(plan['daily_budget']):g}). Change the budget box and build again to spend more.")    # not blocked
            plan["daily_budget"] = daily_budget
    except (TypeError, ValueError):
        pass
    problems = validate_campaign(plan, daily_budget)
    if not problems:
        plan = tidy_tiktok(plan) if tiktok else tidy_plan(plan, conversion)
        plan["currency"] = brief["currency"]
    return {"plan": plan, "problems": problems, "notes": notes, "model": reply.model, "landing_url": landing_url,
            "research_sources": {c: m.get("sources", []) for c, m in market.items()},
            "cost_usd": reply.cost_usd, "status": "draft - review before launching"}
