import time

from paysignal.server import create_app

ADMIN = {"X-API-Key": "admin-key-0123456789abc"}


def test_partner_program(tmp_path):
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "x.db"), start_worker=False)
    st = app.extensions["paysignal"]
    c = app.test_client()
    assert c.post("/admin/pro/partners", json={"name": "Caleb", "code": "c!"}).status_code == 401
    assert c.post("/admin/pro/partners", json={"name": "Caleb", "code": "c!"}, headers=ADMIN).status_code == 400
    r = c.post("/admin/pro/partners", json={"name": "Caleb", "code": "caleb", "percent": 20, "months": 2}, headers=ADMIN).json
    assert r["partner"]["code"] == "CALEB" and r["partner"]["link"].endswith("/?ref=CALEB")
    dash = r["partner"]["dashboard"].split("/partner/")[1]
    c.post("/t/ref", json={"code": "caleb"})
    c.post("/t/ref", json={"code": "nobody"})
    # sign-up through the link (ref kept with the campaign tags), and one without
    tok = c.post("/auth/signup", json={"business": "Glow", "email": "o@glow.ae", "password": "first-password-1", "terms": True,
                                       "utm": {"utm_source": "ig", "ref": "CALEB"}}).json
    c.post("/auth/signup", json={"business": "Other", "email": "x@o.ae", "password": "first-password-1", "terms": True})
    with st.lock:
        assert st.partners.partner_of("glow") == "CALEB" and st.partners.partner_of("other") is None
        assert "ref" not in (st.accounts.attribution("glow") or {})
        now = int(time.time())
        rows = [("in_1", "glow", "first", 299.0, now - 10 * 86400), ("in_2", "glow", "renewal", 299.0, now),
                ("in_3", "glow", "fee", 100.0, now), ("in_4", "glow", "renewal", 299.0, now + 70 * 86400),   # fee and month 3: not counted
                ("in_5", "other", "first", 799.0, now)]
        for inv, cid, kind, amt, at in rows:
            st.revenue.add({"invoice_id": inv, "client_id": cid, "kind": kind, "tier": "pro", "amount": amt, "tax": 0,
                            "currency": "AED", "paid_at": at, "charge_id": None, "payment_intent": None, "utm": {}})
        st.revenue.set_refunded("in_2", 99.0)
    p = c.get("/admin/pro/partners", headers=ADMIN).json["partners"][0]
    assert p["clicks"] == 1 and p["signups"] == 1 and p["paid_ever"] == 1
    assert p["revenue"] == 499.0 and p["earned"] == 99.8 and p["owed"] == 99.8          # (299 + 200) x 20%
    assert c.post("/admin/pro/partners/CALEB/pay", json={"amount": 50}, headers=ADMIN).json["owed"] == 49.8
    page = c.get(f"/partner/{dash}")
    assert page.status_code == 200 and b"Hi Caleb" in page.data and b"o@glow.ae" not in page.data and b"Glow" not in page.data
    assert c.get("/partner/wrong-token").status_code == 404
