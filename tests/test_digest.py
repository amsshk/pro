"""Morning summary email."""
from datetime import date, datetime, timezone
from types import SimpleNamespace as P

from paysignal import digest
from paysignal.server import create_app


def pay(d, amount):
    return P(occurred_at=datetime.fromisoformat(d).replace(tzinfo=timezone.utc), amount=amount, external_id=d + str(amount))


def test_numbers_and_email():
    ps = [pay("2026-11-17T10:00:00", 300), pay("2026-11-17T21:30:00", 200),   # 21:30 UTC = 01:30 on the 18th in Dubai
          pay("2026-11-10T09:00:00", 250), pay("2026-11-05T09:00:00", 100)]
    d = digest.build(ps, lambda p: 0, date(2026, 11, 18), "Asia/Dubai")
    assert d["day"] == "2026-11-17" and d["orders"] == 1 and d["money"] == 300 and d["vs_last_week"] == 0.2
    assert d["week_money"] == 300 and d["week_change"] == -0.1429
    subj, text, html = digest.render("Bynawi", "AED", d, 150, {"checkout": 2, "winback": 0, "review": 1}, {"title": "Refunds on one product"}, "https://x/app")
    assert subj == "Bynawi: 300 AED really paid yesterday (+20%)"
    assert "2.00 back per 1 spent" in text and "2 unpaid checkouts, 1 customers to ask for a review" in text and "Refunds on one product" in text
    assert "<script" not in html and "300 AED" in html


def test_refunds_count_and_store(tmp_path):
    d = digest.build([pay("2026-11-17T10:00:00", 300)], lambda p: 300, date(2026, 11, 18))
    assert d["money"] == 0 and d["orders"] == 0
    import sqlite3
    s = digest.DigestStore(sqlite3.connect(":memory:"))
    assert s.get("a") == {"enabled": True, "last_sent": None}
    assert s.set("a", enabled=False)["enabled"] is False and s.set("a", last_sent="2026-11-18")["last_sent"] == "2026-11-18"


def test_endpoints(tmp_path, monkeypatch):
    key = "dg-key-0123456789abcdef"
    sent = []
    monkeypatch.setenv("SMTP_HOST", "smtp.x"); monkeypatch.setenv("SMTP_FROM", "ProAds <a@x>")
    app = create_app({"shop": {"api_key": key, "name": "Shop", "currency": "AED", "email": "owner@shop.ae"}}, str(tmp_path / "d.db"), start_worker=False)
    c = app.test_client()
    H = {"X-API-Key": key}
    today = digest.local_day("Asia/Dubai")[0]
    c.post("/upload/csv/shop", data=f"id,date,amount,currency\n1,{today.isoformat()},100,AED\n2,{(today.replace(day=1)).isoformat()},50,AED\n".encode(), headers=H)
    assert c.get("/digest/shop", headers=H).json["enabled"] is True
    assert c.post("/digest/shop", json={"enabled": False}, headers=H).json["enabled"] is False
    assert c.post("/digest/shop", json={"enabled": "no"}, headers=H).status_code == 400
    c.post("/digest/shop", json={"enabled": True}, headers=H)
    from paysignal import mailer
    monkeypatch.setattr(mailer, "send", lambda to, subj, text, html=None: sent.append((to, subj)) or True)
    st = next(cl.cell_contents for cl in app.view_functions["digest_view"].__closure__ if type(cl.cell_contents).__name__ == "App")
    monkeypatch.setattr(digest, "local_day", lambda tz, now=None: (today, 9))
    st.digest_all()
    st.digest_all()                                   # once a day only
    assert len(sent) <= 1 and (not sent or sent[0][0] == "owner@shop.ae")
    assert st.digests.get("shop")["last_sent"] == today.isoformat()
