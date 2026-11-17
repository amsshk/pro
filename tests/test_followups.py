"""Follow-ups: recover checkouts, win back, ask for reviews; recovered sales counted honestly."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from paysignal import followups
from paysignal.server import create_app

KEY = "fu-key-0123456789abcdef"
ADMIN = "fu-admin-0123456789abcdef"
SECRET = "hooksecret"
CLIENTS = {"shop": {"api_key": KEY, "name": "Glow", "currency": "AED", "shopify_webhook_secret": SECRET},
           "pro": {"api_key": ADMIN, "name": "Glow Admin", "currency": "AED", "shopify_webhook_secret": SECRET}}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def hook(c, cid, topic, body):
    raw = json.dumps(body).encode()
    sig = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
    return c.post(f"/webhooks/shopify/{cid}", data=raw, headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": topic,
                                                                 "Content-Type": "application/json"})


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_SECRET_KEY", "k" * 40)
    return create_app(CLIENTS, str(tmp_path / "f.db"), start_worker=False).test_client()


def test_checkout_list_send_and_recovered(c):
    now = datetime.now(timezone.utc)
    H = {"X-API-Key": ADMIN}
    co = {"token": "tok1", "created_at": iso(now - timedelta(hours=3)), "updated_at": iso(now - timedelta(hours=3)),
          "email": "sara@x.com", "phone": "050 123 4567", "total_price": "450.00", "currency": "AED",
          "abandoned_checkout_url": "https://glow.ae/checkouts/tok1/recover", "customer": {"first_name": "Sara"},
          "line_items": [{"title": "Discovery Box", "quantity": 1}]}
    assert hook(c, "pro", "checkouts/create", co).json == {"checkout": True}
    hook(c, "pro", "checkouts/create", {**co, "token": "fresh", "created_at": iso(now - timedelta(minutes=10))})   # too new
    f = c.get("/followups/pro", headers=H).json
    items = f["lists"]["checkout"]
    assert f["premium"] and len(items) == 1 and items[0]["name"] == "Sara" and items[0]["amount"] == 450
    assert items[0]["wa"].startswith("https://wa.me/971501234567?text=") and "Discovery%20Box" in items[0]["wa"]
    assert "recover" in items[0]["text"]
    assert c.post("/followups/pro/mark", json={"kind": "checkout", "ref": "tok1", "action": "sent"}, headers=H).json["ok"]
    # she pays an hour later
    c.post("/upload/csv/pro", data=f"id,date,amount,currency,email\no1,{(now + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")},450,AED,sara@x.com\n".encode(), headers=H)
    f = c.get("/followups/pro", headers=H).json
    assert f["lists"]["checkout"] == [] and f["stats"]["checkout"]["recovered"] == 1 and f["stats"]["checkout"]["money"] == 450


def test_paid_checkout_without_message_is_not_counted_as_recovered(c):
    now = datetime.now(timezone.utc)
    H = {"X-API-Key": ADMIN}
    hook(c, "pro", "checkouts/create", {"token": "t2", "created_at": iso(now - timedelta(hours=5)), "email": "a@x.com", "phone": "0501111111"})
    c.post("/upload/csv/pro", data=f"id,date,amount,currency,email\no2,{(now + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")},100,AED,a@x.com\n".encode(), headers=H)
    st = c.get("/followups/pro", headers=H).json["stats"]["checkout"]
    assert st["recovered"] == 0 and st["came_back_alone"] == 1


def test_winback_and_review_need_links_and_premium(c):
    now = datetime.now(timezone.utc)
    H = {"X-API-Key": ADMIN}
    old, recent = (now - timedelta(days=70)).date().isoformat(), (now - timedelta(days=8)).date().isoformat()
    c.post("/upload/csv/pro", data=f"id,date,amount,currency,phone\nw1,{old},200,AED,0502222222\nr1,{recent},300,AED,0503333333\n".encode(), headers=H)
    f = c.get("/followups/pro", headers=H).json
    assert f["lists"]["winback"] == [] and f["lists"]["review"] == []          # no links yet
    c.post("/followups/pro/settings", json={"shop_link": "https://glow.ae", "review_link": "https://g.page/r/glow", "winback_code": "BACK10"}, headers=H)
    f = c.get("/followups/pro", headers=H).json
    assert len(f["lists"]["winback"]) == 1 and "BACK10" in f["lists"]["winback"][0]["text"] and "971502222222" in f["lists"]["winback"][0]["wa"]
    assert len(f["lists"]["review"]) == 1 and "g.page" in f["lists"]["review"][0]["text"]
    assert c.post("/followups/pro/settings", json={"review_link": "glow.ae"}, headers=H).status_code == 400
    # a Pro (non-Premium) shop sees counts only, and can't send
    c.post("/upload/csv/shop", data=f"id,date,amount,currency,phone\nw1,{old},200,AED,0502222222\n".encode(), headers={"X-API-Key": KEY})
    g = c.get("/followups/shop", headers={"X-API-Key": KEY}).json
    assert not g["premium"] and g["lists"]["winback"] == []
    assert c.post("/followups/shop/mark", json={"kind": "winback", "ref": "w1", "action": "sent"}, headers={"X-API-Key": KEY}).status_code == 402


def test_fill_and_links():
    assert followups.fill("Hi {name}, {code_line}go", name="", code="") == "Hi, go"
    assert followups.wa_link("0501234567", "a b") == "https://wa.me/971501234567?text=a%20b"
    assert followups.wa_link("123", "x") is None
