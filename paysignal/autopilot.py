"""Autonomous Experiment Engine: autonomous intelligence, controlled execution.

Stage 1  discover()    reads Buyer quality, Profit engine (verdicts + autopsy), Money leaks and Meta learning and
                       turns them into opportunities, each with evidence, a strength and an honest benefit range.
Stage 2  proposals()   picks three GENUINELY different experiments (different kinds of change).
Stage 3  test_budget() sample size, budget, duration and maximum acceptable loss; says so when a business can't
                       afford a meaningful test.
         next_best()   "this is the one I'd run first", with the reasons.
Stage 4  MetaTest      prepares the variant as a deep copy of the control (PAUSED) with exactly ONE change, plus a
                       Meta A/B test (ad study) for a clean audience split. Nothing is created before approval, and
                       nothing goes live before a second, explicit start.
Stage 5  (server)      monitors spend, purchases and refunds; verdicts only from settled results (buyers.evaluate);
                       the outcome feeds the next discovery (kept changes become the new baseline, rejected ideas
                       never return).

Safety rules enforced in code:
  - budgets are never increased: the copy keeps the control's budget and nothing here edits budgets;
  - nothing is published or modified without approval (two confirmations: create paused, then start);
  - no winner from provisional revenue; no confidence range when settled purchases are too few;
  - a campaign can be in only one active experiment, so tests never share a control group.
"""
from __future__ import annotations

import json
import math
import time
from typing import Optional

Z = 1.96 + 0.84                 # 95% two-sided, 80% power
MIN_SETTLED_PURCHASES = 20       # below this, no confidence range is shown at all
MAX_TEST_WEEKS = 8
BENEFIT_CAP = 0.5               # estimates never above +50%: patterns among customers rarely hold in full when tested
KIND_LABEL = {"product": "Product in the ad", "offer": "Offer", "creative": "Ad message", "landing": "Landing page",
              "audience": "Who sees it", "structure": "Campaign structure"}


# ---------------------------------------------------------------- stage 1: discovery
def discover(buyers_an: Optional[dict], engine_out: Optional[dict], leaks_out: Optional[dict],
             learning_out: Optional[dict], history: list[dict]) -> list[dict]:
    """Every opportunity carries: kind, what to change, why (evidence), strength 0-1, benefit range (estimate)."""
    ops = []
    # rejected ideas never return; kept ones are already applied (the variant is the new baseline)
    rejected = {(h.get("kind"), h.get("segment")) for h in history if h.get("decision") in ("reject", "keep")}
    inconclusive = {(h.get("kind"), h.get("segment")) for h in history if h.get("decision") == "inconclusive"}
    active = {(h.get("kind"), h.get("segment")) for h in history if h.get("status") in ("proposed", "approved", "running", "planned")}

    busy_kinds = {h.get("kind") for h in history if h.get("status") in ("approved", "running")}

    def add(**o):
        if (o["kind"], o["segment"]) in rejected or (o["kind"], o["segment"]) in active or o["kind"] in busy_kinds:
            return                                       # one live test per kind of change, so they can't muddle each other
        if (o["kind"], o["segment"]) in inconclusive:
            o["strength"] = round(o["strength"] * 0.7, 2)
            o["tried"] = "Tested before without a clear result: only worth repeating with a bigger budget."
        ops.append(o)

    # Buyer quality: strong patterns (95% range above zero). Patterns usually shrink when tested: halve the lift.
    if buyers_an and buyers_an.get("ready"):
        for d in buyers_an.get("dimensions", []):
            for s in d["segments"]:
                if s["verdict"] != "better" or not s.get("lift"):
                    continue
                kind = {"product": "product", "offer": "offer", "channel": "creative", "creative": "creative",
                        "campaign": "creative"}.get(d["key"])
                if not kind:
                    continue
                lo = max(0.0, s["interval"][0] / max(1e-9, s["avg_value"] - s["vs_rest"]))
                hi = s["lift"]
                change = {"product": f"Feature {s['name']} in the ad (image and first line)",
                          "offer": "Remove the discount from the ad" if s["name"] == "Full price" else f"Lead the ad with {s['name']}",
                          "creative": f"Rewrite the ad in the style that brought your best buyers ({s['name']})"}[kind]
                add(kind=kind, segment=s["name"], source="Buyer quality", change=change,
                    why=f"{s['customers']} buyers who first came through {s['name']} were worth {s['avg_value']:,.0f} each in "
                        f"{buyers_an['window_days']} days, {s['vs_rest']:+,.0f} vs others (95% range {s['interval'][0]:+,.0f} to "
                        f"{s['interval'][1]:+,.0f}). A pattern, not proof: the test checks if it's the cause.",
                    strength=min(0.9, 0.4 + s["customers"] / 250 + (0.15 if lo > 0.2 else 0)),
                    benefit=[round(min(lo / 2, BENEFIT_CAP / 2), 2), round(min(hi / 2, BENEFIT_CAP), 2)], metric="value",
                    evidence_note=f"pattern from {s['customers']} buyers, 95% range clear of zero")

    # Profit engine autopsy: where the journey breaks, on campaigns with enough traffic
    for c in (engine_out or {}).get("campaigns", []):
        a = c.get("autopsy") or {}
        f = (a.get("findings") or [None])[0]
        if not f:
            continue
        kind = {"ad": "creative", "audience": "audience", "page": "landing", "page_load": "landing", "cart": "offer",
                "checkout": "landing", "economics": "offer"}.get(f["stage"])
        if not kind or f["stage"] == "tracking":
            continue
        add(kind=kind, segment=f"{c.get('name') or c['campaign_id']}: {f['stage']}", source="Ad autopsy",
            change=f["next_experiment"], why=f"{f['evidence']} Likely cause: {f['likely_cause']}",
            strength=0.45 if f["severity"] >= 3 else 0.3, benefit=[0.05, 0.3], metric="value",
            control_campaign=c["campaign_id"])

    # Money leaks: discount codes that lose profit -> offer test
    for l in (leaks_out or {}).get("leaks", []):
        if l["kind"] == "discount" and l["how"] == "measured":
            add(kind="offer", segment=l["title"], source="Money leaks", change="Ads without the discount code; same everything else",
                why=f"{l['evidence']} Costing about {l['per_month']:,.0f} a month.", strength=0.55, benefit=[0.0, 0.25],
                metric="value")

    # Meta learning: budgets split too thin -> structure (a change, not a split test)
    for c in (learning_out or {}).get("campaigns", []):
        for adv in c.get("advice") or []:
            if adv["title"] == "Budget split too thin":
                add(kind="structure", segment=c.get("name") or c["campaign_id"], source="Meta learning",
                    change=adv["action"], why=adv["detail"], strength=0.5, benefit=[0.0, 0.2], metric="value",
                    control_campaign=c["campaign_id"], not_a_split_test=True)
    ops.sort(key=lambda o: -o["strength"])
    return ops


# ---------------------------------------------------------------- stage 3: budget
def purchases_needed(uplift: float) -> int:
    """Purchases per arm to detect a relative uplift in purchase rate (log-rate test, 95% / 80% power)."""
    r = 1 + uplift
    return math.ceil(Z ** 2 * (1 + 1 / r) / math.log(r) ** 2)


def test_budget(cost_per_purchase: Optional[float], margin_per_purchase: Optional[float],
                daily_budget: Optional[float], uplift: float = 0.3) -> dict:
    """What a meaningful test costs. Uses the business's own numbers; says plainly when it isn't affordable."""
    if not cost_per_purchase or cost_per_purchase <= 0:
        return {"affordable": None, "why": "Add your numbers in Profit engine (price, costs, daily budget) so ProAds can size the test."}
    out = None
    for up in (uplift, 0.5, 1.0):                       # if a 30% effect is too costly to detect, try bigger effects
        k = purchases_needed(up)
        budget = round(2 * k * cost_per_purchase, 2)
        days = math.ceil(budget / daily_budget) if daily_budget else None
        worst = 0.3                                      # if the variant were 30% worse than the control
        loss = round(k * worst * (margin_per_purchase or cost_per_purchase), 2)
        out = {"detectable_uplift": up, "purchases_per_arm": k, "budget": budget, "days": days,
               "max_acceptable_loss": loss, "cost_per_purchase": round(cost_per_purchase, 2),
               "affordable": bool(days and days <= MAX_TEST_WEEKS * 7)}
        if out["affordable"]:
            if up > uplift:
                out["note"] = (f"At your budget a test can only reliably spot a big change (about {up:.0%} better). "
                               "Smaller real improvements would look inconclusive.")
            return out
    out["why"] = (f"A fair test needs about {out['purchases_per_arm']} purchases per side, about {out['budget']:,.0f} in total"
                  + (f" ({out['days']} days at your budget)" if out["days"] else "")
                  + ". That's more than a sensible test window, so ProAds won't pretend a smaller test would prove anything. "
                    "Fix money leaks and grow volume first, or test only big, bold changes.")
    return out


# ---------------------------------------------------------------- stage 2 + next best
def proposals(ops: list[dict], sizing: dict) -> list[dict]:
    """Three genuinely different experiments: one per kind of change, strongest first."""
    seen, out = set(), []
    for o in ops:
        if o["kind"] in seen:
            continue
        seen.add(o["kind"])
        keep = ["Audience and countries", "Budget (never raised by ProAds)", "Placements", "Optimisation event",
                "Attribution setting"]
        if o["kind"] != "offer":
            keep.append("Offer and price")
        if o["kind"] in ("creative", "product", "offer"):
            keep.append("Image")                       # only the words change in the ad
        else:
            keep.append("Ad text and image")
        if o["kind"] != "landing":
            keep.append("Landing page")
        out.append({**o, "key": f"{o['kind']}:{o['segment']}", "label": KIND_LABEL[o["kind"]],
                    "unchanged": keep, "budget": sizing,
                    "measure": "Settled net value (after refunds) per unit spent, control vs variant; guardrails: refund "
                               "rate and cost per purchase vs your break-even. Verdict only from settled purchases.",
                    "benefit_text": (f"Estimate: {o['benefit'][0]:+.0%} to {o['benefit'][1]:+.0%} net value per spend. "
                                     "Not a promise; the test decides.")})
        if len(out) == 3:
            break
    return out


def next_best(props: list[dict]) -> Optional[dict]:
    """Strongest evidence, lowest cost, highest potential, lowest downside, in that order of weight."""
    if not props:
        return None
    def score(p):
        b = p["budget"] or {}
        cost = 0.0 if b.get("affordable") is False else 1.0 / (1 + (b.get("budget") or 0) / 1000)
        potential = sum(p["benefit"]) / 2
        downside = 1.0 if p.get("not_a_split_test") else 0.8 if p["kind"] in ("creative", "product") else 0.6
        return 0.35 * p["strength"] + 0.25 * cost + 0.25 * min(1.0, potential * 2) + 0.15 * downside
    ranked = sorted(props, key=score, reverse=True)
    best = ranked[0]
    grade = "strong" if best["strength"] >= 0.75 else "moderate" if best["strength"] >= 0.5 else "weak"
    reasons = [f"Evidence: {grade}, from {best['source']}" + (f" ({best['evidence_note']})" if best.get("evidence_note") else "") + ".",
               f"Potential: {best['benefit_text']}",
               "Low downside: only the variant's half of the test budget is at risk, and ProAds watches the loss limit."
               if not best.get("not_a_split_test") else "Low downside: a structure fix, no split test needed."]
    b = best["budget"] or {}
    if b.get("affordable"):
        reasons.insert(1, f"Cost: about {b['budget']:,.0f} over about {b['days']} days.")
    elif b.get("affordable") is False:
        reasons.insert(1, "Cost: a fully reliable test is not affordable at today's volume; " + (b.get("why") or ""))
    return {"key": best["key"], "reasons": reasons, "ranking": [p["key"] for p in ranked]}


# ---------------------------------------------------------------- stage 4: Meta execution
class MetaTestError(RuntimeError):
    pass


class MetaTest:
    """Prepares and runs a test through supported Meta Marketing API calls. Never edits budgets."""

    def __init__(self, http, token: str, ad_account: str, version: str = "v25.0"):
        self.http, self.token, self.acct, self.v = http, token, ad_account, version

    def _url(self, path: str) -> str:
        return f"https://graph.facebook.com/{self.v}/{path}"

    def _check(self, r, step: str) -> dict:
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code != 200 or "error" in body:
            e = body.get("error") or {}
            raise MetaTestError(f"Meta refused {step}: {e.get('error_user_msg') or e.get('message') or r.status_code}")
        return body

    def get(self, path: str, fields: str, step: str) -> dict:
        return self._check(self.http.get(self._url(path), params={"access_token": self.token, "fields": fields}, timeout=30), step)

    def post(self, path: str, data: dict, step: str) -> dict:
        data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in data.items()}
        data["access_token"] = self.token
        if any(k in data for k in ("daily_budget", "lifetime_budget", "bid_amount")):
            raise MetaTestError("ProAds never changes budgets or bids in a test")      # safety rule, enforced
        return self._check(self.http.post(self._url(path), data=data, timeout=60), step)

    def snapshot(self, campaign_id: str) -> dict:
        """The control as Meta has it: ad sets (budget, targeting, event) and ads with their text and link."""
        sets = self.get(f"{campaign_id}/adsets", "id,name,daily_budget,optimization_goal,promoted_object,targeting,attribution_spec",
                        "reading the control's ad sets").get("data", [])
        ads = self.get(f"{campaign_id}/ads", "id,name,adset_id,creative{id,object_story_spec}", "reading the control's ads").get("data", [])
        return {"campaign_id": campaign_id, "adsets": sets, "ads": ads}

    @staticmethod
    def preview(snapshot: dict, change: dict) -> list[dict]:
        """Exactly what will differ between control and variant (for the owner to approve)."""
        rows = []
        for ad in snapshot["ads"]:
            ld = ((ad.get("creative") or {}).get("object_story_spec") or {}).get("link_data") or {}
            for field, label in (("message", "Ad text"), ("name", "Headline"), ("link", "Landing page")):
                new = change.get(field)
                if new is not None and new != ld.get(field):
                    rows.append({"ad": ad.get("name") or ad["id"], "field": label, "control": ld.get(field), "variant": new})
        return rows

    def create_paused(self, snapshot: dict, change: dict, name: str, days: int) -> dict:
        """Deep-copy the control (paused, same budget), swap ONE thing in each ad's creative, set up an A/B test."""
        cp = self.post(f"{snapshot['campaign_id']}/copies", {"deep_copy": "true", "status_option": "PAUSED",
                                                              "rename_options": {"rename_suffix": " · ProAds test"}},
                       "copying the control campaign")
        new_id = cp.get("copied_campaign_id")
        if not new_id:
            raise MetaTestError("Meta did not return the copied campaign")
        ads = self.get(f"{new_id}/ads", "id,name,creative{id,object_story_spec}", "reading the copied ads").get("data", [])
        swapped = []
        for ad in ads:
            spec = json.loads(json.dumps((ad.get("creative") or {}).get("object_story_spec") or {}))
            ld = spec.setdefault("link_data", {})
            for field in ("message", "name", "link"):
                if change.get(field) is not None:
                    ld[field] = change[field]
                    if field == "link" and "call_to_action" in ld:
                        ld["call_to_action"].setdefault("value", {})["link"] = change[field]
            cr = self.post(f"{self.acct}/adcreatives", {"name": f"{name} variant", "object_story_spec": spec},
                           "creating the variant's ad")
            self.post(ad["id"], {"creative": {"creative_id": cr["id"]}}, "putting the variant's ad in place")
            swapped.append({"ad_id": ad["id"], "creative_id": cr["id"]})
        start = int(time.time()) + 3600
        study = self.post(f"{self.acct}/ad_studies", {
            "name": name[:100], "type": "SPLIT_TEST", "start_time": start, "end_time": start + days * 86400,
            "cells": [{"name": "Control", "treatment_percentage": 50, "campaigns": [snapshot["campaign_id"]]},
                      {"name": "Variant", "treatment_percentage": 50, "campaigns": [new_id]}]},
            "setting up Meta's A/B test")
        return {"variant_campaign": new_id, "ads": swapped, "split_test_id": study.get("id")}

    def set_status(self, campaign_id: str, status: str) -> None:
        self.post(campaign_id, {"status": status}, f"switching campaign {campaign_id} to {status.lower()}")
