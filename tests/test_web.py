from datetime import datetime, timezone

import pytest

from paysignal.server import create_app

KEY = "web-test-key-0123456789"
CLIENTS = {"shop": {"api_key": KEY, "name": "Glow Clinic", "currency": "AED", "website": "glowclinic.ae",
                    "shopify_webhook_secret": "s"}}
H = {"X-API-Key": KEY}
POSTS = """Post ID,Description,Publish time,Post type,Views
p1,"Before & after ✨",10/01/2026 20:00,IG reel,5000
"""
ORDERS = "id,date,amount,currency,promo_code\n1,2026-10-02,300,AED,\n"


@pytest.fixture
def make(tmp_path):
    db = str(tmp_path / "web.db")
    return lambda: create_app(CLIENTS, db, start_worker=False).test_client()


def test_web_app_is_served(make):
    r = make().get("/app")
    assert r.status_code == 200 and b"ProAds" in r.data and b"innerHTML" not in r.data
    assert r.headers["X-Frame-Options"] == "DENY"


def test_status_needs_key_and_reports_connections(make):
    c = make()
    assert c.get("/status/shop").status_code == 401
    s = c.get("/status/shop", headers=H).json
    assert s["name"] == "Glow Clinic" and s["connections"]["shopify"]["configured"]
    assert not s["connections"]["stripe"]["configured"] and s["webhooks"]["stripe"] == "/webhooks/stripe/shop"


def test_tracking_code_is_saved_and_attributes_sales(make):
    c = make()
    r = c.post("/tracking/shop", json={"post_name": "Review story Sara", "platform": "instagram"}, headers=H).json
    assert r["link"].startswith("https://glowclinic.ae?") and "utm_content=review-story-sara" in r["link"]
    code = r["code"]
    c2 = make()                                           # server restarted
    assert c2.get("/tracking/shop", headers=H).json["codes"][0]["code"] == code
    c2.post("/import/shop", data=ORDERS.replace("AED,\n", f"AED,{code.lower()}\n"), headers=H)
    content = c2.get("/report/shop", headers=H).json["content"]
    sara = [row for row in content if row["content"] == "review-story-sara"]
    assert sara and sara[0]["orders"] == 1 and sara[0]["platform"] == "instagram"


def test_tracking_validation(make):
    c = make()
    assert c.post("/tracking/shop", json={}, headers=H).status_code == 400
    assert c.post("/tracking/shop", json={"post_name": "x", "platform": "myspace"}, headers=H).status_code == 400


def test_posts_survive_restart(make):
    c = make()
    assert c.post("/import/shop", data=POSTS, headers=H).json["posts"] == 1
    s = make().get("/status/shop", headers=H).json
    assert s["posts"] == 1


def test_status_shows_last_payment(make):
    c = make()
    c.post("/import/shop", data=ORDERS, headers=H)
    up = c.get("/status/shop", headers=H).json["connections"]["uploads"]
    assert up["count"] == 1 and up["last"].startswith("2026-10-02")


def test_landing_has_languages_and_country_comes_from_cloudflare(make):
    c = make()
    page = c.get("/").data.decode()
    assert 'id="i18n"' in page and '"ar"' in page and "innerHTML" not in page
    assert c.get("/public-config", headers={"CF-IPCountry": "ae"}).json["country"] == "AE"
    assert c.get("/public-config", headers={"CF-IPCountry": "XX"}).json["country"] == ""
    assert c.get("/public-config").json["country"] == ""


def test_google_site_verification_tag(make, monkeypatch):
    monkeypatch.setenv("GOOGLE_SITE_VERIFICATION", 'google-site-verification=abc_DEF-123"><script>')
    page = make().get("/").data.decode()
    assert '<meta name="google-site-verification" content="abc_DEF-123script">' in page


def test_trust_pages_and_company_details(make, monkeypatch):
    monkeypatch.setenv("PROADS_COMPANY_LEGAL_NAME", "EzDevSoft Technology and Solutions")
    monkeypatch.setenv("PROADS_CONTACT_EMAIL", "support@ezdevsoft.net")
    c = make()
    for path, words in (("/about", "About ProAds"), ("/terms", "Terms of service"), ("/security", "Security at ProAds")):
        r = c.get(path)
        assert r.status_code == 200 and words in r.data.decode()
    co = c.get("/public-config").json["company"]
    assert co["legal_name"] == "EzDevSoft Technology and Solutions" and co["email"] == "support@ezdevsoft.net"
    assert "mailto:support@ezdevsoft.net" in c.get("/.well-known/security.txt").data.decode()
    assert 'href="/terms"' in c.get("/").data.decode()


def test_profit_only_with_a_real_product_cost(tmp_path):
    key = "pc-key-0123456789abcdef"
    app = create_app({"shop": {"api_key": key, "name": "Shop", "currency": "AED"}}, str(tmp_path / "p.db"), start_worker=False)
    c = app.test_client()
    H = {"X-API-Key": key}
    from datetime import date
    c.post("/upload/csv/shop", data=f"id,date,amount,currency\n1,{date.today().isoformat()},1000,AED\n".encode(), headers=H)
    d = c.get("/dashboard/shop", headers=H).json
    assert "profit" not in {x["key"] for x in d["metrics"]} and "profit" not in d["series"] and d["profit_basis"]["known"] is False
    a = c.get("/audit/shop", headers=H).json
    assert a["audit"]["profit_known"] is False and a["audit"]["kept"] == 1000 and "Kept after refunds 1,000" in a["text"]
    assert c.post("/learning/shop/settings", json={"cost_share": "30"}, headers=H).status_code == 200
    d = c.get("/dashboard/shop", headers=H).json
    m = {x["key"]: x for x in d["metrics"]}
    assert m["profit"]["value"] == 700 and m["profit"]["label"] == "Profit after product cost" and d["profit_basis"]["cost_share"] == 0.3
    a = c.get("/audit/shop", headers=H).json["audit"]
    assert a["profit_known"] is True and a["real_profit"] == 700
