import hashlib
import time

import pytest

from paysignal.server import create_app

ENV_CLIENTS = {"pro": {"api_key": "A7medooh", "name": "ProAds"}}
GOOD = {"business": "Glow Clinic", "email": "Owner@Glow.ae", "password": "long-enough-pass", "phone": "050 123 4567",
        "currency": "AED", "terms": True}
SALES = "id,date,amount,currency\n1,2026-10-02,300,AED\n2,2026-10-03,150,AED\n"


class Resp:
    status_code = 200
    content = b"{}"

    def json(self):
        return {"events_received": 1}


class FakeMeta:
    def __init__(self):
        self.calls = []

    def post(self, url, params=None, json=None, timeout=None):
        self.calls.append({"url": url, "params": params, "json": json})
        return Resp()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_META_DATASET_ID", "999")
    monkeypatch.setenv("PROADS_META_TOKEN", "meta-token")
    a = create_app(ENV_CLIENTS, str(tmp_path / "a.db"), start_worker=False)
    st = a.extensions["paysignal"]
    st.growth_http, st.growth_sent = FakeMeta(), []
    return a


def sent(app, n, timeout=3):
    st = app.extensions["paysignal"]
    end = time.time() + timeout
    while len(st.growth_sent) < n and time.time() < end:
        time.sleep(0.02)
    return st.growth_sent


def test_pages_are_served(app):
    c = app.test_client()
    assert b"Get your free audit" in c.get("/").data
    assert b"Privacy policy" in c.get("/privacy").data
    assert c.get("/public-config").json["pixel_id"] == "999"


def test_signup_and_use_the_app(app):
    c = app.test_client()
    r = c.post("/auth/signup", json=GOOD)
    assert r.status_code == 200 and r.json["client_id"] == "glow-clinic"
    H = {"X-API-Key": r.json["token"]}
    s = c.get("/status/glow-clinic", headers=H).json
    assert s["name"] == "Glow Clinic" and s["currency"] == "AED"
    assert c.post("/import/glow-clinic", data=SALES, headers=H).json["new"] == 2
    assert c.get("/audit/glow-clinic", headers=H).json["audit"]["orders"] == 2


def test_signup_validation_and_duplicates(app):
    c = app.test_client()
    bad = c.post("/auth/signup", json=dict(GOOD, password="short", terms=False)).json["errors"]
    assert any("10 characters" in e for e in bad) and any("privacy" in e for e in bad)
    assert c.post("/auth/signup", json=GOOD).status_code == 200
    assert c.post("/auth/signup", json=dict(GOOD, email="owner@glow.ae")).status_code == 409
    second = c.post("/auth/signup", json=dict(GOOD, email="other@glow.ae")).json
    assert second["client_id"].startswith("glow-clinic-")            # same name, unique id


def test_password_is_not_stored(app):
    c = app.test_client()
    c.post("/auth/signup", json=GOOD)
    row = app.extensions["paysignal"].store.db.execute("SELECT pw_hash FROM accounts").fetchone()[0]
    assert "long-enough-pass" not in row and row.startswith("scrypt$")
    tok = app.extensions["paysignal"].store.db.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert len(tok) == 64                                               # only a hash of the session token


def test_login_logout(app):
    c = app.test_client()
    c.post("/auth/signup", json=GOOD)
    r = c.post("/auth/login", json={"login": "OWNER@glow.ae", "password": GOOD["password"]})
    assert r.status_code == 200 and r.json["client_id"] == "glow-clinic"
    H = {"X-API-Key": r.json["token"]}
    assert c.get("/status/glow-clinic", headers=H).status_code == 200
    c.post("/auth/logout", headers=H)
    assert c.get("/status/glow-clinic", headers=H).status_code == 401


def test_env_business_can_log_in(app):
    c = app.test_client()
    r = c.post("/auth/login", json={"login": "pro", "password": "A7medooh"})
    assert r.status_code == 200 and r.json["client_id"] == "pro"
    assert c.get("/status/pro", headers={"X-API-Key": r.json["token"]}).status_code == 200


def test_lock_after_five_wrong_passwords(app):
    c = app.test_client()
    c.post("/auth/signup", json=GOOD)
    for _ in range(5):
        assert c.post("/auth/login", json={"login": GOOD["email"], "password": "wrong-password"}).status_code == 401
    r = c.post("/auth/login", json={"login": GOOD["email"], "password": GOOD["password"]})
    assert r.status_code == 429 and "minutes" in r.json["error"]


def test_access_key_guessing_is_locked(app):
    c = app.test_client()
    for _ in range(5):
        assert c.get("/status/pro", headers={"X-API-Key": "guess"}).status_code == 401
    assert c.get("/status/pro", headers={"X-API-Key": "A7medooh"}).status_code == 429


def test_one_account_cannot_read_another(app):
    c = app.test_client()
    a = c.post("/auth/signup", json=GOOD).json
    b = c.post("/auth/signup", json=dict(GOOD, business="Other Shop", email="b@shop.ae")).json
    assert c.get(f"/status/{b['client_id']}", headers={"X-API-Key": a["token"]}).status_code == 401


def test_ad_events_with_consent(app):
    c = app.test_client()
    r = c.post("/auth/signup", json=dict(GOOD, ads_consent=True, fbp="fb.1.1.123", fbclid="CLICK",
                                         source_url="https://pro.ezdevsoft.net/?fbclid=CLICK#signup"),
               headers={"User-Agent": "Test", "X-Forwarded-For": "5.6.7.8"}).json
    ev, res = sent(app, 1)[0]
    assert ev["event_name"] == "CompleteRegistration" and ev["event_id"] == r["event_id"]
    u = ev["user_data"]
    assert u["em"] == [hashlib.sha256(b"owner@glow.ae").hexdigest()]
    assert u["ph"] == [hashlib.sha256(b"971501234567").hexdigest()]
    assert u["fbc"].startswith("fb.1.") and u["fbc"].endswith(".CLICK") and u["fbp"] == "fb.1.1.123"
    assert u["client_ip_address"] == "5.6.7.8" and ev["event_source_url"].endswith("fbclid=CLICK")
    assert "Owner@Glow.ae" not in str(ev) and "owner@glow.ae" not in str(ev)
    call = app.extensions["paysignal"].growth_http.calls[0]
    assert call["url"] == "https://graph.facebook.com/v25.0/999/events" and call["params"]["access_token"] == "meta-token"

    H = {"X-API-Key": r["token"]}
    c.post("/import/glow-clinic", data=SALES, headers=H)
    events = sent(app, 2)
    assert events[1][0]["event_name"] == "StartTrial" and events[1][0]["event_id"] == "activate:glow-clinic"
    c.post("/import/glow-clinic", data=SALES.replace("1,2026", "9,2026"), headers=H)
    time.sleep(0.2)
    assert len(app.extensions["paysignal"].growth_sent) == 2           # activation sent once only


def test_no_ad_events_without_consent(app):
    c = app.test_client()
    r = c.post("/auth/signup", json=dict(GOOD, ads_consent=False, fbclid="CLICK")).json
    c.post("/import/glow-clinic", data=SALES, headers={"X-API-Key": r["token"]})
    time.sleep(0.2)
    assert app.extensions["paysignal"].growth_sent == []
    assert app.extensions["paysignal"].store.db.execute("SELECT ads_ctx FROM accounts").fetchone()[0] is None
