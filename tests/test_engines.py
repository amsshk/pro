import json

import pytest

from paysignal import engines
from paysignal.server import create_app
from test_meta_ads import FakeGraph, R

ADMIN = {"X-API-Key": "admin-key-0123456789abc"}


def econ(**kw):
    return engines.economics(engines.clean_inputs({"price": 300, "product_cost": 80, "other_costs": 20,
                                                    "desired_profit": 100, **kw}))


def test_max_cost_per_customer_from_the_clinic_example():
    e = econ(daily_budget=40)
    assert e["break_even_cpa"] == 200 and e["target_cpa"] == 100 and e["viable"]
    assert e["budget"]["survival_mode"] and e["budget"]["ad_sets"] == 1 and e["budget"]["days_to_first_verdict"] == 15
    e = econ(payment_fee_pct=3, refund_rate=0.1, orders_per_customer=2)
    assert e["margin_per_sale"] == 191 and e["break_even_cpa"] == 171.9 and e["lifetime_break_even_cpa"] == 343.8
    assert not econ(desired_profit=250)["viable"] and "desired profit" in econ(desired_profit=250)["warning"]
    with pytest.raises(ValueError):
        engines.clean_inputs({"price": 0})
    with pytest.raises(ValueError):
        engines.clean_inputs({"price": 10, "product_cost": -1})


def test_verdicts_need_evidence():
    v = lambda spend, sales: engines.verdict(spend, sales, 100, 200)["verdict"]
    assert v(50, 0) == "too_early" and v(350, 0) == "too_early"     # no verdict on a tiny sample
    assert v(600, 0) == "losing"                                      # 3x break-even, nothing: 95% sure
    assert v(1000, 12) == "profitable" and engines.verdict(1000, 12, 100, 200)["confidence"] >= 0.95
    assert v(500, 3) == "break_even"
    assert v(600, 2) == "likely_losing" and v(3000, 5) == "losing"
    assert engines.verdict(100, 0, -5, -1)["verdict"] == "no_room"


def f(**kw):
    base = {"spend": 400, "impressions": 20000, "clicks": 300, "link_clicks": 260, "landing_views": 200,
            "add_to_cart": 20, "checkout": 12, "purchase": 4, "signup": 0, "frequency": 1.6, "cpm": 20, "purchase_value": 1200}
    return {**base, **kw}


def test_autopsy_finds_the_weakest_step():
    assert engines.autopsy(f(impressions=500), "purchase")["status"] == "too_early"
    a = engines.autopsy(f(link_clicks=60), "purchase")
    assert a["findings"][0]["stage"] == "ad" and "opening line" in a["findings"][0]["next_experiment"]
    a = engines.autopsy(f(landing_views=150, add_to_cart=0, checkout=0, purchase=0), "purchase")
    assert a["findings"][0]["stage"] == "page"
    a = engines.autopsy(f(add_to_cart=30, checkout=4, purchase=2), "purchase")
    assert a["findings"][0]["stage"] == "cart" and "Delivery" in a["findings"][0]["likely_cause"]
    a = engines.autopsy(f(checkout=20, purchase=2), "purchase")
    assert a["findings"][0]["stage"] == "checkout"
    a = engines.autopsy(f(landing_views=80), "purchase")
    assert a["findings"][0]["stage"] == "page_load"
    a = engines.autopsy(f(purchase=10), "purchase", real_ratio=0.5)
    assert any(x["stage"] == "tracking" for x in a["findings"])
    assert engines.autopsy(f(), "purchase")["status"] == "healthy"
    steps = engines.autopsy(f(), "purchase")["steps"]
    assert [s["step"] for s in steps][-1] == "Paid" and steps[1]["from_previous"] == 0.013


class Graph(FakeGraph):
    def get(self, url, params, timeout):
        if url.endswith("/insights") and "inline_link_clicks" in params.get("fields", ""):
            return R(200, {"data": [{"spend": "700", "impressions": "30000", "clicks": "400", "inline_link_clicks": "90",
                                     "frequency": "1.4", "account_currency": "AED",
                                     "actions": [{"action_type": "landing_page_view", "value": "80"},
                                                 {"action_type": "omni_complete_registration", "value": "3"}]}]})
        return super().get(url, params, timeout)


def test_engines_endpoint(tmp_path, monkeypatch):
    for k, v in {"PROADS_AD_ACCOUNT_ID": "123", "PROADS_PAGE_ID": "555", "PROADS_ADS_TOKEN": "tok",
                 "PROADS_META_DATASET_ID": "777", "PROADS_PUBLIC_URL": "https://pro.example.net"}.items():
        monkeypatch.setenv(k, v)
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "x.db"), start_worker=False)
    st = app.extensions["paysignal"]
    st.ads_http = Graph()
    c = app.test_client()
    assert c.get("/engines/pro", headers=ADMIN).json["campaigns"] == []
    assert c.post("/engines/pro/economics", json={"price": "abc"}, headers=ADMIN).status_code == 400
    r = c.post("/engines/pro/economics", json={"price": 299, "payment_fee_pct": 3, "desired_profit": 0}, headers=ADMIN).json
    assert r["target_cpa"] == 290.03
    st.launches.add("pro", {"campaign_id": "c1", "name": "Sign-ups UAE", "ad_sets": [], "ads": [], "status": "ACTIVE"})
    out = c.get("/engines/pro", headers=ADMIN).json
    camp = out["campaigns"][0]
    assert camp["results"] == 3 and camp["autopsy"]["findings"][0]["stage"] == "ad"   # 0.3% clicked
    assert out["economics"]["break_even_cpa"] == 290.03
    with app.test_request_context():
        pass
    from test_ai import Session, ok
    from paysignal.ai import MetaAI
    s = Session(lambda m, b: ok(json.dumps({"answer": "ok"})))
    st.ai = MetaAI(api_key="k", session=s)
    c.post("/ask/pro", json={"question": "how much can I pay per customer?"}, headers=ADMIN)
    assert "target_cpa" in json.dumps(s.calls[0]["body"])
