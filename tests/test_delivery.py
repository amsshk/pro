import time

from paysignal import delivery
from test_billing import ACCT, app, signed  # noqa: F401  (fixture)
from test_proads_revenue import invoice, wait_for


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.content = status, body, b"x"

    def json(self):
        return self._b


class Meta:
    """Fake Conversions API: answers from a script, records every send."""
    def __init__(self):
        self.answer = (200, {"events_received": 1, "fbtrace_id": "T"})
        self.sent = []

    def post(self, url, params=None, json=None, timeout=None):
        self.sent.append(json["data"][0]["event_id"])
        return Resp(*self.answer)


DEAD = (400, {"error": {"code": 190, "error_subcode": 460, "message": "Error validating access token: session invalidated"}})
BAD = (400, {"error": {"code": 100, "message": "Invalid parameter", "error_user_msg": "event_time is in the future"}})


def ev(eid, t=None):
    return {"event_name": "CompleteRegistration", "event_id": eid, "event_time": int(t or time.time()),
            "user_data": {"em": ["h"], "fbp": "fb.1"}}


def test_classify():
    assert delivery.classify({"status": 200, "body": {"events_received": 1}})[0] == "accepted"
    st, msg, code, _ = delivery.classify({"status": DEAD[0], "body": DEAD[1]})
    assert st == "retrying" and code == 190 and "System User token" in msg
    assert delivery.classify({"status": BAD[0], "body": BAD[1]})[0] == "rejected"
    assert delivery.classify({"error": "timeout"})[0] == "retrying"
    assert delivery.classify({"skipped": "not set"})[0] == "retrying"
    assert delivery.classify({"status": 503, "body": {}})[0] == "retrying"


def test_log_backoff_dedup_and_expiry(app):
    log = app.extensions["paysignal"].delivery
    now = 1_800_000_000
    assert log.queued("proads", ev("a", now), now) and not log.queued("proads", ev("a", now), now)
    assert log.result("a", {"status": DEAD[0], "body": DEAD[1]}, now) == "retrying"
    assert log.due(now + 10) == []                                    # waits 5 minutes
    assert [e for e, _ in log.due(now + 301)] == ["a"]
    assert log.result("a", {"status": 200, "body": {"events_received": 1}}, now + 301) == "accepted"
    h = log.health("proads", 7, now + 400)
    assert h["healthy"] and h["recovered"] == 1 and h["accepted"] == 1
    log.queued("proads", ev("old", now), now)
    log.result("old", {"error": "down"}, now)
    assert log.due(now + 7 * 86400) == [] and log.status_of("old")["status"] == "expired"


def test_dead_token_then_fixed_recovers_every_event_once(app, monkeypatch):
    monkeypatch.setenv("PROADS_META_DATASET_ID", "1437834427873532")
    monkeypatch.setenv("PROADS_META_TOKEN", "tok")
    state = app.extensions["paysignal"]
    meta = Meta()
    meta.answer = DEAD
    state.growth_http = meta
    state.billing_sync = True
    c = app.test_client()
    c.post("/auth/signup", json={**ACCT, "ads_consent": True, "fbp": "fb.1.1.1"})
    assert wait_for(lambda: (state.delivery.status_of("signup:glow-clinic") or {}).get("status") == "retrying")
    h = c.get("/delivery/pro", headers={"X-API-Key": "admin-key-0123456789abc"}).json
    assert not h["health"]["healthy"] and "System User token" in h["health"]["alerts"][0]["fix"]
    assert h["reconciliation"]["eligible"] == 1 and h["reconciliation"]["retrying"] == 1
    # the token is replaced; the worker's retry delivers it, exactly once more
    meta.answer = (200, {"events_received": 1})
    state.store.db.execute("UPDATE meta_delivery SET next_try=0")
    state.retry_meta_events()
    assert state.delivery.status_of("signup:glow-clinic")["status"] == "accepted"
    assert meta.sent.count("signup:glow-clinic") == 2
    state.retry_meta_events()
    assert meta.sent.count("signup:glow-clinic") == 2
    h = c.get("/delivery/pro", headers={"X-API-Key": "admin-key-0123456789abc"}).json
    assert h["health"]["healthy"] and h["reconciliation"]["accepted"] == 1


def test_first_payment_reconciled_and_bad_data_not_retried(app, monkeypatch):
    monkeypatch.setenv("PROADS_META_DATASET_ID", "1437834427873532")
    monkeypatch.setenv("PROADS_META_TOKEN", "tok")
    state = app.extensions["paysignal"]
    meta = Meta()
    state.growth_http = meta
    state.billing_sync = True
    c = app.test_client()
    c.post("/auth/signup", json={**ACCT, "ads_consent": True, "fbp": "fb.1.1.1"})
    signed(c, {"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "client_reference_id": "glow-clinic", "customer": "cus_1", "subscription": "sub_1",
        "payment_status": "paid"}}})
    meta.answer = BAD
    signed(c, invoice("in_1"))
    assert wait_for(lambda: (state.delivery.status_of("proads:first:in_1") or {}).get("status") == "rejected")
    state.store.db.execute("UPDATE meta_delivery SET next_try=0")
    n = len(meta.sent)
    state.retry_meta_events()
    assert len(meta.sent) == n                                          # Meta refused the data: fix it, don't spam
    rec = c.get("/delivery/pro", headers={"X-API-Key": "admin-key-0123456789abc"}).json["reconciliation"]
    assert rec["eligible"] == 2 and rec["rejected"] == 1
    assert {r["event"]: r["status"] for r in rec["rows"]}["Purchase"] == "rejected"


def test_alert_email_once_a_day(app, monkeypatch):
    state = app.extensions["paysignal"]
    sent = []
    monkeypatch.setattr("paysignal.mailer.available", lambda: True)
    monkeypatch.setattr("paysignal.mailer.send", lambda to, subj, text: sent.append((to, subj)))
    monkeypatch.setenv("PROADS_ALERT_EMAIL", "me@ezdevsoft.net")
    now = int(time.time())
    state.delivery.queued("proads", ev("x", now - 3600), now - 3600)
    state.delivery.result("x", {"status": DEAD[0], "body": DEAD[1]}, now - 3600)
    assert state.delivery_alert(now)
    assert state.delivery_alert(now + 3600) is None                     # not again within a day
    assert wait_for(lambda: sent == [("me@ezdevsoft.net", "ProAds: events not reaching Meta")])


def test_client_stuck_and_signed_out(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    d = c.get("/delivery/glow-clinic", headers={"X-API-Key": tok}).json
    assert d["healthy"] and d["alerts"][0]["level"] == "info"
