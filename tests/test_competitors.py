import json
import time

from paysignal import competitors
from paysignal.ai import MetaAI
from paysignal.server import create_app

ADMIN = {"X-API-Key": "admin-key-0123456789abc"}
OTHER = {"X-API-Key": "other-key-0123456789abc"}


class R:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class Library:
    def __init__(self, body=None, status=200):
        self.calls, self.body, self.status = [], body, status

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        return R(self.status, self.body if self.body is not None else {"data": [
            {"id": "1", "page_name": "Rival", "page_id": "99", "ad_creative_bodies": ["Old winner"],
             "ad_creative_link_titles": ["Shop now"], "ad_delivery_start_time": "2026-08-01",
             "publisher_platforms": ["facebook", "instagram"]},
            {"id": "2", "page_name": "Rival", "page_id": "99", "ad_creative_bodies": ["New test"],
             "ad_delivery_start_time": time.strftime("%Y-%m-%d"), "publisher_platforms": ["instagram"]}]})


def app_for(tmp_path):
    return create_app({"pro": {"api_key": "admin-key-0123456789abc"}, "other": {"api_key": "other-key-0123456789abc"}},
                      str(tmp_path / "x.db"), start_worker=False)


def test_scan_finds_the_ads_they_keep_paying_for():
    lib = Library()
    ads = competitors.scan(lib, "tok", "v25.0", query="rival", country="ae")
    p = lib.calls[0][1]
    assert lib.calls[0][0].endswith("/ads_archive") and p["search_terms"] == "rival"
    assert json.loads(p["ad_reached_countries"]) == ["AE"] and p["ad_active_status"] == "ACTIVE"
    s = competitors.summarize(ads)
    assert s["ads"] == 2 and s["advertisers"] == [{"page": "Rival", "ads": 2}]
    assert s["longest_running"][0]["id"] == "1" and s["longest_running"][0]["days_running"] > 60
    assert s["platforms"][0] == {"name": "Instagram", "ads": 2}


def test_identity_error_is_explained():
    lib = Library({"error": {"code": 10, "message": "Application does not have permission"}}, status=400)
    try:
        competitors.scan(lib, "tok", "v25.0", query="rival")
        assert False
    except competitors.ScanError as e:
        assert "Ad Library to this account" in str(e)


def test_pasted_ads_and_browser_input_are_cleaned():
    ads = competitors.from_pasted("First ad text\n\nSecond ad\nline two\n\n")
    assert [a["text"] for a in ads] == ["First ad text", "Second ad\nline two"]
    a = competitors.normalize_client({"text": "x" * 900, "days_running": "lots", "platforms": "fb", "evil": 1})
    assert len(a["text"]) == 600 and a["days_running"] is None and a["platforms"] == [] and "evil" not in a


def test_premium_gate_and_counter_campaign(tmp_path):
    from test_ai import Session, ok
    from test_meta_ads import plan
    app = app_for(tmp_path)
    st = app.extensions["paysignal"]
    st.ads_http = Library()
    c = app.test_client()
    r = c.post("/competitors/other/scan", json={"query": "rival"}, headers=OTHER)
    assert r.status_code == 402 and r.json["needs_tier"] == "premium"
    with st.lock:
        st.billing.upsert("other", status="active", tier="premium")
    r = c.post("/competitors/other/scan", json={"query": "rival"}, headers=OTHER)
    assert r.status_code == 409                                  # no Facebook connected yet
    r = c.post("/competitors/other/scan", json={"pasted": "Their ad one\n\nTheir ad two"}, headers=OTHER)
    assert r.status_code == 200 and r.json["summary"]["ads"] == 2

    p = {k: v for k, v in plan().items() if k != "conversion"}
    reply = {"insights": {"whats_working": ["long-running offer"], "weak_spots": ["no proof"]}, "plan": p}
    s = Session(lambda m, b: ok(json.dumps(reply)))
    st.ai = MetaAI(api_key="k", session=s)
    r = c.post("/competitors/pro/counter", json={"ads": [{"text": "Rival words", "days_running": 90}],
                                                 "daily_budget": 40}, headers=ADMIN)
    assert r.status_code == 200 and r.json["insights"]["weak_spots"] == ["no proof"] and r.json["draft_id"]
    sent = s.calls[0]["body"]["messages"]
    assert "ORIGINAL" in sent[0]["content"] and "Rival words" in sent[1]["content"]
    assert r.json["plan"]["objective"] == "OUTCOME_LEADS"
    assert c.get("/campaign/pro/history", headers=ADMIN).json["history"][0]["draft_id"] == r.json["draft_id"]


def test_billing_switch_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("PROADS_STRIPE_PRICE_ID", "price_pro")
    monkeypatch.setenv("PROADS_STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("PROADS_STRIPE_PREMIUM_PRICE_ID", "price_prem")
    app = app_for(tmp_path)
    st = app.extensions["paysignal"]
    posts = []

    class Stripe:
        def get(self, url, params=None, headers=None, timeout=None, auth=None):
            if "/subscriptions/" in url:
                return R(200, {"items": {"data": [{"id": "si_1"}]}, "metadata": {"client_id": "other"}})
            pid = url.rsplit("/", 1)[-1]
            return R(200, {"unit_amount": 79900 if pid == "price_prem" else 29900, "currency": "aed",
                           "product": {"name": pid}, "recurring": {"interval": "month"}})

        def post(self, url, data=None, headers=None, timeout=None, auth=None):
            posts.append((url, data))
            return R(200, {"url": "https://checkout.stripe.com/x", "id": "x"})

    st.stripe_http = Stripe()
    c = app.test_client()
    b = c.get("/billing/other", headers=OTHER).json
    assert b["tier"] == "free" and b["premium_price"]["amount"] == 799 and b["price"]["amount"] == 299
    assert c.post("/billing/other/checkout", json={"tier": "premium"}, headers=OTHER).status_code == 200
    assert posts[-1][1]["line_items[0][price]"] == "price_prem"
    assert c.post("/billing/other/switch", json={"tier": "premium"}, headers=OTHER).status_code == 409
    with st.lock:
        st.billing.upsert("other", status="active", subscription_id="sub_1", tier="pro")
    r = c.post("/billing/other/switch", json={"tier": "premium"}, headers=OTHER)
    assert r.status_code == 200 and posts[-1][1]["items[0][price]"] == "price_prem"
    assert c.get("/billing/other", headers=OTHER).json["tier"] == "premium"


def test_scan_works_without_a_test_client(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda url, params, timeout: R(200, {"data": []}))
    assert competitors.scan(None, "tok", "v25.0", query="rival") == []
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)
    try:
        competitors.scan(None, "tok", "v25.0", query="rival")
        assert False
    except competitors.ScanError as e:
        assert "couldn't reach" in str(e)
