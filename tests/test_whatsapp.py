import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from paysignal.server import create_app

APP_SECRET = "meta-app-secret"
KEY = "wa-test-key-0123456789"
H = {"X-API-Key": KEY}
WABA = "102290129340398"
NOW = datetime.now(timezone.utc)


def clients(live=False):
    return {"clinic": {"api_key": KEY, "currency": "AED",
                       "whatsapp": {"waba_id": WABA, "dataset_id": "DS1", "access_token": "tok", "live": live}}}


def message(wa_id, at, referral=None, waba=WABA):
    m = {"from": wa_id, "id": f"wamid.{wa_id}.{int(at.timestamp())}", "timestamp": str(int(at.timestamp())),
         "type": "text", "text": {"body": "Hi, how much is the facial?"}}
    if referral:
        m["referral"] = referral
    return {"object": "whatsapp_business_account", "entry": [{"id": waba, "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "971500000000", "phone_number_id": "PN1"},
        "contacts": [{"profile": {"name": "Sara"}, "wa_id": wa_id}], "messages": [m]}}]}]}


AD = {"source_url": "https://fb.me/abc", "source_id": "120210000000001", "source_type": "ad",
      "headline": "Glow facial, book on WhatsApp", "body": "...", "media_type": "image", "ctwa_clid": "ARAkLkA8rmlFeiCktEJQ-QTwRiyYHAFDLMNDBH0CD3qpjd0HR4irJ6LEkR7JwFF4XvnO2E4Nx0-eM-GABDLOPaOdRMv-_zfUQ2a"}


class Resp:
    def __init__(self, status=200):
        self.status_code = status


class FakeMeta:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def post(self, url, params=None, json=None, timeout=None):
        self.calls.append({"url": url, "params": params, "json": json})
        return Resp(self.status)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("PROADS_WHATSAPP_VERIFY_TOKEN", "verify-me-please")


def make(tmp_path, live=False):
    app = create_app(clients(live), str(tmp_path / "w.db"), start_worker=False)
    st = app.extensions["paysignal"]
    st.wa_http, st.wa_sync = FakeMeta(), True
    return app, app.test_client()


def hook(c, payload, secret=APP_SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return c.post("/webhooks/whatsapp", data=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def sales_csv(rows):
    return "id,date,amount,currency,phone\n" + "".join(f"{i},{d},{a},AED,{ph}\n" for i, d, a, ph in rows)


def test_verify_handshake(tmp_path, env):
    _, c = make(tmp_path)
    ok = c.get("/webhooks/whatsapp", query_string={"hub.mode": "subscribe", "hub.verify_token": "verify-me-please",
                                                   "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.data == b"1158201444"
    bad = c.get("/webhooks/whatsapp", query_string={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert bad.status_code == 403


def test_rejects_unsigned_events(tmp_path, env):
    _, c = make(tmp_path)
    assert hook(c, message("971501234567", NOW, AD), secret="wrong").status_code == 401


def test_message_text_and_names_are_not_stored(tmp_path, env):
    app, c = make(tmp_path)
    assert hook(c, message("971501234567", NOW, AD)).json["received"] == 1
    dump = "\n".join(app.extensions["paysignal"].store.db.iterdump())
    assert "how much" not in dump and "Sara" not in dump and "971501234567" not in dump


def test_sale_matched_to_ad_and_reported(tmp_path, env):
    app, c = make(tmp_path, live=True)
    hook(c, message("971501234567", NOW - timedelta(days=2), AD))
    hook(c, message("971509999999", NOW - timedelta(days=2)))                 # chat not from an ad
    hook(c, message("971501234567", NOW - timedelta(days=1)))                 # same customer, later message
    paid = (NOW - timedelta(hours=3)).isoformat()
    r = c.post("/upload/csv/clinic", data=sales_csv([("s1", paid, 450, "050 123 4567"),
                                                     ("s2", paid, 300, "050 999 9999"),
                                                     ("s3", paid, 200, "")]), headers=H).json
    assert r["new"] == 3
    rep = c.get("/whatsapp/clinic", headers=H).json
    ad = next(s for s in rep["sources"] if s["source_id"] == AD["source_id"])
    assert ad["chats"] == 1 and ad["buyers"] == 1 and ad["orders"] == 1 and ad["revenue"] == 450.0
    assert ad["headline"] == AD["headline"] and ad["chat_to_buyer"] == 1.0
    assert rep["totals"] == {"chats": 2, "from_ads": 1, "buyers": 1, "real_value": ad["real_value"]}

    calls = app.extensions["paysignal"].wa_http.calls
    assert len(calls) == 1                                                    # only the ad chat's sale
    ev = calls[0]["json"]["data"][0]
    assert calls[0]["url"].endswith("/DS1/events") and calls[0]["params"]["access_token"] == "tok"
    assert ev["action_source"] == "business_messaging" and ev["messaging_channel"] == "whatsapp"
    assert ev["user_data"] == {"whatsapp_business_account_id": WABA, "ctwa_clid": AD["ctwa_clid"]}
    assert ev["event_name"] == "Purchase" and ev["custom_data"]["currency"] == "AED" and ev["custom_data"]["gross_value"] == 450.0
    assert rep["recent_sales_reported"][0]["status"] == "sent"

    # same sales uploaded again: nothing counted or sent twice
    c.post("/upload/csv/clinic", data=sales_csv([("s1", paid, 450, "050 123 4567")]), headers=H)
    assert len(calls) == 1

    # the sale also shows in the normal content report, credited to the ad
    content = c.get("/report/clinic", headers=H).json["content"]
    assert any(row["content"] == "wa-ad:" + AD["source_id"] and row["kind"] == "paid" for row in content)


def test_practice_mode_sends_nothing(tmp_path, env):
    app, c = make(tmp_path, live=False)
    hook(c, message("971501234567", NOW - timedelta(days=1), AD))
    c.post("/upload/csv/clinic", data=sales_csv([("s1", NOW.isoformat(), 450, "+971 50 123 4567")]), headers=H)
    assert app.extensions["paysignal"].wa_http.calls == []
    assert c.get("/whatsapp/clinic", headers=H).json["recent_sales_reported"][0]["status"] == "practice mode"


def test_time_window(tmp_path, env):
    app, c = make(tmp_path, live=True)
    hook(c, message("971501234567", NOW - timedelta(days=40), AD))
    c.post("/upload/csv/clinic", data=sales_csv([("old", NOW.isoformat(), 450, "0501234567")]), headers=H)
    assert c.get("/whatsapp/clinic", headers=H).json["totals"]["buyers"] == 0     # chat too long before the sale
    hook(c, message("971502222222", NOW, AD))
    c.post("/upload/csv/clinic", data=sales_csv([("before", (NOW - timedelta(days=5)).isoformat(), 90, "0502222222")]), headers=H)
    assert c.get("/whatsapp/clinic", headers=H).json["totals"]["buyers"] == 0     # sale before the chat


def test_other_business_numbers_are_ignored(tmp_path, env):
    app, c = make(tmp_path)
    assert hook(c, message("971501234567", NOW, AD, waba="999")).json["received"] == 0


def test_promo_code_attribution_is_kept(tmp_path, env):
    app, c = make(tmp_path, live=True)
    hook(c, message("971501234567", NOW - timedelta(days=1), AD))
    csv = "id,date,amount,currency,phone,promo_code\nx1," + NOW.isoformat() + ",450,AED,0501234567,SARA-1234\n"
    c.post("/upload/csv/clinic", data=csv, headers=H)
    p = app.extensions["paysignal"].store.purchases("clinic")[0]
    assert p.promo_code == "SARA-1234" and p.utm_content is None             # existing attribution not overwritten
    assert len(app.extensions["paysignal"].wa_http.calls) == 1              # but Meta still learns the ad sold
