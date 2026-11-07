import time

from paysignal import purchase_funnel as pf
from test_billing import ACCT, app, signed  # noqa: F401  (fixture)
from test_proads_revenue import invoice


def st(clicks, visits, signups, active, checkout, plan, paid, partial=()):
    keys = ["clicks", "visits", "signups", "active", "checkout", "plan", "paid"]
    vals = [clicks, visits, signups, active, checkout, plan, paid]
    return [{"key": k, "label": k.title(), "count": v, "source": "test", "partial": k in partial} for k, v in zip(keys, vals)]


def test_example_funnel_finds_the_biggest_confident_opportunity():
    # 1,000 clicks, 650 visits, 180 sign-ups, 70 checkout starts, 25 payments (illustrative)
    out = pf.analyse(st(1000, 650, 180, 120, 70, 40, 25), "saas", value_each=299.0, currency="AED")
    assert out["status"] == "opportunity"
    steps = {s["to"]: s for s in out["steps"]}
    assert steps["visits"]["rate"] == 0.65 and steps["visits"]["weak"]          # 90% range [0.62,0.67] < 0.75
    assert not steps["signups"]["weak"]                                          # 27.7% sign-up rate is strong
    best = out["opportunity"]
    assert best["extra_results"] == max(s["extra_results"] for s in out["steps"] if s["weak"])
    assert out["next_experiment"]["title"] and out["next_experiment"]["people_per_version"] > 0
    assert out["next_experiment"]["days_needed"] > 0 and "AED" in out["summary"]


def test_small_numbers_are_not_judged():
    out = pf.analyse(st(25, 18, 3, 1, 0, 0, 0), "saas")
    assert out["status"] == "too_early" and out["opportunity"] is None
    assert not any(s["weak"] for s in out["steps"])


def test_reference_proven_by_another_campaign_beats_rule_of_thumb():
    peer = [{**s, "campaign": "Winner"} for s in st(2000, 1500, 400, 300, 200, 150, 120)]
    out = pf.analyse(st(1000, 800, 60, 40, 20, 15, 10), "saas", peers=[peer])
    s = next(x for x in out["steps"] if x["to"] == "signups")
    assert s["reference_source"] == "proven here by Winner" and 0.24 < s["reference"] < 400 / 1500 and s["weak"]
    assert out["opportunity"]["to"] == "signups"


def test_immature_payments_are_not_called_weak():
    stages = st(1000, 800, 300, 200, 150, 100, 10)
    stages[-1]["mature"] = (12, 10)                          # only 12 plans are past their trial; 10 of them paid
    out = pf.analyse(stages, "saas")
    last = out["steps"][-1]
    assert last["top"] == 12 and last["reached"] == 10 and not last["judged"] and not last["weak"]


def test_unknown_stage_is_skipped_not_zero():
    stages = st(None, 500, 60, 30, 20, 10, 8)
    out = pf.analyse(stages, "saas")
    assert out["steps"][0]["from"] == "visits"


def test_proads_funnel_end_to_end(app):
    state = app.extensions["paysignal"]
    state.billing_sync = True
    state.growth_sent = []
    state.funnel.set_started_at(int(time.time()) - 5 * 86400)
    c = app.test_client()
    tags = {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "launch-thb"}
    for _ in range(3):
        assert c.post("/t/visit", json=tags).status_code == 204
    c.post("/t/visit", json={"utm_source": "", "utm_medium": "", "utm_campaign": ""})       # organic: not in the Meta funnel
    tok = c.post("/auth/signup", json={**ACCT, "utm": tags}).json["token"]
    H = {"X-API-Key": tok}
    assert c.post("/billing/glow-clinic/checkout", headers=H).status_code == 200
    signed(c, {"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "client_reference_id": "glow-clinic", "customer": "cus_1", "subscription": "sub_1",
        "payment_status": "paid"}}})
    signed(c, invoice("in_1"))
    state.launches.add("pro", {"campaign_id": "c1", "name": "Launch THB", "utm_slug": "launch-thb", "status": "ACTIVE"})
    out = c.get("/funnel/pro?period=30", headers={"X-API-Key": "admin-key-0123456789abc"}).json
    counts = {s["key"]: s["count"] for s in out["stages"]}
    assert counts == {"clicks": None, "visits": 3, "signups": 1, "active": 0, "checkout": 1, "plan": 1, "paid": 1}
    assert out["campaigns"] == [{"slug": "launch-thb", "name": "Launch THB"}] and out["value_each"] == 299.0
    assert out["status"] == "too_early"
    last = out["steps"][-1]                                  # plan started today, 14-day trial: too soon to judge payment
    assert last["to"] == "paid" and last["top"] == 0 and not last["judged"] and "trial" in last["mature_note"]
    one = c.get("/funnel/pro?campaign=launch-thb", headers={"X-API-Key": "admin-key-0123456789abc"}).json
    assert one["selected"] == "launch-thb" and {s["key"]: s["count"] for s in one["stages"]}["paid"] == 1
    # a client gets their own shop funnel, never ProAds' numbers
    mine = c.get("/funnel/glow-clinic", headers=H).json
    assert mine["kind"] == "shop" and mine["status"] == "no_data"


def test_dead_facebook_session_asks_for_reconnect(app):
    from paysignal import meta_ads

    class R:
        status_code = 400
        def json(self):
            return {"error": {"code": 190, "error_subcode": 460, "message": "Error validating access token: The session has been invalidated"}}

    class H:
        def get(self, *a, **k):
            return R()
    hit = []
    pub = meta_ads.Publisher(H(), {"ad_account": "act_1", "page_id": "p", "token": "t", "pixel_id": "x", "version": "v25.0",
                                   "on_token_invalid": lambda: hit.append(1)})
    try:
        pub._get("123", "id", "reading results")
        assert False
    except meta_ads.PublishError as e:
        assert e.token_invalid and "Reconnect Facebook" in str(e)
    assert hit == [1]
