import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from paysignal import tiktok
from paysignal.models import LineItem, Payment
from paysignal.server import create_app

KEY = "tt-test-key-0123456789"
H = {"X-API-Key": KEY}
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()


class R:
    def __init__(self, body, status=200):
        self.status_code, self._b, self.content = status, body, b"x"

    def json(self):
        return self._b


class Http:
    def __init__(self, reply=None):
        self.calls, self.reply = [], reply or R({"code": 0, "message": "OK"})

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append((url, json, headers))
        return self.reply


def pay(i, days=0, **kw):
    return Payment(client_id="shop", source="stripe", external_id=f"ch_{i}", kind="purchase", amount=120.0, currency="AED",
                   occurred_at=datetime.now(timezone.utc) - timedelta(days=days), **kw)


def test_event_is_hashed_and_has_tiktok_shape():
    ev = tiktok.purchase_event(pay(1, email=" Sara@Mail.com ", phone="0501234567", ttclid="E.C.abc", ttp="tp1",
                                   items=[LineItem("SKU1", "Serum", 2, 60.0)]))
    assert ev["event"] == "CompletePayment" and ev["event_id"] == "shop:stripe:ch_1"
    assert ev["user"]["email"] == sha("sara@mail.com") and ev["user"]["phone"] == sha("+971501234567")
    assert ev["user"]["ttclid"] == "E.C.abc" and ev["user"]["ttp"] == "tp1"
    assert ev["properties"]["value"] == 120.0 and ev["properties"]["currency"] == "AED"
    assert ev["properties"]["contents"][0]["content_id"] == "SKU1"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_SECRET_KEY", "k" * 44)
    a = create_app({"shop": {"api_key": KEY, "name": "Shop", "currency": "AED"}}, str(tmp_path / "t.db"), start_worker=False)
    return a


def state_of(app):
    for f in app.view_functions.values():
        for cell in (getattr(f, "__closure__", None) or ()):
            v = cell.cell_contents
            if type(v).__name__ == "App":
                return v


def test_connect_sends_recent_sales_once_and_skips_old(app):
    st = state_of(app)
    st.tiktok_http = http = Http()
    st.store.add(pay(1, email="a@b.co"))
    st.store.add(pay(2, days=10, email="old@b.co"))          # too old for TikTok
    st.store.add(pay(3))                                       # no way to match: skipped
    c = app.test_client()
    r = c.post("/connect/shop/tiktok", json={"pixel_code": "c4abc123def456", "access_token": "x" * 40, "test_event_code": "TEST1"}, headers=H)
    assert r.status_code == 200 and r.json["connected"] and r.json["sent"] == 1 and r.json["test_mode"]
    url, body, headers = http.calls[0]
    assert url == tiktok.URL and headers["Access-Token"] == "x" * 40
    assert body["event_source_id"] == "C4ABC123DEF456" and body["test_event_code"] == "TEST1" and len(body["data"]) == 1
    st.tiktok_push("shop")                                     # nothing new: nothing re-sent
    assert len(http.calls) == 1
    s = c.get("/status/shop", headers=H).json
    assert s["connections"]["tiktok"]["sent"] == 1 and "token" not in str(s["connections"]["tiktok"]).lower().replace("tiktok", "")


def test_refused_token_is_explained_and_retried(app):
    st = state_of(app)
    st.tiktok_http = Http(R({"code": 40001, "message": "Access token is invalid"}))
    st.store.add(pay(1, email="a@b.co"))
    r = app.test_client().post("/connect/shop/tiktok", json={"pixel_code": "C4ABC123DEF456", "access_token": "y" * 40}, headers=H)
    assert not r.json["ok"] and r.json["status"] == "error" and "access token" in r.json["message"]
    st.tiktok_http = Http()
    assert st.tiktok_push("shop")["sent"] == 1                  # not marked sent before: goes out once fixed


def test_bad_pixel_rejected(app):
    r = app.test_client().post("/connect/shop/tiktok", json={"pixel_code": "nope!", "access_token": "x" * 40}, headers=H)
    assert r.status_code == 400


def test_proads_signup_goes_to_tiktok_with_click_id(monkeypatch):
    from paysignal import growth
    ev = growth.build_event("CompleteRegistration", {"client_id": "acme", "email": "Owner@Acme.ae", "phone": "+971501234567"},
                            {"ttclid": "CLICK", "ttp": "TTP", "ip": "1.2.3.4", "ua": "UA", "url": "https://pro.ezdevsoft.net/"}, "signup:acme")
    tt = ev["_tiktok"]
    assert tt["event"] == "CompleteRegistration" and tt["event_id"] == "signup:acme"
    assert tt["user"]["ttclid"] == "CLICK" and tt["user"]["email"] == sha("owner@acme.ae") and tt["user"]["phone"] == sha("+971501234567")
