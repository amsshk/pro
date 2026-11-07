import time

from paysignal import proads_revenue
from test_billing import ACCT, app, signed  # noqa: F401  (fixture)


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def setup(app, consent=True):
    state = app.extensions["paysignal"]
    state.billing_sync = True
    state.growth_sent = []
    c = app.test_client()
    body = {**ACCT, "ads_consent": consent, "fbp": "fb.1.1.123", "fbclid": "CLICK1",
            "source_url": "https://pro.example.net/?utm_source=meta&utm_medium=paid&utm_campaign=launch-thb",
            "utm": {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "launch-thb", "utm_content": "launch-thb-1-1"}}
    tok = c.post("/auth/signup", json=body).json["token"]
    signed(c, {"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "client_reference_id": "glow-clinic", "customer": "cus_1", "subscription": "sub_1",
        "payment_status": "paid"}}})
    return state, c, tok


def invoice(iid, reason="subscription_create", paid=31395, tax=1495, **extra):
    return {"type": "invoice.paid", "data": {"object": {
        "id": iid, "customer": "cus_1", "status": "paid", "amount_paid": paid, "currency": "aed",
        "billing_reason": reason, "subscription": "sub_1", "total_taxes": [{"amount": tax}] if tax else [],
        "payment_intent": f"pi_{iid}", "charge": f"ch_{iid}", "status_transitions": {"paid_at": int(time.time())}, **extra}}}


def purchases_sent(state):
    return [e for e, _ in state.growth_sent if e["event_name"] == "Purchase"]


def test_first_payment_is_a_new_customer_sent_once_net_of_tax(app):
    state, c, _ = setup(app)
    assert signed(c, invoice("in_1")).status_code == 200
    signed(c, invoice("in_1"))                                     # Stripe retries: no double count
    rows = state.revenue.all()
    assert len(rows) == 1 and rows[0]["kind"] == "first" and rows[0]["amount"] == 299.0 and rows[0]["tax"] == 14.95
    assert rows[0]["utm"]["utm_campaign"] == "launch-thb"
    assert wait_for(lambda: len(purchases_sent(state)) == 1)
    time.sleep(0.1)
    ev = purchases_sent(state)
    assert len(ev) == 1
    ev = ev[0]
    assert ev["event_id"] == "proads:first:in_1" and ev["custom_data"]["value"] == 299.0
    assert ev["custom_data"]["currency"] == "AED" and ev["user_data"]["fbp"] == "fb.1.1.123"
    assert ev["user_data"]["fbc"].endswith(".CLICK1")
    # stored as ProAds' own purchase with the sign-up attribution, already marked sent (never re-sent by the pipeline)
    p = state.store.purchase("pro", "in_1")
    assert p.amount == 299.0 and p.utm_campaign == "launch-thb" and p.fbp == "fb.1.1.123" and p.customer_id == "glow-clinic"
    assert not [x for x in state.store.unsent("pro") if x.external_id == "in_1"]


def test_renewals_and_fees_are_revenue_not_new_customers(app):
    state, c, _ = setup(app)
    signed(c, invoice("in_1"))
    signed(c, invoice("in_2", reason="subscription_cycle"))
    signed(c, invoice("in_3", reason="manual", tax=0, paid=1500, subscription=None, metadata={"kind": "ad_spend_fee"}))
    signed(c, invoice("in_4", paid=0))                             # a 100% discount: nothing collected, nothing recorded
    kinds = {r["invoice_id"]: r["kind"] for r in state.revenue.all()}
    assert kinds == {"in_1": "first", "in_2": "renewal", "in_3": "fee"}
    time.sleep(0.2)
    assert [e["event_id"] for e in purchases_sent(state)] == ["proads:first:in_1"]


def test_no_meta_event_without_ads_consent(app):
    state, c, _ = setup(app, consent=False)
    signed(c, invoice("in_1"))
    time.sleep(0.2)
    assert state.revenue.all()[0]["kind"] == "first" and purchases_sent(state) == []


def test_refunds_and_chargebacks_reduce_profit_once(app):
    state, c, _ = setup(app)
    signed(c, invoice("in_1"))
    refund = {"type": "charge.refunded", "data": {"object": {"id": "ch_in_1", "payment_intent": "pi_in_1",
                                                             "amount_refunded": 15698}}}       # half, including tax
    signed(c, refund)
    signed(c, refund)                                              # duplicate delivery
    r = state.revenue.get("in_1")
    assert r["refunded"] == 149.5
    assert state.store.refunded_amount("pro", "in_1") == 149.5
    signed(c, {"type": "charge.dispute.closed", "data": {"object": {"id": "dp_1", "charge": "ch_in_1", "status": "lost",
                                                                    "amount": 31395}}})
    signed(c, {"type": "charge.dispute.closed", "data": {"object": {"id": "dp_1", "charge": "ch_in_1", "status": "lost",
                                                                    "amount": 31395}}})
    r = state.revenue.get("in_1")
    assert r["chargeback"] == 149.5                                # capped at what was still kept
    assert wait_for(lambda: len([e for e, _ in state.growth_sent if e["event_name"] == "Refund"]) == 2)


def test_customer_acquisition_profit_labels_estimated_cost(app, monkeypatch):
    state, c, tok = setup(app)
    monkeypatch.setenv("PROADS_SERVICE_COST_PER_CLIENT", "40")
    state.launches.add("pro", {"campaign_id": "c1", "name": "Launch THB", "utm_slug": "launch-thb", "status": "ACTIVE"})
    state.ledger.record("pro", "c1", "act_1", [{"day": "2026-11-01", "spend": 120.0, "currency": "AED"}])
    signed(c, invoice("in_1"))
    signed(c, invoice("in_2", reason="subscription_cycle"))
    out = c.get("/acquisition/pro", headers={"X-API-Key": "admin-key-0123456789abc"}).json
    assert out["new_subscribers"] == 1 and out["first_payments"] == 299.0 and out["renewals"] == 299.0
    assert out["service_cost_is_estimate"] and "ESTIMATE" in out["note"]
    camp = out["campaigns"][0]
    assert camp["slug"] == "launch-thb" and camp["subscribers"] == 1 and camp["renewals"] == 299.0
    assert camp["campaign"] == "Launch THB" and camp["spend"] == 120.0 and camp["acquisition_cost"] == 120.0
    assert camp["acquisition_contribution_each"] == 299.0 - 120.0 - 40.0
    # not for normal clients
    assert c.get("/acquisition/glow-clinic", headers={"X-API-Key": tok}).status_code == 403


def test_acquisition_profit_math():
    rows = [{"invoice_id": "a", "client_id": "x", "kind": "first", "amount": 299.0, "refunded": 0.0, "chargeback": 0.0,
             "currency": "AED", "utm": {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "s1"}},
            {"invoice_id": "b", "client_id": "y", "kind": "first", "amount": 299.0, "refunded": 299.0, "chargeback": 0.0,
             "currency": "AED", "utm": {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "s1"}},
            {"invoice_id": "c", "client_id": "z", "kind": "first", "amount": 799.0, "refunded": 0.0, "chargeback": 0.0,
             "currency": "AED", "utm": {}}]
    out = proads_revenue.acquisition_profit(rows, {"s1": 400.0}, {"s1": "Spring"}, 50.0, True)
    s1 = next(g for g in out["campaigns"] if g["slug"] == "s1")
    assert s1["campaign"] == "Spring" and s1["acquisition_cost"] == 200.0 and s1["first_payment_each"] == 149.5
    assert s1["acquisition_contribution_each"] == -100.5
    organic = next(g for g in out["campaigns"] if not g["slug"])
    assert organic["campaign"] == "No campaign (direct or organic)" and organic["acquisition_contribution_each"] == 749.0
    assert out["refunds"] == 299.0 and out["new_subscribers"] == 3
