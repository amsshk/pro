import hashlib

from paysignal.server import create_app

H = lambda s: hashlib.sha256(s.encode()).hexdigest()


def test_buyer_list(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Bynawi", "email": "o@bynawi.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    csv_text = ("id,date,amount,currency,email,phone,kind,refund_of\n"
                "o1,2026-11-01,300,AED,Sara@X.ae,050 123 4567,purchase,\n"
                "o2,2026-11-03,200,AED,sara@x.ae,,purchase,\n"            # same person, second order
                "o3,2026-11-04,150,AED,,+971 55 000 1111,purchase,\n"       # phone only
                "o4,2026-11-05,500,AED,gone@x.ae,,purchase,\n"
                "r4,2026-11-06,500,AED,gone@x.ae,,refund,o4\n"            # fully refunded: not a buyer
                "o5,2026-11-07,90,AED,,,purchase,\n")                       # no contact: can't be matched
    assert c.post("/upload/csv/bynawi", data=csv_text, headers=h).status_code == 200
    s = c.get("/audience/bynawi?format=summary", headers=h).json
    assert s["buyers"] == 2 and s["repeat"] == 1 and s["with_email"] == 1 and s["with_phone"] == 2 and not s["meta_ok"]
    meta = c.get("/audience/bynawi?format=meta", headers=h)
    lines = meta.data.decode().strip().splitlines()
    assert lines[0] == "email,phone,value" and lines[1] == f"{H('sara@x.ae')},{H('971501234567')},500.00"
    assert lines[2] == f",{H('971550001111')},150.00" and len(lines) == 3
    assert b"sara" not in meta.data.lower() and b"gone" not in meta.data
    assert c.get("/audience/bynawi?format=tiktok_email", headers=h).data.decode().split() == [H("sara@x.ae")]
    assert c.get("/audience/bynawi?format=tiktok_phone", headers=h).data.decode().split() == [H("+971501234567"), H("+971550001111")]
    assert c.get("/audience/bynawi?format=x", headers=h).status_code == 400
    assert c.get("/audience/bynawi?format=meta").status_code == 401
