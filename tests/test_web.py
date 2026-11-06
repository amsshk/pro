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
