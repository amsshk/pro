import base64
import hashlib
import hmac
import json

from datetime import datetime

from paysignal.models import Payment
from paysignal.server import create_app


def stripe(app, cid, eid, at, amount, email):
    st = app.extensions["paysignal"]
    return st.ingest(cid, [Payment(client_id=cid, source="stripe", external_id=eid, kind="purchase", amount=amount,
                                   currency="AED", occurred_at=datetime.fromisoformat(at), email=email)])

KEY = "a3f9c2e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0"


def sign(body: bytes, key=KEY):
    return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


def order(oid, amount, email, at="2026-11-16T10:00:00+04:00"):
    return {"id": oid, "created_at": at, "processed_at": at, "total_price": str(amount), "currency": "AED", "email": email,
            "customer": {"id": 9, "email": email}, "line_items": [{"sku": "N2", "title": "N°2 50ML", "price": str(amount), "quantity": 1}],
            "financial_status": "paid", "total_discounts": "0"}


def test_webhooks_connect_and_merge_with_stripe(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_SECRET_KEY", "test-secret-key-for-encryption-0123456789")
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Bynawi", "email": "o@bynawi.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    body = json.dumps(order(1001, 340, "sara@x.ae")).encode()
    hdr = {"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": sign(body)}
    assert c.post("/webhooks/shopify/bynawi", data=body, headers=hdr).status_code == 401       # no key yet
    assert c.post("/connect/bynawi/shopify-webhooks", json={"secret": "short"}, headers=h).status_code == 400
    r = c.post("/connect/bynawi/shopify-webhooks", json={"secret": KEY}, headers=h).json
    assert r["connected"] and r["url"].endswith("/webhooks/shopify/bynawi")
    assert c.get("/status/bynawi", headers=h).json["connections"]["shopify_hooks"]["connected"]
    # the same sale already came from Stripe (06:05 UTC = 10:05 Dubai): kept once
    stripe(app, "bynawi", "ch_1", "2026-11-16T06:05:00+00:00", 340, "Sara@x.ae")
    got = c.post("/webhooks/shopify/bynawi", data=body, headers=hdr).json
    assert got["new"] == 0
    # a different sale: stored
    body2 = json.dumps(order(1002, 275, "ali@x.ae")).encode()
    assert c.post("/webhooks/shopify/bynawi", data=body2, headers={"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": sign(body2)}).json["new"] == 1
    st = c.get("/status/bynawi", headers=h).json["connections"]["shopify_hooks"]
    assert st["merged"] == 1 and st["orders"] == 1
    # a bad signature is refused
    assert c.post("/webhooks/shopify/bynawi", data=body2, headers={"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": sign(body2, "x" * 30)}).status_code == 401
    assert not c.post("/connect/bynawi/shopify-webhooks/disconnect", headers=h).json["connected"]
    assert c.post("/webhooks/shopify/bynawi", data=body2, headers={"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": sign(body2)}).status_code == 401


def test_stripe_after_shopify_is_merged_too(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_SECRET_KEY", "test-secret-key-for-encryption-0123456789")
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Bynawi", "email": "o@bynawi.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    c.post("/connect/bynawi/shopify-webhooks", json={"secret": KEY}, headers=h)
    body = json.dumps(order(2001, 160, "mona@x.ae")).encode()
    assert c.post("/webhooks/shopify/bynawi", data=body, headers={"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": sign(body)}).json["new"] == 1
    assert stripe(app, "bynawi", "ch_9", "2026-11-16T06:03:00+00:00", 160, "mona@x.ae")["new"] == 0     # same sale
    assert stripe(app, "bynawi", "ch_8", "2026-11-16T06:04:00+00:00", 160, "other@x.ae")["new"] == 1    # same price, other buyer
    assert stripe(app, "bynawi", "ch_7", "2026-11-16T09:30:00+00:00", 160, "mona@x.ae")["new"] == 1     # hours later: a new sale


def test_shopify_test_notifications_are_ignored():
    from paysignal.connectors import parse_shopify_webhook
    sample = {"id": 820982911946154508, "email": "jon@example.com", "total_price": "403.00", "currency": "USD",
              "created_at": "2021-12-31T19:00:00-05:00", "line_items": []}
    assert parse_shopify_webhook("orders/paid", sample, "b") == []
    assert parse_shopify_webhook("orders/paid", {**order(5, 100, "a@b.ae"), "test": True}, "b") == []
    assert parse_shopify_webhook("refunds/create", {"id": 1, "order_id": 820982911946154508}, "b") == []
    assert len(parse_shopify_webhook("orders/paid", order(5, 100, "a@b.ae"), "b")) == 1


def test_shopify_export_upload_merges_with_stripe(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Bynawi", "email": "o@bynawi.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    stripe(app, "bynawi", "ch_1", "2026-11-10T10:02:00+00:00", 340, "sara@x.ae")
    export = ("Name,Email,Financial Status,Paid at,Currency,Total,Lineitem quantity,Lineitem name,Lineitem price,Id\n"
              "#1001,sara@x.ae,paid,2026-11-10 14:00:00 +0400,AED,340.00,1,N°2 50ML,340.00,5001\n"     # = Stripe ch_1
              "#1002,ali@x.ae,paid,2026-11-11 12:00:00 +0400,AED,160.00,1,Discovery Box,160.00,5002\n")  # cash on delivery
    r = c.post("/import/bynawi", data=export, headers=h).json
    assert r["new"] == 1, r
    assert c.get("/status/bynawi", headers=h).json["totals"]["gross"] == 500.0
