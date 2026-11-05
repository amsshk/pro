import base64
import hashlib
import hmac
import json
import time

import pytest

from paysignal.server import create_app

KEY = "test-api-key"
CLIENTS = {"shop1": {
    "api_key": KEY,
    "stripe_webhook_secret": "whsec_test",
    "shopify_webhook_secret": "shpss_test",
    "costs": {"SKU1": 40},
    "meta": {"dataset_id": "123", "access_token": "tok", "live": False},
}}


@pytest.fixture
def client(tmp_path):
    app = create_app(CLIENTS, str(tmp_path / "t.db"), start_worker=False)
    return app.test_client()


def stripe_post(client, event, secret="whsec_test"):
    body = json.dumps(event).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/stripe/shop1", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})


def shopify_post(client, topic, payload, secret="shpss_test"):
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return client.post("/webhooks/shopify/shop1", data=body,
                       headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": topic})


CHARGE = {"type": "charge.succeeded", "data": {"object": {
    "id": "ch_1", "amount": 25000, "currency": "aed", "created": int(time.time()),
    "billing_details": {"email": "a@x.com"}, "metadata": {"utm_content": "story1"}}}}


def test_health(client):
    assert client.get("/health").json["ok"]


def test_stripe_payment_is_stored_once(client):
    assert stripe_post(client, CHARGE).json["new"] == 1
    r = stripe_post(client, CHARGE)                       # Stripe retries the same event
    assert r.status_code == 200 and r.json["duplicates"] == 1


def test_fake_stripe_payment_rejected(client):
    assert stripe_post(client, CHARGE, secret="wrong").status_code == 401


def test_shopify_order_and_refund(client):
    order = {"id": 55, "total_price": "100.00", "currency": "AED", "email": "b@x.com",
             "line_items": [{"sku": "SKU1", "title": "t", "quantity": 1, "price": "100.00"}]}
    assert shopify_post(client, "orders/paid", order).json["new"] == 1
    refund = {"id": 9, "order_id": 55, "transactions": [{"kind": "refund", "status": "success",
                                                          "amount": "100.00", "currency": "AED"}]}
    assert shopify_post(client, "refunds/create", refund).json["new"] == 1
    totals = client.get("/report/shop1", headers={"X-API-Key": KEY}).json["totals"]
    assert totals == {"gross": 100.0, "refunds": 100.0, "net": 0.0}


def test_fake_shopify_rejected(client):
    assert shopify_post(client, "orders/paid", {"id": 1}, secret="wrong").status_code == 401


def test_unknown_client_404(client):
    assert client.post("/webhooks/stripe/nobody", data=b"{}").status_code == 404


def test_csv_needs_api_key(client):
    csv = "id,date,amount,currency\ncash1,2026-11-01,50,AED\n"
    assert client.post("/upload/csv/shop1", data=csv).status_code == 401
    r = client.post("/upload/csv/shop1", data=csv, headers={"X-API-Key": KEY})
    assert r.status_code == 200 and r.json["new"] == 1


def test_bad_csv_reports_errors(client):
    r = client.post("/upload/csv/shop1", data="foo,bar\n1,2\n", headers={"X-API-Key": KEY})
    assert r.status_code == 400 and "missing columns" in r.json["errors"][0]


def test_meta_sync_dry_run(client):
    stripe_post(client, CHARGE)
    r = client.post("/sync/meta/shop1", headers={"X-API-Key": KEY}).json
    assert r["mode"] == "dry_run" and r["events"] == 1 and not r["errors"]


def test_report_with_posts(client):
    stripe_post(client, CHARGE)
    posts = "post_id,platform,posted_at,views,format,hook\nstory1,instagram,2026-11-01T20:00:00,3000,story,testimonial\n"
    assert client.post("/upload/posts/shop1", data=posts, headers={"X-API-Key": KEY}).json["posts"] == 1
    rep = client.get("/report/shop1", headers={"X-API-Key": KEY}).json
    assert rep["posts"][0]["post_id"] == "story1" and rep["posts"][0]["orders"] == 1
