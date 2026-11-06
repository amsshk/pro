import hashlib
import hmac
import json
import time

import pytest

from paysignal import billing
from paysignal.server import create_app
from test_meta_ads import PNG, FakeGraph, plan

ACCT = {"business": "Glow Clinic", "email": "owner@glow.ae", "password": "first-password-1", "terms": True}


class R:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class FakeStripe:
    def __init__(self):
        self.posts = []

    def post(self, url, data, auth, timeout, headers=None):
        assert auth == ("sk_test_x", "")
        self.posts.append((url.rsplit("/v1/", 1)[1], data))
        self.headers = headers
        if url.endswith("invoiceitems"):
            return R(200, {"id": "ii_1"})
        if url.endswith("checkout/sessions"):
            return R(200, {"url": "https://checkout.stripe.com/c/pay/cs_1"})
        if url.endswith("billing_portal/sessions"):
            return R(200, {"url": "https://billing.stripe.com/p/session/1"})
        return R(400, {"error": {"message": "nope"}})

    def get(self, url, params, auth, timeout):
        return R(200, {"unit_amount": 19900, "currency": "aed", "recurring": {"interval": "month"},
                       "product": {"name": "ProAds Pro"}})


def signed(c, event):
    body = json.dumps(event).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec_x", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return c.post("/webhooks/proads-billing", data=body, headers={"Stripe-Signature": f"t={t},v1={sig}"})


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"PROADS_STRIPE_SECRET_KEY": "sk_test_x", "PROADS_STRIPE_WEBHOOK_SECRET": "whsec_x",
                 "PROADS_STRIPE_PRICE_ID": "price_1", "PROADS_TRIAL_DAYS": "14", "PROADS_REQUIRE_PLAN": "1",
                 "PROADS_PUBLIC_URL": "https://pro.example.net"}.items():
        monkeypatch.setenv(k, v)
    billing._price_cache.clear()
    a = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "x.db"), start_worker=False)
    a.extensions["paysignal"].stripe_http = FakeStripe()
    return a


def test_upgrade_webhooks_portal_and_cancel(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    H = {"X-API-Key": tok}
    b = c.get("/billing/glow-clinic", headers=H).json
    assert b["available"] and not b["active"] and b["price"] == {"name": "ProAds Pro", "amount": 199.0, "currency": "AED", "interval": "month"}
    assert c.post("/billing/glow-clinic/portal", headers=H).status_code == 400
    url = c.post("/billing/glow-clinic/checkout", headers=H).json["url"]
    assert url.startswith("https://checkout.stripe.com/")
    data = app.extensions["paysignal"].stripe_http.posts[0][1]
    assert data["mode"] == "subscription" and data["client_reference_id"] == "glow-clinic"
    assert data["customer_email"] == "owner@glow.ae" and data["subscription_data[trial_period_days]"] == "14"
    assert data["success_url"] == "https://pro.example.net/app#/billing?paid=1"
    # forged webhooks are refused
    assert c.post("/webhooks/proads-billing", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"}).status_code == 400
    assert signed(c, {"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "client_reference_id": "glow-clinic", "customer": "cus_1", "subscription": "sub_1",
        "payment_status": "no_payment_required"}}}).status_code == 200
    assert c.get("/billing/glow-clinic", headers=H).json["active"]
    signed(c, {"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "trialing", "metadata": {"client_id": "glow-clinic"},
        "items": {"data": [{"current_period_end": 1900000000}]}, "cancel_at_period_end": True}}})
    b = c.get("/billing/glow-clinic", headers=H).json
    assert b["status"] == "trialing" and b["period_end"] == 1900000000 and b["cancel_at_period_end"]
    assert c.post("/billing/glow-clinic/checkout", headers=H).status_code == 409
    assert c.post("/billing/glow-clinic/portal", headers=H).json["url"].startswith("https://billing.stripe.com/")
    signed(c, {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled"}}})
    assert not c.get("/billing/glow-clinic", headers=H).json["active"]
    # second checkout reuses the customer and gives no new trial
    c.post("/billing/glow-clinic/checkout", headers=H)
    data = app.extensions["paysignal"].stripe_http.posts[-1][1]
    assert data["customer"] == "cus_1" and "subscription_data[trial_period_days]" not in data


def test_publishing_needs_a_plan_when_switched_on(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    r = c.post("/campaign/glow-clinic/publish", json={"plan": plan(), "images": {"0-0": PNG}}, headers={"X-API-Key": tok})
    assert r.status_code == 402 and r.json["needs_plan"]


def test_unknown_events_and_accounts_are_ignored(app):
    c = app.test_client()
    assert signed(c, {"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_9", "customer": "cus_9", "status": "active", "metadata": {"client_id": "ghost"}}}}).status_code == 200
    assert app.extensions["paysignal"].billing.get("ghost") is None
    assert signed(c, {"type": "charge.refunded", "data": {"object": {}}}).json == {"received": True}


def test_renewal_invoice_gets_the_ad_spend_fee(app):
    state = app.extensions["paysignal"]
    state.billing_sync = True
    state.ads_http = g = FakeGraph()
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    found = {"user": "Sara", "ad_accounts": [{"id": "act_11", "name": "Glow", "currency": "AED", "active": True}],
             "pages": [{"id": "p1", "name": "Glow", "instagram_id": "", "instagram": ""}]}
    monkey_key = "a-long-secret-key-for-tests"
    import os
    os.environ["PROADS_SECRET_KEY"] = monkey_key
    try:
        state.meta_conns.save("glow-clinic", {"access_token": "LONG", "expires_at": None}, found)
        state.meta_conns.select("glow-clinic", "act_11", "p1", "px1", True)
        state.launches.add("glow-clinic", {"campaign_id": "c1", "ad_sets": [], "ads": [], "status": "ACTIVE"})
        signed(c, {"type": "customer.subscription.updated", "data": {"object": {
            "id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"client_id": "glow-clinic"},
            "items": {"data": [{"current_period_start": 1790000000, "current_period_end": 1792600000}]}}}})
        inv = {"id": "in_1", "customer": "cus_1", "currency": "aed", "status": "draft", "billing_reason": "subscription_cycle",
               "period_start": 1790000000, "period_end": 1792600000}
        assert signed(c, {"type": "invoice.created", "data": {"object": inv}}).status_code == 200
        url, data = state.stripe_http.posts[-1]
        assert url == "invoiceitems" and data["invoice"] == "in_1" and data["amount"] == "18002"   # 3% of 6000.50 AED
        assert "6,000.50" in data["description"] and state.stripe_http.headers == {"Idempotency-Key": "proads-fee-in_1"}
        n = len(state.stripe_http.posts)
        signed(c, {"type": "invoice.created", "data": {"object": inv}})                    # Stripe retries: no double fee
        assert len(state.stripe_http.posts) == n
        b = c.get("/billing/glow-clinic", headers={"X-API-Key": tok}).json
        assert b["fees"][0]["fee"] == 180.02 and b["spend_so_far"]["fee"] == 180.02 and b["fee_percent"] == 3
        # first invoice of a subscription and other currencies get no fee
        signed(c, {"type": "invoice.created", "data": {"object": {**inv, "id": "in_2", "billing_reason": "subscription_create"}}})
        signed(c, {"type": "invoice.created", "data": {"object": {**inv, "id": "in_3", "currency": "usd"}}})
        assert len(state.stripe_http.posts) == n
    finally:
        os.environ.pop("PROADS_SECRET_KEY", None)
