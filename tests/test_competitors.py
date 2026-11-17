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


def test_win_their_customers(tmp_path):
    from test_ai import Session, ok
    app = app_for(tmp_path)
    st = app.extensions["paysignal"]
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Bynawi", "email": "o@bynawi.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    c.post("/upload/csv/bynawi", data="id,date,amount,currency,email\n1,2026-11-01,160,AED,a@x.ae\n2,2026-11-02,340,AED,b@x.ae\n", headers=h)
    plan = {"edges": [{"edge": "Try before a full bottle", "proof": "Discovery Box is the top seller"}],
            "search_keywords": [{"keyword": "rival alternative", "platform": "Both", "why": "comparing"}],
            "tiktok_targeting": {"hashtags": ["#perfumetok"], "interests": ["Beauty"], "creator_categories": ["Beauty"]},
            "meta_interests": ["Perfume"], "scripts": [{"title": "t", "hook": "h", "script": "s", "on_screen_text": "o", "cta": "c"}] * 4,
            "creator_brief": {"who": "w", "send": "s", "ask": "a", "deliverables": ["1 video"], "rules": ["#ad"]}, "watch_out": ["no names"]}
    s = Session(lambda m, b: ok(json.dumps(plan)))
    st.ai = MetaAI(api_key="k", session=s)
    assert c.post("/competitors/bynawi/conquest", json={"competitors": "Rival"}, headers=h).status_code == 402   # Premium
    assert c.post("/competitors/pro/conquest", json={"competitors": ""}, headers=ADMIN).status_code == 400
    # the admin, looking at Bynawi view-only, can build his plan (no plan needed, nothing is changed)
    v = {"X-API-Key": c.post("/admin/pro/view-as/bynawi", headers=ADMIN).json["token"]}
    r = c.post("/competitors/bynawi/conquest", json={"competitors": "Rival, Other Brand", "country": "AE"}, headers=v)
    assert r.status_code == 200, r.json
    assert len(r.json["plan"]["scripts"]) == 3 and r.json["libraries"][1]["name"] == "Other Brand"
    assert "q=Other%20Brand" in r.json["libraries"][1]["meta"] and r.json["facts"]["orders_90_days"] == 2
    sent = s.calls[-1]["body"]["messages"]
    assert "never contain the competitor's name" in sent[0]["content"] and "Rival" in sent[1]["content"]
    assert c.post("/competitors/bynawi/counter", json={"ads": [{"text": "x"}], "daily_budget": 40}, headers=v).status_code == 403


def test_suggested_competitors_have_lookup_links_and_skip_self():
    from paysignal import competitors as C

    class AI:
        def chat(self, task, messages, max_completion_tokens=0):
            from types import SimpleNamespace
            return SimpleNamespace(text='{"competitors": [{"name": "Arabian Oud", "why": "Oud perfumes, same buyers", "type": "regional"},'
                                        ' {"name": "Bynawi", "why": "self"}, {"name": "arabian oud", "why": "dupe"}, {"name": "", "why": "x"}]}')
    out = C.suggest(AI(), {"name": "Bynawi"}, "AE")
    assert [x["name"] for x in out] == ["Arabian Oud"] and out[0]["type"] == "regional"
    l = out[0]["links"]
    assert "facebook.com/ads/library" in l["meta"] and "country=AE" in l["meta"] and "q=Arabian%20Oud" in l["meta"]
    assert "creativecenter" in l["tiktok"] and l["google"].startswith("https://www.google.com/search?q=") and "instagram.com" in l["instagram"]


def test_suggest_endpoint_premium_and_cached(tmp_path):
    from types import SimpleNamespace
    from paysignal.server import create_app
    calls = []

    class AI:
        available = True

        def chat(self, task, messages, max_completion_tokens=0):
            calls.append(1)
            return SimpleNamespace(text='{"competitors": [{"name": "Rival Scents", "why": "same price"}]}')
    app = create_app({"pro": {"api_key": "sug-admin-0123456789ab", "name": "P"}, "shop": {"api_key": "sug-shop-0123456789abc", "name": "S"}},
                     str(tmp_path / "s.db"), start_worker=False)
    st = next(c.cell_contents for c in app.view_functions["competitors_suggest"].__closure__ if type(c.cell_contents).__name__ == "App")
    st.ai = AI()
    c = app.test_client()
    assert c.get("/competitors/shop/suggest", headers={"X-API-Key": "sug-shop-0123456789abc"}).status_code == 402
    H = {"X-API-Key": "sug-admin-0123456789ab"}
    r = c.get("/competitors/pro/suggest?country=SA", headers=H).json
    assert r["country"] == "SA" and r["competitors"][0]["name"] == "Rival Scents" and "country=SA" in r["competitors"][0]["links"]["meta"]
    c.get("/competitors/pro/suggest?country=SA", headers=H)
    assert len(calls) == 1                                   # kept for 7 days
    c.get("/competitors/pro/suggest?country=SA&fresh=1", headers=H)
    assert len(calls) == 2
    assert c.get("/competitors/pro/suggest?country=1A", headers=H).status_code == 400
