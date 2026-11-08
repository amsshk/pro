import time

import pytest

from paysignal import stripe_sync
from test_billing import ACCT, app  # noqa: F401  (fixture)

NOW = int(time.time())


class R:
    def __init__(self, body, status=200):
        self.b, self.status_code = body, status

    def json(self):
        return self.b


def charge(cid, amount=10000, refunded=0, status="succeeded", created=None, **kw):
    return {"id": cid, "status": status, "paid": status == "succeeded", "captured": True, "amount": amount,
            "amount_refunded": refunded, "currency": "aed", "created": created or NOW - 86400,
            "billing_details": {"email": None, "name": "Sara Ali", "phone": None, "address": {"city": "Dubai", "country": "AE"}},
            "customer": {"id": "cus_1", "email": "sara@x.ae", "name": "Sara Ali"},
            "metadata": {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "spring"}, **kw}


class FakeStripe:
    def __init__(self):
        self.charges = []
        self.refunds = []
        self.disputes = []
        self.no_customers = False
        self.revoked = False
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("/v1/")[1]
        self.calls.append((path, dict(params or {})))
        if self.revoked:
            return R({"error": {"message": "Invalid API Key"}}, 401)
        if path == "charges":
            if self.no_customers and params.get("expand[]"):
                return R({"error": {"message": "no"}}, 403)
            since = int(params.get("created[gte]", 0))
            data = sorted([c for c in self.charges if c["created"] >= since], key=lambda c: -c["created"])
            if params.get("starting_after"):
                i = [c["id"] for c in data].index(params["starting_after"])
                data = data[i + 1:]
            lim = int(params.get("limit", 10))
            return R({"data": data[:lim], "has_more": len(data) > lim})
        if path.startswith("charges/"):
            return R(next(c for c in self.charges if c["id"] == path.split("/")[1]))
        if path == "refunds":
            return R({"data": self.refunds})
        if path == "disputes":
            return R({"data": self.disputes})
        return R({}, 404)


def test_key_rules():
    s = FakeStripe()
    with pytest.raises(stripe_sync.StripeSyncError, match="publishable"):
        stripe_sync.check_key(s, "pk_live_x")
    with pytest.raises(stripe_sync.StripeSyncError, match="move money"):
        stripe_sync.check_key(s, "sk_live_x")
    assert stripe_sync.check_key(s, "rk_live_abc")["live"]
    s.revoked = True
    with pytest.raises(stripe_sync.StripeSyncError, match="didn't accept"):
        stripe_sync.check_key(s, "rk_live_abc")


@pytest.fixture
def setup(app, monkeypatch):
    monkeypatch.setenv("PROADS_SECRET_KEY", "a-long-secret-key-for-tests")
    state = app.extensions["paysignal"]
    state.billing_sync = True
    s = FakeStripe()
    state.stripe_http = s
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    return state, s, c, {"X-API-Key": tok}


def sales(state):
    rows = state.store.db.execute("SELECT external_id, kind, amount FROM payments WHERE client_id='glow-clinic'"
                                  " ORDER BY external_id").fetchall()
    return [(a, b, round(c, 2)) for a, b, c in rows]


def test_connect_imports_history_with_details(setup):
    state, s, c, H = setup
    s.charges = [charge("ch_1"), charge("ch_2", 5000, refunded=2000), charge("ch_3", status="failed"),
                 charge("ch_old", created=NOW - 400 * 86400)]
    assert c.post("/connect/glow-clinic/stripe", headers=H, json={"key": "sk_live_x"}).status_code == 400
    r = c.post("/connect/glow-clinic/stripe", headers=H, json={"key": "rk_live_abc"}).json
    assert r["connected"] and r["status"] == "ok" and r["imported"] == 3
    assert sales(state) == [("ch_1", "purchase", 100.0), ("ch_2", "purchase", 50.0), ("ch_2:refund:2000", "refund", 20.0)]
    p = state.store.purchase("glow-clinic", "ch_1")
    assert p.email == "sara@x.ae" and p.utm_campaign == "spring" and p.city == "Dubai" and p.first_name == "Sara"
    st = c.get("/status/glow-clinic", headers=H).json["connections"]
    assert st["stripe_api"]["connected"] and "key_enc" not in st["stripe_api"] and st["stripe"]["configured"]


def test_partial_refunds_counted_once_across_webhook_and_sync(setup):
    state, s, c, H = setup
    s.charges = [charge("ch_1", 10000, refunded=1000)]
    c.post("/connect/glow-clinic/stripe", headers=H, json={"key": "rk_live_abc"})
    s.charges = [charge("ch_1", 10000, refunded=3000)]           # a second partial refund of 20
    state.stripe_sync_one("glow-clinic")
    state.stripe_sync_one("glow-clinic")                         # nothing new: no double count
    assert state.store.refunded_amount("glow-clinic", "ch_1") == 30.0
    # the webhook path reports the same running total: recognised, not added again
    from paysignal.connectors import parse_stripe_event
    ev = {"type": "charge.refunded", "data": {"object": charge("ch_1", 10000, refunded=3000)}}
    state.ingest("glow-clinic", parse_stripe_event(ev, "glow-clinic"))
    assert state.store.refunded_amount("glow-clinic", "ch_1") == 30.0


def test_refund_on_old_charge_and_lost_dispute_capped(setup):
    state, s, c, H = setup
    s.charges = [charge("ch_1", 10000, created=NOW - 100 * 86400)]
    c.post("/connect/glow-clinic/stripe", headers=H, json={"key": "rk_live_abc"})
    state.store.db.execute("UPDATE stripe_keys SET last_sync=?", (NOW - 3600,))
    s.charges = [charge("ch_1", 10000, refunded=4000, created=NOW - 100 * 86400)]
    s.refunds = [{"id": "re_1", "charge": "ch_1", "amount": 4000, "status": "succeeded"}]
    s.disputes = [{"id": "dp_1", "charge": "ch_1", "status": "lost", "amount": 10000, "currency": "aed", "created": NOW}]
    state.stripe_sync_one("glow-clinic")
    assert state.store.refunded_amount("glow-clinic", "ch_1") == 100.0      # 40 refunded + 60 lost (not 100 more)


def test_missing_customer_access_and_revoked_key(setup):
    state, s, c, H = setup
    s.no_customers = True
    s.charges = [charge("ch_1", customer="cus_9")]
    r = c.post("/connect/glow-clinic/stripe", headers=H, json={"key": "rk_live_abc"}).json
    assert r["status"] == "ok" and sales(state) == [("ch_1", "purchase", 100.0)]
    s.revoked = True
    state.stripe_sync_one("glow-clinic")
    st = c.get("/status/glow-clinic", headers=H).json["connections"]["stripe_api"]
    assert st["status"] == "error" and "paste a new one" in st["message"]
    assert c.post("/connect/glow-clinic/stripe/disconnect", headers=H).json["ok"]
    assert not c.get("/status/glow-clinic", headers=H).json["connections"]["stripe_api"]["connected"]
