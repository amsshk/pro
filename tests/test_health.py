import time
from datetime import datetime, timedelta, timezone

from paysignal.server import create_app

ACCT = {"business": "Glow Clinic", "email": "o@glow.ae", "password": "first-password-1", "terms": True, "phone": "0501234567"}


def app_and_token(tmp_path):
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "h.db"), start_worker=False)
    c = app.test_client()
    return app, c, c.post("/auth/signup", json=ACCT).json["token"]


def test_rejected_tiktok_sales_raise_an_alert(tmp_path):
    app, c, tok = app_and_token(tmp_path)
    db = app.extensions["paysignal"].store.db
    for i in range(6):
        db.execute("INSERT INTO tiktok_delivered (client_id, pixel_code, event_id, sent_at, status, reason) VALUES ('glow-clinic','P',?,?,?,?)",
                   (f"e{i}", int(time.time()), "accepted" if i < 3 else "rejected", None if i < 3 else "bad email"))
    db.commit()
    a = c.get("/alerts/glow-clinic", headers={"X-API-Key": tok}).json["alerts"]
    assert a and "TikTok accepted only 3 of 6" in a[0]["text"]


def test_order_trace_shows_what_happened(tmp_path):
    app, c, tok = app_and_token(tmp_path)
    day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    c.post("/import/glow-clinic", data=f"id,date,amount,currency,email,ttclid\nA1,{day},250,AED,a@b.com,xyz\n", headers={"X-API-Key": tok})
    db = app.extensions["paysignal"].store.db
    db.execute("INSERT INTO tiktok_delivered (client_id, pixel_code, event_id, sent_at, status, reason) VALUES ('glow-clinic','P','A1',?, 'accepted', NULL)",
               (int(time.time()),))
    db.commit()
    o = c.get("/orders-trace/glow-clinic", headers={"X-API-Key": tok}).json["orders"][0]
    assert o["amount"] == 250 and o["tiktok"] == "accepted" and o["from_ad"] == "TikTok ad" and "a@b.com" not in str(o)


def test_setup_guide_fits_tiktok_only(tmp_path):
    app, c, tok = app_and_token(tmp_path)
    labels = [x["label"] for x in c.get("/dashboard/glow-clinic", headers={"X-API-Key": tok}).json["setup"]]
    assert "Send sales to your ad platforms" in labels and "Connect Facebook & Instagram" not in labels


def test_add_offline_sale_goes_to_meta_as_chat_not_tiktok(tmp_path):
    from paysignal import meta_capi, tiktok
    app, c, tok = app_and_token(tmp_path)
    r = c.post("/sales/glow-clinic/add", headers={"X-API-Key": tok}, json={"amount": 485, "where": "whatsapp", "phone": "0501234567"})
    assert r.status_code == 200 and r.json["added"] == 1
    assert c.post("/sales/glow-clinic/add", headers={"X-API-Key": tok}, json={"amount": 485, "where": "whatsapp"}).status_code == 400
    st = app.extensions["paysignal"]
    p = st.store.purchases("glow-clinic")[0]
    assert p.channel == "chat" and meta_capi._action_source(p) == "chat"

    class S:
        def was_sent(self, *a): return False
    pl = tiktok.plan([p], S(), "glow-clinic", lambda p: 0, lambda p: p.amount)
    assert pl["send"] == []


def test_tired_ad_alert(tmp_path):
    from datetime import date, timedelta as td
    app, c, tok = app_and_token(tmp_path)
    db = app.extensions["paysignal"].store.db
    today = date.today()
    for i in range(7):
        d = (today - td(days=i)).isoformat()
        clicks = 20 if i < 3 else 80                       # 1% now vs 4% before
        db.execute("INSERT INTO tiktok_spend (client_id, day, ad_id, ad_name, spend, impressions, clicks) VALUES ('glow-clinic',?,?,?,?,?,?)",
                   (d, "A1", "Shop N6", 50, 2000, clicks))
    db.commit()
    a = c.get("/alerts/glow-clinic", headers={"X-API-Key": tok}).json["alerts"]
    assert any("getting tired" in x["text"] for x in a)
