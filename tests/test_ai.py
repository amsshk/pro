import json

import pytest

import demo_organic as sim
from paysignal import Engine
from paysignal.ai import MetaAI
from paysignal.assistant import ask, build_campaign, business_context, validate_campaign
from paysignal.labeler import MetaLabeler, label_posts, make_labeler
from paysignal.organic import OrganicEngine


class Resp:
    def __init__(self, status, payload=None):
        self.status_code, self.payload = status, payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def ok(text, tin=1000, tout=500):
    return Resp(200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                      "usage": {"prompt_tokens": tin, "completion_tokens": tout}})


class Session:
    """Fake HTTP: reply_fn(model, body) -> Resp. Records every call."""
    def __init__(self, reply_fn):
        self.calls, self.reply_fn = [], reply_fn

    def post(self, url, timeout, json, headers):
        self.calls.append({"url": url, "body": json, "headers": headers})
        return self.reply_fn(json["model"], json)


def test_routes_best_model_for_campaign_and_chat_cheaper_for_labelling():
    ai = MetaAI(api_key="k")
    assert ai.models_for("campaign_builder")[0][0] == "muse-wonder-1.0"
    assert ai.models_for("chat")[0][0] == "muse-wonder-1.0"
    assert ai.models_for("labelling")[0][0] == "muse-spark-1.3"
    assert ai.models_for("campaign_builder")[1] == "high"


def test_request_shape_and_cost():
    s = Session(lambda m, b: ok("hi", tin=1_000_000, tout=1_000_000))
    ai = MetaAI(api_key="secret", session=s)
    r = ai.chat("chat", [{"role": "user", "content": "x"}])
    call = s.calls[0]
    assert call["url"] == "https://api.meta.ai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["body"]["model"] == "muse-wonder-1.0" and call["body"]["reasoning_effort"] == "medium"
    for unsupported in ("temperature", "stop", "max_tokens", "frequency_penalty", "presence_penalty"):
        assert unsupported not in call["body"]
    assert r.text == "hi" and r.cost_usd == pytest.approx(24.0)    # $4 in + $20 out per 1M


def test_falls_back_when_best_model_fails():
    s = Session(lambda m, b: Resp(503) if m == "muse-wonder-1.0" else ok("fallback answer"))
    r = MetaAI(api_key="k", session=s).chat("campaign_builder", [{"role": "user", "content": "x"}])
    assert r.model == "muse-spark-1.3" and r.text == "fallback answer"
    assert [c["body"]["model"] for c in s.calls] == ["muse-wonder-1.0", "muse-spark-1.3"]


def test_bad_key_stops_immediately():
    s = Session(lambda m, b: Resp(401))
    with pytest.raises(PermissionError):
        MetaAI(api_key="bad", session=s).chat("chat", [{"role": "user", "content": "x"}])
    assert len(s.calls) == 1


def test_contributor_tier_only_for_labelling_and_only_when_allowed():
    assert "contributor" not in MetaAI(api_key="k").models_for("labelling")[0][0]
    ai = MetaAI(api_key="k", allow_contributor=True,
                routes={"chat": ["muse-spark-1.3-contributor", "muse-wonder-1.0"]})
    assert ai.models_for("labelling")[0][0] == "muse-spark-1.3-contributor"
    assert all("contributor" not in m for m in ai.models_for("chat")[0])


def test_override_routes_from_env(monkeypatch):
    monkeypatch.setenv("PAYSIGNAL_MODELS", json.dumps({"chat": ["muse-spark-1.3"]}))
    assert MetaAI(api_key="k").models_for("chat")[0] == ["muse-spark-1.3"]


def test_meta_labeller_uses_labelling_route():
    from paysignal.organic import Post
    from datetime import datetime, timezone
    p = Post("a", "instagram", datetime(2026, 11, 1, tzinfo=timezone.utc), caption="Week 1 vs week 8", media_type="REELS")
    reply = json.dumps([{"post_id": "a", "format": "reel", "hook": "before_after", "offer": "none", "confidence": 0.9}])
    s = Session(lambda m, b: ok(reply))
    summary = label_posts([p], MetaLabeler(api_key="k", session=s))
    assert summary["source"] == "meta" and p.hook == "before_after"
    body = s.calls[0]["body"]
    assert body["model"] == "muse-spark-1.3" and body["messages"][0]["role"] == "developer"


def test_auto_provider_prefers_meta(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "c")
    assert make_labeler().name == "meta"
    monkeypatch.delenv("MODEL_API_KEY")
    assert make_labeler().name == "claude"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert make_labeler() is None


@pytest.fixture(scope="module")
def context():
    posts, pays = sim.build(11)
    e = Engine(sim.CLIENT)
    e.ingest(pays)
    e.train(sim.NOW)
    return business_context(e, OrganicEngine(e, posts, now=sim.NOW))


def test_ai_context_has_no_personal_data(context):
    text = json.dumps(context, default=str)
    assert "@" not in text and "example.com" not in text
    assert context["posts"] and context["patterns"]["hook"]


def test_ask_sends_context_and_question(context):
    s = Session(lambda m, b: ok("Testimonial stories make the most per view."))
    r = ask(MetaAI(api_key="k", session=s), "ما هو أفضل منشور؟", context)
    msgs = s.calls[0]["body"]["messages"]
    assert msgs[-1]["content"] == "ما هو أفضل منشور؟" and "value_per_1k_views" in msgs[1]["content"]
    assert r.model == "muse-wonder-1.0"


def good_plan(budget=300):
    return {"name": "Testimonial winners", "objective": "OUTCOME_SALES", "optimization": "value",
            "daily_budget": budget, "currency": "AED", "rationale": "...",
            "ad_sets": [{"name": "Broad", "audience": "UAE 22-50 broad", "budget_share": 1.0, "placements": "advantage",
                         "ads": [{"source_post_id": "post10", "format": "story", "hook": "testimonial",
                                  "offer": "none", "primary_text": "...", "headline": "...",
                                  "call_to_action": "SHOP_NOW", "why": "..."}]}],
            "tests": [], "kill_rules": [], "what_to_watch": []}


def test_build_campaign_uses_best_model_and_validates(context):
    s = Session(lambda m, b: ok("Here is the plan:\n" + json.dumps(good_plan())))
    out = build_campaign(MetaAI(api_key="k", session=s), context, daily_budget=300)
    assert out["problems"] == [] and out["model"] == "muse-wonder-1.0"
    assert s.calls[0]["body"]["reasoning_effort"] == "high"
    assert "boost_candidates" in s.calls[0]["body"]["messages"][1]["content"]


def test_campaign_validation_catches_overspend_and_bad_split():
    plan = good_plan(500)
    plan["ad_sets"][0]["budget_share"] = 0.6
    problems = validate_campaign(plan, daily_budget=300)
    assert any("above the limit" in p for p in problems) and any("add up" in p for p in problems)


def test_server_ai_endpoints(tmp_path, context):
    from paysignal.server import create_app
    app = create_app({"c": {"api_key": "K"}}, str(tmp_path / "db"), start_worker=False)
    client = app.test_client()
    assert client.post("/ask/c", json={"question": "hi"}, headers={"X-API-Key": "K"}).status_code == 503
    app.extensions["paysignal"].ai = MetaAI(api_key="k", session=Session(
        lambda m, b: ok(json.dumps(good_plan()) if b["reasoning_effort"] == "high" else "answer")))
    r = client.post("/ask/c", json={"question": "how are we doing?"}, headers={"X-API-Key": "K"})
    assert r.status_code == 200 and r.json["answer"] == "answer"
    r = client.post("/campaign/c", json={"daily_budget": 300}, headers={"X-API-Key": "K"})
    assert r.status_code == 200 and r.json["status"].startswith("draft")
    assert client.post("/campaign/c", json={}, headers={"X-API-Key": "K"}).status_code == 400
