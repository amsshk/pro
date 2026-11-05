"""Post labeller: fills in each post's format, hook and offer from its caption.

Two layers:
  RuleLabeler   free, offline, instant. English + Arabic keywords and emojis.
  ClaudeLabeler optional, more accurate on unusual captions. Uses the Claude API,
                labels up to 20 posts per request, and falls back to rules on any error.

Labels a person already filled in are never overwritten.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

FORMATS = ("reel", "story", "carousel", "image", "video", "live")
HOOKS = ("before_after", "testimonial", "tutorial", "trend_meme", "behind_scenes",
         "product_showcase", "other")
OFFERS = ("none", "discount", "free_gift", "bundle")

HOOK_MEANING = {
    "before_after": "shows a transformation or result, before vs after",
    "testimonial": "a customer's review, story or rating",
    "tutorial": "teaches how to do or use something, steps or tips",
    "trend_meme": "trend, meme, POV, joke or challenge aimed at entertainment",
    "behind_scenes": "the team, the workplace, how things are made",
    "product_showcase": "presents a product or service, new arrival or launch",
    "other": "none of the above",
}

_MEDIA = {"reels": "reel", "reel": "reel", "story": "story", "stories": "story",
          "carousel_album": "carousel", "carousel": "carousel", "image": "image", "photo": "image",
          "video": "video", "live": "live", "live_video": "live"}

# keyword patterns, most specific first; Arabic is matched after normalisation
_HOOK_RULES = {
    "testimonial": [r"\breview", r"\btestimonial", r"\bour (client|customer)", r"\bcustomers?\b", r"\bfeedback",
                    r"\b(she|he|they) said", r"⭐", r"★", r"\brated\b", r"\bloved it", r"تجربة", r"رأي", r"تقييم",
                    r"عميل", r"عملائنا", r"زبون"],
    "tutorial": [r"\bhow to\b", r"\bsteps?\b", r"\btips?\b", r"\bguide\b", r"\btutorial", r"\broutine\b",
                 r"\bdo'?s and don'?ts", r"\bhere'?s how", r"طريقة", r"خطوات", r"نصائح", r"نصيحة", r"كيف",
                 r"تعلم", r"روتين"],
    "before_after": [r"before\s*(and|&|/|\+)\s*after", r"\btransformation", r"\bresults?\b", r"\bglow[- ]?up",
                     r"قبل\s*و\s*بعد", r"النتيجة", r"النتائج", r"الفرق"],
    "trend_meme": [r"\bpov\b", r"\btrend", r"\bwhen you\b", r"\bme when\b", r"😂", r"🤣", r"\bmeme", r"\brelatable",
                   r"\bchallenge\b", r"\btag (a|your) friend", r"ترند", r"تحدي"],
    "behind_scenes": [r"behind the scenes", r"\bbts\b", r"\bour team\b", r"\ba day in\b", r"\bmeet the\b",
                      r"\bhow we make", r"كواليس", r"فريقنا", r"فريق العمل", r"يوم في"],
    "product_showcase": [r"\bnew arrival", r"\bintroducing\b", r"\blaunch", r"\bnow available", r"\bjust dropped",
                         r"\bmeet our new\b", r"\bnew in\b", r"\bin stock\b", r"جديد", r"وصل", r"متوفر", r"اطلاق"],
}
_OFFER_RULES = {   # checked in this order: the most specific offer wins
    "bundle": [r"\bbundle", r"\bbuy \w+ get \w+", r"\bbogo\b", r"\b1\s*\+\s*1\b", r"\bpackage\b", r"باقة", r"اشتر"],
    "free_gift": [r"\bfree gift", r"\bgift with", r"\bcomplimentary", r"\bfree (sample|session|consultation)",
                  r"هدية", r"مجان"],
    "discount": [r"\d+\s*%", r"\b\d+\s*off\b", r"\b% off\b", r"\bsale\b", r"\bdiscount", r"\bpromo", r"\bcode\b",
                 r"\bdeal\b", r"\bfree (shipping|delivery)", r"خصم", r"تخفيض", r"تخفيضات", r"عرض", r"كود"],
}


def _norm(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[ً-ْـ]", "", t)            # Arabic diacritics and tatweel
    t = re.sub("[إأآا]", "ا", t).replace("ى", "ي").replace("ة", "ة")
    return t


@dataclass
class Labels:
    format: str
    hook: str
    offer: str
    confidence: float     # 0-1, how sure the labeller is about the hook
    source: str           # "rules" | "ai"


class RuleLabeler:
    def label(self, caption: str, media_type: str = "", platform: str = "") -> Labels:
        text = _norm(caption)
        fmt = _MEDIA.get(media_type.strip().lower(), "")
        if not fmt:
            if platform.lower() == "tiktok":
                fmt = "video"
            elif re.search(r"\bswipe\b|اسحب", text):
                fmt = "carousel"
            else:
                fmt = "unknown"

        hits = {h: sum(bool(re.search(p, text)) for p in pats) for h, pats in _HOOK_RULES.items()}
        best = max(hits, key=lambda h: hits[h])          # dict order breaks ties
        if hits[best] == 0:
            hook, conf = "other", 0.2
        else:
            second = sorted(hits.values())[-2]
            hook, conf = best, min(0.5 + 0.2 * hits[best] - 0.15 * second, 0.95)

        offer = next((o for o, pats in _OFFER_RULES.items() if any(re.search(p, text) for p in pats)), "none")
        return Labels(fmt, hook, offer, round(max(conf, 0.2), 2), "rules")


class ClaudeLabeler:
    """Labels with the Claude API. Needs ANTHROPIC_API_KEY (from console.anthropic.com).
    Sends captions only: no customer data, no payment data."""
    URL = "https://api.anthropic.com/v1/messages"
    VERSION = "2026-10-21"
    BATCH = 20

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, session=None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("PAYSIGNAL_LABEL_MODEL", "claude-haiku-5-5")
        self.session = session
        self.rules = RuleLabeler()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _prompt(self, items: list[dict]) -> str:
        hooks = "\n".join(f"- {h}: {m}" for h, m in HOOK_MEANING.items())
        return (
            "You label social media posts for a marketing analytics tool. For each post, choose:\n"
            f"format: one of {', '.join(FORMATS)} (use the given media_type if present)\n"
            f"hook: the main angle used to grab attention, one of:\n{hooks}\n"
            "offer: none, discount (any price cut, % off, code, sale, free shipping), "
            "free_gift (something extra for free), bundle (multi-item deal)\n"
            "Captions may be in English, Arabic or both.\n\n"
            "Reply with only a JSON array, one object per post, in the same order:\n"
            '[{"post_id": "...", "format": "...", "hook": "...", "offer": "...", "confidence": 0.0}]\n\n'
            f"Posts:\n{json.dumps(items, ensure_ascii=False)}"
        )

    def _call(self, prompt: str) -> str:
        if self.session is None:
            import requests
            self.session = requests.Session()
        r = self.session.post(self.URL, timeout=60, headers={
            "x-api-key": self.api_key, "anthropic-version": self.VERSION, "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 2048, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")

    @staticmethod
    def _parse(text: str) -> list[dict]:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end < start:
            raise ValueError("no JSON array in reply")
        return json.loads(text[start:end + 1])

    def label_many(self, posts: list) -> dict[str, Labels]:
        out: dict[str, Labels] = {}
        for i in range(0, len(posts), self.BATCH):
            chunk = posts[i:i + self.BATCH]
            items = [{"post_id": p.post_id, "platform": p.platform, "media_type": p.media_type,
                      "caption": (p.caption or "")[:1500]} for p in chunk]
            try:
                rows = {str(r.get("post_id")): r for r in self._parse(self._call(self._prompt(items)))}
            except Exception:
                rows = {}                                 # whole batch falls back to rules
            for p in chunk:
                rule = self.rules.label(p.caption, p.media_type, p.platform)
                r = rows.get(p.post_id)
                if not r:
                    out[p.post_id] = rule
                    continue
                # keep only valid values; anything odd falls back to the rule label
                fmt = r.get("format") if r.get("format") in FORMATS else rule.format
                hook = r.get("hook") if r.get("hook") in HOOKS else rule.hook
                offer = r.get("offer") if r.get("offer") in OFFERS else rule.offer
                try:
                    conf = min(max(float(r.get("confidence", 0.8)), 0.0), 1.0)
                except (TypeError, ValueError):
                    conf = 0.8
                out[p.post_id] = Labels(fmt, hook, offer, round(conf, 2), "ai")
        return out


def label_posts(posts: list, ai: Optional[ClaudeLabeler] = None) -> dict:
    """Fill in missing labels on posts, in place. Returns a short summary."""
    need = [p for p in posts if "unknown" in (p.format, p.hook, p.offer)]
    if not need:
        return {"labelled": 0, "source": None, "low_confidence": []}
    if ai is not None and ai.available:
        labels, source = ai.label_many(need), "ai"
    else:
        rules = RuleLabeler()
        labels, source = {p.post_id: rules.label(p.caption, p.media_type, p.platform) for p in need}, "rules"
    low = []
    for p in need:
        lab = labels[p.post_id]
        if p.format == "unknown":
            p.format = lab.format
        if p.hook == "unknown":
            p.hook = lab.hook
        if p.offer == "unknown":
            p.offer = lab.offer
        p.label_source = lab.source
        if lab.confidence < 0.5:
            low.append(p.post_id)
    return {"labelled": len(need), "source": source, "low_confidence": low}
