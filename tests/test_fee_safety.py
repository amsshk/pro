import os

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
    c.post("/meta/glow-clinic/disconnect", json={"force": True}, headers=h)   # left running, access gone
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
