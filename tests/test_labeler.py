import json
from datetime import datetime, timezone

from label_cases import CASES
from label_cases_hard import HARD
from paysignal.labeler import ClaudeLabeler, RuleLabeler, label_posts
from paysignal.organic import Post

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def post(pid, caption, media="REELS", hook="unknown", offer="unknown", fmt="unknown"):
    return Post(pid, "instagram", NOW, fmt, hook, offer, caption=caption, media_type=media)


def test_rules_on_clear_captions():
    r = RuleLabeler()
    res = [r.label(c, m, p) for c, m, p, *_ in CASES]
    hooks = sum(l.hook == c[3] for l, c in zip(res, CASES))
    offers = sum(l.offer == c[4] for l, c in zip(res, CASES))
    formats = sum(l.format == c[5] for l, c in zip(res, CASES))
    assert hooks / len(CASES) >= 0.9 and offers / len(CASES) >= 0.9 and formats == len(CASES)


def test_rules_are_never_confidently_wrong_on_hard_captions():
    """Rules may not know, but when they say they're sure, they must be right."""
    r = RuleLabeler()
    for caption, media, platform, hook, _ in HARD:
        lab = r.label(caption, media, platform)
        if lab.confidence >= 0.5:
            assert lab.hook == hook, caption


def test_arabic_variants_normalised():
    r = RuleLabeler()
    assert r.label("خُصُم ٢٠٪ اليوم فقط").offer == "discount"
    assert r.label("طريقة الاستخدام").hook == "tutorial"


def test_manual_labels_are_kept():
    p = post("p1", "Before and after 30% off", hook="testimonial", offer="none", fmt="story")
    assert label_posts([p])["labelled"] == 0
    assert (p.hook, p.offer, p.format) == ("testimonial", "none", "story")


def test_only_missing_fields_filled():
    p = post("p1", "How to use it: 3 steps", hook="unknown", offer="bundle", fmt="unknown")
    label_posts([p])
    assert (p.hook, p.offer, p.format, p.label_source) == ("tutorial", "bundle", "reel", "rules")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, reply_fn):
        self.calls, self.reply_fn = [], reply_fn

    def post(self, url, timeout, headers, json):
        self.calls.append({"url": url, "headers": headers, "body": json})
        return self.reply_fn(json)


def claude_reply(rows):
    return FakeResponse({"content": [{"type": "text", "text": "Here you go:\n" + json.dumps(rows)}]})


def test_ai_labels_used_and_request_is_well_formed():
    posts = [post("a", "Week 1 vs week 8. No filter."), post("b", "Wait for it 😭")]
    session = FakeSession(lambda body: claude_reply([
        {"post_id": "a", "format": "reel", "hook": "before_after", "offer": "none", "confidence": 0.9},
        {"post_id": "b", "format": "reel", "hook": "trend_meme", "offer": "none", "confidence": 0.7}]))
    ai = ClaudeLabeler(api_key="sk-test", session=session)
    summary = label_posts(posts, ai)
    assert summary["source"] == "ai" and summary["low_confidence"] == []
    assert [p.hook for p in posts] == ["before_after", "trend_meme"]
    call = session.calls[0]
    assert call["headers"]["anthropic-version"] == "2026-10-21"
    assert call["headers"]["x-api-key"] == "sk-test"
    assert call["body"]["model"] == "claude-haiku-5-5" and "temperature" not in call["body"]
    assert "Week 1 vs week 8" in call["body"]["messages"][0]["content"]


def test_invalid_ai_values_fall_back_to_rules():
    p = post("a", "How to apply it in 3 steps")
    session = FakeSession(lambda body: claude_reply(
        [{"post_id": "a", "format": "tv_ad", "hook": "made_up", "offer": "none", "confidence": "high"}]))
    label_posts([p], ClaudeLabeler(api_key="k", session=session))
    assert (p.hook, p.format) == ("tutorial", "reel")


def test_api_error_falls_back_to_rules():
    p = post("a", "30% off this weekend")
    session = FakeSession(lambda body: FakeResponse({}, status=529))
    label_posts([p], ClaudeLabeler(api_key="k", session=session))
    assert p.offer == "discount" and p.label_source == "rules"


def test_batches_of_twenty():
    posts = [post(f"p{i}", "hello") for i in range(45)]
    session = FakeSession(lambda body: claude_reply([]))
    label_posts(posts, ClaudeLabeler(api_key="k", session=session))
    assert len(session.calls) == 3


def test_no_api_key_uses_rules():
    p = post("a", "Meet the team behind the scenes")
    assert label_posts([p], ClaudeLabeler(api_key=""))["source"] == "rules"
    assert p.hook == "behind_scenes"
