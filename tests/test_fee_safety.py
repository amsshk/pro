import os
import time

import pytest

from paysignal import billing
from test_billing import ACCT, FakeStripe, R, app, signed  # noqa: F401  (fixture)
from test_meta_ads import FakeGraph

H = {}


class Graph(FakeGraph):
    """Remembers each campaign's switch, like Meta does."""
    def __init__(self, refuse_pause=False):
        super().__init__()
        self.switch, self.refuse_pause, self.down = {}, refuse_pause, False

    def post(self, url, data, timeout):
        obj = url.rsplit("/", 1)[-1]
        if obj in ("c1", "s1", "a1"):
            if self.refuse_pause:
                return R(400, {"error": {"message": "Permissions error"}})
            self.switch[obj] = data.get("status")
            return R(200, {"success": True})
        return super().post(url, data, timeout)

    def get(self, url, params, timeout):
        if self.down and url.endswith("/insights"):
            raise ConnectionError("down")
        obj = url.rsplit("/", 1)[-1]
        if params.get("fields") == "status":
            return R(200, {"status": self.switch.get(obj, "ACTIVE")})
        return super().get(url, params, timeout)


def setup(app, graph):
    st = app.extensions["paysignal"]
    st.billing_sync = True
    st.ads_http = graph
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    os.environ["PROADS_SECRET_KEY"] = "a-long-secret-key-for-tests"
    found = {"user": "Sara", "ad_accounts": [{"id": "act_11", "name": "Glow", "currency": "AED", "active": True}],
             "pages": [{"id": "p1", "name": "Glow", "instagram_id": "", "instagram": ""}]}
    st.meta_conns.save("glow-clinic", {"access_token": "LONG", "expires_at": None}, found)
    st.meta_conns.select("glow-clinic", "act_11", "p1", "px1", True)
    st.launches.add("glow-clinic", {"campaign_id": "c1", "name": "Spring", "ad_sets": ["s1"], "ads": ["a1"], "status": "ACTIVE"})
    st.billing.start_ads_trial("glow-clinic")
    signed(c, {"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"client_id": "glow-clinic"},
        "items": {"data": [{"current_period_start": 1790000000, "current_period_end": 1792600000}]}}}})
    return st, c, {"X-API-Key": tok}, found


INV = {"id": "in_1", "customer": "cus_1", "currency": "aed", "status": "draft", "billing_reason": "subscription_cycle",
       "period_start": 1790000000, "period_end": 1792600000}


@pytest.fixture(autouse=True)
def clean_env():
    yield
    os.environ.pop("PROADS_SECRET_KEY", None)


def test_disconnect_pauses_running_campaigns_first(app):
    st, c, h, _ = setup(app, Graph())
    r = c.post("/meta/glow-clinic/disconnect", headers=h).json
    assert r["paused"] == ["c1"] and r["still_running"] == []
    assert st.ads_http.switch == {"c1": "PAUSED", "s1": "PAUSED", "a1": "PAUSED"}
    assert st.launches.list("glow-clinic")[0]["status"] == "PAUSED" and st.meta_conns.get("glow-clinic") is None


def test_disconnect_refuses_when_it_cannot_pause(app):
    st, c, h, _ = setup(app, Graph(refuse_pause=True))
    r = c.post("/meta/glow-clinic/disconnect", headers=h)
    assert r.status_code == 409 and r.json["not_paused"][0]["campaign_id"] == "c1" and r.json["can_force"]
    assert st.meta_conns.get("glow-clinic") is not None                     # still connected
    r = c.post("/meta/glow-clinic/disconnect", json={"force": True}, headers=h)
    assert r.status_code == 200 and r.json["still_running"][0]["campaign_id"] == "c1"


def test_spend_is_never_treated_as_zero_and_is_billed_once_readable(app):
    st, c, h, found = setup(app, Graph(refuse_pause=True))
    st.ads_http.down = True                                                   # Meta not answering when they leave
    c.post("/meta/glow-clinic/disconnect", json={"force": True}, headers=h)   # left running, access gone
    st.ads_http.down = False
    n = len(st.stripe_http.posts)
    assert signed(c, {"type": "invoice.created", "data": {"object": INV}}).status_code == 200
    assert len(st.stripe_http.posts) == n                                     # monthly invoice goes out, no guessed fee
    b = c.get("/billing/glow-clinic", headers=h).json
    assert b["fees_pending"] and b["fees_pending"][0]["period_start"] == 1790000000
    st.reconcile_pending_fees()                                               # still disconnected: stays pending
    assert len(st.billing.pending("glow-clinic")) == 1 and len(st.stripe_http.posts) == n
    st.meta_conns.save("glow-clinic", {"access_token": "LONG2", "expires_at": None}, found)
    st.meta_conns.select("glow-clinic", "act_11", "p1", "px1", True)
    st.reconcile_pending_fees()
    (u1, item), (u2, inv) = st.stripe_http.posts[-2:]
    assert u1 == "invoiceitems" and "invoice" not in item and item["amount"] == str(billing.fee_amount(6000.50, 5))
    assert "catch-up" in item["description"] and u2 == "invoices" and inv["pending_invoice_items_behavior"] == "include"
    assert st.billing.pending("glow-clinic") == []
    st.reconcile_pending_fees()
    assert len(st.stripe_http.posts) == n + 2                                 # billed once
    fees = c.get("/billing/glow-clinic", headers=h).json["fees"]
    assert any(f["invoice_id"].startswith("catch-up-") and f["fee"] > 0 for f in fees)


def test_meta_down_for_a_day_moves_the_fee_to_catch_up(app, monkeypatch):
    st, c, h, _ = setup(app, Graph())
    st.ads_http.down = True
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    assert st.stripe_http.posts[-1] == ("invoices/in_1", {"auto_advance": "false"})
    st.billing.db.execute("UPDATE billing_fee_held SET held_at=held_at-90000")
    st.retry_held_fees()
    assert st.stripe_http.posts[-1] == ("invoices/in_1", {"auto_advance": "true"}) and len(st.billing.pending()) == 1
    st.ads_http.down = False
    st.reconcile_pending_fees()
    assert [p[0] for p in st.stripe_http.posts[-2:]] == ["invoiceitems", "invoices"] and st.billing.pending() == []


def test_campaign_switched_on_in_ads_manager_is_still_billed(app):
    st, c, h, _ = setup(app, Graph())
    st.billing.db.execute("UPDATE billing SET ads_trial_started=NULL")         # never pressed Go live in ProAds
    st.launches.set_status("glow-clinic", "c1", "PAUSED")
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    url, data = st.stripe_http.posts[-1]
    assert url == "invoiceitems" and data["amount"] == str(billing.fee_amount(6000.50, 5))


def test_deleted_campaign_keeps_its_recorded_spend(app):
    st, c, h, _ = setup(app, Graph())
    st.sync_all_spend()                                                       # spend recorded while it existed
    gone = st.ads_http.get
    st.ads_http.get = lambda url, params, timeout: R(400, {"error": {"code": 100, "message": "Object does not exist"}}) \
        if url.endswith("/insights") else gone(url, params, timeout)
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    assert st.ledger.state("c1")["gone"] == 1
    url, data = st.stripe_http.posts[-1]
    assert url == "invoiceitems" and data["amount"] == str(billing.fee_amount(6000.50, 5))


def test_crash_after_stripe_charged_never_charges_twice(app):
    st, c, h, found = setup(app, Graph(refuse_pause=True))
    st.ads_http.down = True
    c.post("/meta/glow-clinic/disconnect", json={"force": True}, headers=h)
    st.ads_http.down = False
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    signed(c, {"type": "customer.subscription.deleted", "data": {"object": {     # they cancel meanwhile
        "id": "sub_1", "customer": "cus_1", "status": "canceled", "metadata": {"client_id": "glow-clinic"}}}})
    st.meta_conns.save("glow-clinic", {"access_token": "LONG2", "expires_at": None}, found)
    st.meta_conns.select("glow-clinic", "act_11", "p1", "px1", True)
    real = st.billing.resolve_pending
    st.billing.resolve_pending = lambda pid: (_ for _ in ()).throw(RuntimeError("worker crashed"))
    try:
        st.reconcile_pending_fees()
    except RuntimeError:
        pass
    st.billing.resolve_pending = real
    items = [p for p in st.stripe_http.posts if p[0] == "invoiceitems"]
    invoices = [p for p in st.stripe_http.posts if p[0] == "invoices"]
    assert len(items) == 1 and len(invoices) == 1 and invoices[0][1]["default_payment_method"] == "pm_card"
    st.reconcile_pending_fees()                                               # restart, days later (no idempotency key left)
    assert len([p for p in st.stripe_http.posts if p[0] == "invoiceitems"]) == 1
    assert st.billing.pending("glow-clinic") == []
    assert any(f["invoice_id"].startswith("catch-up-") for f in st.billing.fees("glow-clinic"))


def test_ledger_takes_revisions_backs_off_and_flags_gaps(tmp_path):
    import sqlite3
    from paysignal import ledger
    L = ledger.SpendLedger(sqlite3.connect(":memory:"))
    L.record("x", "c9", "act_1", [{"date_start": "2026-10-01", "spend": "10", "account_currency": "AED"}])
    L.record("x", "c9", "act_1", [{"date_start": "2026-10-01", "spend": "12.5", "account_currency": "AED"}])
    L.synced("x", "c9", "2026-10-01")
    assert L.total(["c9"], "2026-10-01", "2026-10-31") == (12.5, "AED")      # Meta's revision wins until billed
    assert L.uncovered(["c9", "c10"], "2026-10-31") == ["c9", "c10"]        # not known through the end: no bill

    class Limited:
        calls = 0

        def insights(self, ids, since, until, daily):
            Limited.calls += 1
            e = RuntimeError("User request limit reached")
            e.code = 17
            raise e
    out = ledger.sync(Limited(), L, "x", [{"campaign_id": "c9"}], "act_1", force=True, today="2026-11-02")
    assert out["failed"] == ["c9"] and L.state("c9")["next_try"] > time.time() + 3000
    ledger.sync(Limited(), L, "x", [{"campaign_id": "c9"}], "act_1", today="2026-11-02")
    assert Limited.calls == 1                                                 # backing off, not hammering Meta
