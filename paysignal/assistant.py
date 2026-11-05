"""AI features on top of the engine, powered by Meta's best model (router tasks
'chat' and 'campaign_builder').

  ask()             answer questions about a client's results in plain language
  build_campaign()  draft a full Meta ad campaign from what organically made money

Privacy: the AI only ever sees aggregated numbers (totals, per-post results,
patterns). Customer names, emails, phones and individual payments are never sent.
Campaign plans are drafts for a person to review; nothing is launched.
"""
from __future__ import annotations

import json
from typing import Optional

from .ai import MetaAI, Reply
from .engine import Engine
from .organic import OrganicEngine


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
    "You are PaySignal's analyst for a marketing agency. You answer questions about one business's "
    "results using ONLY the data provided. Key ideas: 'real value' is profit after refunds; posts are "
    "judged by real value per 1,000 views, not likes; 'est_extra_value' is an estimate of sales that came "
    "without a link. Patterns marked confident=false are not proven yet: say so instead of presenting them "
    "as fact. If the data can't answer the question, say what data is missing. Be short and concrete, give "
    "numbers, and reply in the language the question was asked in."
)

SYSTEM_CAMPAIGN = (
    "You are a senior Meta ads strategist. Draft a Meta (Facebook/Instagram) campaign for this business, "
    "built on what already made money organically. Rules:\n"
    "- Optimise for Purchase with value (the business sends real profit values through the Conversions API).\n"
    "- Prefer broad targeting and few ad sets: Meta's delivery system finds buyers when given good signals.\n"
    "- Use the boost_candidates and the confident winning patterns for creatives. Never build on patterns "
    "marked confident=false without labelling them as tests.\n"
    "- Avoid what lost money (high refunds, watchers-only hooks).\n"
    "- Stay within the given budget. Respect Meta advertising policies; no sensitive-trait targeting.\n"
    "Reply with only a JSON object in this shape:\n"
    '{"name": str, "objective": "OUTCOME_SALES", "optimization": str, "daily_budget": number, '
    '"currency": str, "rationale": str, "ad_sets": [{"name": str, "audience": str, "budget_share": number, '
    '"placements": str, "ads": [{"source_post_id": str or null, "format": str, "hook": str, "offer": str, '
    '"primary_text": str, "headline": str, "call_to_action": str, "why": str}]}], '
    '"tests": [str], "kill_rules": [str], "what_to_watch": [str]}'
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
    return problems


def build_campaign(ai: MetaAI, context: dict, daily_budget: float, goal: str = "",
                   extra_instructions: str = "") -> dict:
    brief = {"daily_budget": daily_budget, "currency": context.get("currency", "AED"),
             "goal": goal or "more paying customers who keep what they buy", "notes": extra_instructions}
    reply = ai.chat("campaign_builder", [
        {"role": "developer", "content": SYSTEM_CAMPAIGN},
        {"role": "user", "content": "Brief:\n" + json.dumps(brief, ensure_ascii=False) +
                                    "\n\nBusiness data:\n" + json.dumps(context, ensure_ascii=False, default=str)}],
        max_completion_tokens=16000)
    try:
        plan = _extract_json(reply.text)
    except (json.JSONDecodeError, CampaignError) as e:
        raise CampaignError(f"could not read the campaign the model returned: {e}") from e
    return {"plan": plan, "problems": validate_campaign(plan, daily_budget), "model": reply.model,
            "cost_usd": reply.cost_usd, "status": "draft - review before launching"}
