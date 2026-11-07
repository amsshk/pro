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
    st.launches.db.execute("UPDATE ad_launches SET created_at='2026-09-01 00:00:00'")   # made before the test period
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


FEE = round(6000.50 * 5)
NEXT = {**INV, "id": "in_2", "period_start": 1792600000, "period_end": 1795200000}


def items(st):
    return [p for p in st.stripe_http.posts if p[0] == "invoiceitems"]


def test_days_follow_the_ad_account_time_zone_and_belong_to_one_period():
    from datetime import datetime, timezone
    from paysignal.settlement import local_days
    ts = lambda s: int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
    # Stripe period 1 Oct 00:00 UTC - 1 Nov 00:00 UTC; Dubai is UTC+4
    assert local_days(ts("2026-10-01T00:00:00"), ts("2026-11-01T00:00:00"), "Asia/Dubai") == ("2026-10-02", "2026-11-01")
    assert local_days(ts("2026-11-01T00:00:00"), ts("2026-12-01T00:00:00"), "Asia/Dubai")[0] == "2026-11-02"   # no overlap
    # an exact local-midnight boundary: the day belongs to the period it starts in
    assert local_days(ts("2026-09-30T20:00:00"), ts("2026-10-31T20:00:00"), "Asia/Dubai") == ("2026-10-01", "2026-10-31")
    # New York across the November DST change: still whole days, nothing lost or doubled
    a = local_days(ts("2026-10-01T04:00:00"), ts("2026-11-01T04:00:00"), "America/New_York")
    b = local_days(ts("2026-11-01T04:00:00"), ts("2026-12-01T05:00:00"), "America/New_York")
    assert a == ("2026-10-01", "2026-10-31") and b == ("2026-11-01", "2026-11-30")


def test_unreadable_spend_stays_pending_never_zero_then_settles(app):
    st, c, h, found = setup(app, Graph(refuse_pause=True))
    st.ads_http.down = True                                                   # Meta not answering when they leave
    c.post("/meta/glow-clinic/disconnect", json={"force": True}, headers=h)   # left running, access gone
    st.ads_http.down = False
    st.ledger.db.execute("DELETE FROM spend_sync"); st.ledger.db.execute("DELETE FROM spend_ledger")
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    st.settle_due_periods()
    fp = st.fees.for_client("glow-clinic")[0]
    assert fp["status"] == "pending" and "isn't connected" in fp["reason"] and fp["fee_minor"] is None
    assert c.get("/billing/glow-clinic", headers=h).json["fee_periods"][0]["status"] == "pending"
    st.meta_conns.save("glow-clinic", {"access_token": "LONG2", "expires_at": None}, found)
    st.meta_conns.select("glow-clinic", "act_11", "p1", "px1", True)
    st.settle_due_periods()
    assert st.fees.get(fp["id"])["status"] == "ready" and st.fees.get(fp["id"])["fee_minor"] == FEE


def test_settled_period_is_locked(app):
    st, c, h, _ = setup(app, Graph())
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    st.settle_due_periods()
    st.ledger.record("glow-clinic", "c1", "act_11", [{"date_start": "2026-10-02", "spend": "999", "account_currency": "AED"}])
    st.settle_due_periods()
    assert st.fees.for_client("glow-clinic")[0]["fee_minor"] == FEE


def test_campaign_switched_on_in_ads_manager_is_still_billed(app):
    st, c, h, _ = setup(app, Graph())
    st.billing.db.execute("UPDATE billing SET ads_trial_started=NULL")         # never pressed Go live in ProAds
    st.launches.set_status("glow-clinic", "c1", "PAUSED")
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    st.settle_due_periods()
    assert st.fees.for_client("glow-clinic")[0]["fee_minor"] == FEE


def test_deleted_campaign_is_billed_from_records_and_marked_incomplete(app):
    st, c, h, _ = setup(app, Graph())
    st.sync_all_spend()
    st.ledger.db.execute("UPDATE spend_sync SET synced_through='2026-10-05'")   # deleted before the last days were read
    real = st.ads_http.get
    st.ads_http.get = lambda url, params, timeout: R(400, {"error": {"code": 100, "message": "Object does not exist"}}) \
        if url.endswith("/insights") else real(url, params, timeout)
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    st.settle_due_periods()
    fp = st.fees.for_client("glow-clinic")[0]
    assert st.ledger.state("c1")["gone"] == 1 and fp["completeness"] == "incomplete" and fp["fee_minor"] == FEE
    signed(c, {"type": "invoice.created", "data": {"object": NEXT}})
    assert "based on recorded spend only" in items(st)[-1][1]["description"]


def test_cancelled_client_crash_after_charge_never_charges_twice(app):
    st, c, h, found = setup(app, Graph())
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    signed(c, {"type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "canceled", "currency": "aed", "metadata": {"client_id": "glow-clinic"},
        "items": {"data": [{"current_period_start": 1792600000}]}, "ended_at": 1793000000}}})
    assert len(st.fees.for_client("glow-clinic")) == 2                       # final partial period too
    st.settle_due_periods()
    real = st.fees.billed
    st.fees.billed = lambda *a: (_ for _ in ()).throw(RuntimeError("worker crashed"))
    try:
        st.bill_leftover_fees()
    except RuntimeError:
        pass
    st.fees.billed = real
    st.bill_leftover_fees()                                                   # restart (idempotency keys long gone)
    st.bill_leftover_fees()
    refs = [d["metadata[proads_ref]"] for _, d in items(st)]
    assert len(refs) == len(set(refs))                                        # every period charged at most once
    inv = [d for u, d in st.stripe_http.posts if u == "invoices"]
    assert inv and all(d["default_payment_method"] == "pm_card" for d in inv)
    assert all(p["status"] in ("billed", "nothing") for p in st.fees.for_client("glow-clinic"))


def test_meta_correction_after_billing_becomes_an_adjustment(app):
    st, c, h, _ = setup(app, Graph())
    signed(c, {"type": "invoice.created", "data": {"object": INV}})
    st.settle_due_periods()
    signed(c, {"type": "invoice.created", "data": {"object": NEXT}})
    fp = st.fees.for_client("glow-clinic")[-1]
    assert fp["status"] == "billed"
    st.fees.db.execute("UPDATE fee_periods SET billed_at=billed_at-8*86400 WHERE id=?", (fp["id"],))
    st.ledger.db.execute("UPDATE spend_sync SET next_try=0")
    orig = st.ads_http.get
    def revised(url, params, timeout):
        r = orig(url, params, timeout)
        if url.endswith("/insights") and params.get("time_increment"):
            r.body["data"][0]["spend"] = "7000.50"
        return r
    st.ads_http.get = revised
    st.recheck_billed_fees()
    adj = [p for p in st.fees.for_client("glow-clinic") if p["kind"] == "adjustment"]
    assert adj and adj[0]["fee_minor"] == round(7000.50 * 5) - FEE and adj[0]["status"] == "ready"
    assert st.fees.get(fp["id"])["fee_minor"] == FEE                         # the billed period itself never changes
    st.recheck_billed_fees()
    assert len([p for p in st.fees.for_client("glow-clinic") if p["kind"] == "adjustment"]) == 1
    signed(c, {"type": "invoice.created", "data": {"object": {**NEXT, "id": "in_3", "period_start": 1795200000, "period_end": 1797800000}}})
    assert "correction" in items(st)[-1][1]["description"]


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
