import time
from datetime import datetime, timedelta, timezone

from paysignal import meta_capi, signals
from paysignal.connectors.shopify import parse_shopify_webhook
from paysignal.engine import Engine
from paysignal.identity import _sha256
from paysignal.models import Payment
from paysignal.server import create_app
from paysignal.store import PaymentStore as Store
from test_meta_ads import FakeGraph, R

NOW = datetime.now(timezone.utc)


def order(**kw):
    o = {"id": 77, "total_price": "300.00", "currency": "AED", "processed_at": NOW.isoformat(), "email": "Sara@X.ae",
         "customer": {"id": 9, "first_name": "Sara"}, "browser_ip": "5.6.7.8",
         "client_details": {"user_agent": "Mozilla/5.0"}, "note_attributes": [{"name": "_fbp", "value": "fb.1.1.123"}],
         "billing_address": {"first_name": "Sara", "last_name": "Al Ali", "city": "Abu Dhabi", "zip": "", "country_code": "AE"},
         "line_items": []}
    o.update(kw)
    return o


def test_purchase_carries_every_match_key_hashed():
    p = parse_shopify_webhook("orders/paid", order(), "glow")[0]
    u = meta_capi.user_data_for(p)
    assert u["fn"] == [_sha256("sara")] and u["ln"] == [_sha256("alali")] and u["ct"] == [_sha256("abudhabi")]
    assert u["country"] == [_sha256("ae")] and u["external_id"] == [_sha256("9")] and u["em"] == [_sha256("sara@x.ae")]
    assert u["fbp"] == "fb.1.1.123" and u["client_ip_address"] == "5.6.7.8" and u["client_user_agent"] == "Mozilla/5.0"
    assert "zp" not in u                                              # empty values are never sent


def test_refunds_are_sent_matched_on_the_original_buyer():
    st = Store()
    eng = Engine("glow", st)
    p = parse_shopify_webhook("orders/paid", order(), "glow")[0]
    st.add(p)
    st.add(Payment("glow", "shopify", "refund:1", "refund", 300.0, "AED", NOW, refund_of="77"))
    res = eng.push_to_meta("px", "tok", dry_run=True)
    events = res[0]["payload"]["data"]
    assert [e["event_name"] for e in events] == ["Purchase", "Refund"]
    assert events[1]["user_data"]["em"] == [_sha256("sara@x.ae")] and events[1]["custom_data"]["order_id"] == "77"
    rep = res[-1]
    assert rep["report"] and rep["purchases"] == 1 and rep["refunds"] == 1 and "fn" in rep["keys"][0]


def test_signal_log_summary_and_double_count():
    import sqlite3
    log = signals.SignalLog(sqlite3.connect(":memory:"))
    results = [{"status": 200, "body": {"events_received": 2, "fbtrace_id": "T"}, "count": 2},
               {"report": True, "purchases": 2, "refunds": 0, "no_identifier": 1, "too_old": 0,
                "keys": [["em", "ph", "fn"], ["em"]]}]
    assert log.log("glow", "live", results) == {"sent": 2, "received": 2, "error": None}
    s = log.summary("glow")
    keys = {k["key"]: k["share"] for k in s["match_keys"]}
    assert s["accepted"] == 2 and s["no_identifier"] == 1 and keys["em"] == 1.0 and keys["ph"] == 0.5 and keys["fbp"] == 0
    bad = [{"status": 400, "body": {"error": {"message": "Invalid parameter"}}, "count": 1},
           {"report": True, "purchases": 1, "refunds": 0, "no_identifier": 0, "too_old": 0, "keys": [["em"]]}]
    log.log("glow", "live", bad)
    assert log.summary("glow")["last_error"]["message"] == "Invalid parameter"
    assert log.sent_purchases_since("glow", 0) == 2                   # failed batch not counted as sent
    assert signals.double_count_check(40, 20)["status"] == "warning"
    assert signals.double_count_check(22, 20)["status"] == "ok"
    assert signals.double_count_check(None, 20)["status"] == "unknown"


def test_learning_advice():
    a = lambda **kw: {"effective_status": "ACTIVE", "daily_budget": 40, "learning": "learning", "conversions_week": 5, **kw}
    titles = lambda xs: [x["title"] for x in xs]
    assert "Budget split too thin" in titles(signals.learning_advice([a(), a()], 100, "purchase"))
    assert "Learning limited" in titles(signals.learning_advice([a(learning="limited")], None, "purchase"))
    assert "Recent big edits" in titles(signals.learning_advice([a(last_edit=time.time() - 3600)], None, "purchase"))
    assert "Budget too small to learn on purchases" in titles(signals.learning_advice([a()], 100, "purchase"))
    assert titles(signals.learning_advice([a(daily_budget=1000)], 10, "purchase")) == ["Learning"]


class Graph(FakeGraph):
    def get(self, url, params, timeout):
        if url.endswith("/adsets"):
            return R(200, {"data": [
                {"name": "UAE broad", "effective_status": "ACTIVE", "daily_budget": "4000",
                 "learning_stage_info": {"status": "FAIL", "conversions": 6}},
                {"name": "UAE women", "effective_status": "ACTIVE", "daily_budget": "4000",
                 "learning_stage_info": {"status": "LEARNING", "conversions": 3}}]})
        if url.endswith("/stats"):
            return R(200, {"data": [{"data": [{"value": "Purchase", "count": 30}, {"value": "PageView", "count": 900}]}]})
        return super().get(url, params, timeout)


def test_learning_endpoint_and_instant_send(tmp_path, monkeypatch):
    for k, v in {"PROADS_AD_ACCOUNT_ID": "123", "PROADS_PAGE_ID": "555", "PROADS_ADS_TOKEN": "tok",
                 "PROADS_META_DATASET_ID": "777", "PROADS_PUBLIC_URL": "https://pro.example.net"}.items():
        monkeypatch.setenv(k, v)
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "x.db"), start_worker=False)
    st = app.extensions["paysignal"]
    st.ads_http = Graph()
    st.launches.add("pro", {"campaign_id": "c1", "name": "Sign-ups", "ad_sets": [], "ads": [], "status": "ACTIVE"})
    out = app.test_client().get("/learning/pro", headers={"X-API-Key": "admin-key-0123456789abc"}).json
    camp = out["campaigns"][0]
    assert [a["learning"] for a in camp["adsets"]] == ["limited", "learning"] and camp["adsets"][0]["daily_budget"] == 40
    assert {x["title"] for x in camp["advice"]} >= {"Budget split too thin", "Learning limited"}
    calls = []
    monkeypatch.setattr(st, "sync_meta", lambda cid: calls.append(cid) or {})
    st.instant_meta = True
    st.ingest("pro", [Payment("pro", "stripe", "ch_1", "purchase", 100.0, "AED", NOW, email="a@b.c")])
    st.ingest("pro", [Payment("pro", "stripe", "ch_2", "purchase", 100.0, "AED", NOW, email="a@b.c")])
    time.sleep(0.3)
    assert calls == ["pro"]                                               # sent at once, and not hammered


def test_purchase_value_is_what_was_paid_unless_profit_is_chosen():
    from paysignal.models import LineItem
    st = Store()
    eng = Engine("glow", st)
    paid = Payment("glow", "shopify", "o1", "purchase", 300.0, "AED", NOW, email="a@b.c", event_source_url="https://x",
                   items=[LineItem("s", "Serum", 1, 300.0, 120.0)], fbc="fb.1.1.abc")
    upload = Payment("glow", "upload", "u1", "purchase", 200.0, "AED", NOW, email="b@b.c")
    st.add(paid); st.add(upload)
    ev = {e["custom_data"]["order_id"]: e for e in eng.push_to_meta("px", "tok", dry_run=True)[0]["payload"]["data"]}
    assert ev["o1"]["custom_data"]["value"] == 300.0 and ev["o1"]["custom_data"]["proads_value_basis"] == "amount_paid"
    assert "predicted_return_risk" not in ev["o1"]["custom_data"]                # no predictions to Meta
    assert ev["o1"]["user_data"]["fbc"] == "fb.1.1.abc" and ev["o1"]["action_source"] == "website"
    assert ev["u1"]["action_source"] == "other" and ev["u1"]["custom_data"]["proads_source"] == "upload"
    ev = {e["custom_data"]["order_id"]: e for e in eng.push_to_meta("px", "tok", dry_run=True, value_mode="profit")[0]["payload"]["data"]}
    assert ev["o1"]["custom_data"]["value"] == 180.0 and ev["o1"]["custom_data"]["proads_value_basis"] == "profit"
    assert ev["u1"]["custom_data"]["value"] == 200.0                            # no known cost: never a guess


def test_value_mode_setting(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Glow", "email": "o@glow.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    assert c.get("/learning/glow", headers=h).json["value_mode"] == "amount"
    assert c.post("/learning/glow/settings", json={"value_mode": "ltv"}, headers=h).status_code == 400
    assert c.post("/learning/glow/settings", json={"value_mode": "profit"}, headers=h).json["value_mode"] == "profit"
    assert c.get("/learning/glow", headers=h).json["value_mode"] == "profit"
