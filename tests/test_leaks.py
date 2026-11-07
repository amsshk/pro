from datetime import date, timedelta

from paysignal.server import create_app

TODAY = date.today()


def csv_rows():
    out = ["id,date,amount,currency,status,promo code,product,refunded,email"]
    n = 0
    for i in range(120):
        d = (TODAY - timedelta(days=i % 90)).isoformat()
        n += 1
        if i % 4 == 0:      # discounted orders: smaller and with a code
            out.append(f"o{n},{d},150,AED,paid,FLASH30,Serum,,c{n}@x.ae")
        elif i % 5 == 0:    # a product people send back
            out.append(f"o{n},{d},300,AED,paid,,Laser kit,{300 if i % 10 == 0 else ''},c{n}@x.ae")
        else:
            out.append(f"o{n},{d},300,AED,paid,,Serum,,c{n}@x.ae")
    for k in range(6):
        out.append(f"u{k},{(TODAY - timedelta(days=k * 3)).isoformat()},280,AED,payment failed,,Serum,,u{k}@x.ae")
    return "\n".join(out) + "\n"


def test_leaks_are_found_measured_and_ranked(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Glow", "email": "o@glow.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    r = c.post("/import/glow", data=csv_rows(), headers=h).json
    assert r["unpaid_kept"] == 6
    assert c.post("/import/glow", data=csv_rows(), headers=h).json.get("unpaid_kept", 0) == 0     # no double counting
    c.post("/engines/glow/economics", json={"price": 300, "delivery_cost": 35}, headers=h)
    out = c.get("/leaks/glow", headers=h).json
    kinds = {l["kind"]: l for l in out["leaks"]}
    assert set(kinds) >= {"discount", "refunds", "unpaid", "delivery"}
    assert kinds["discount"]["how"] == "measured" and "FLASH30" in kinds["discount"]["title"]
    assert kinds["refunds"]["title"].endswith("Laser kit") and kinds["unpaid"]["how"] == "up to"
    assert "6 orders worth 1,680" in kinds["unpaid"]["evidence"] and "failed payments" in kinds["unpaid"]["evidence"]
    pm = [l["per_month"] for l in out["leaks"]]
    assert pm == sorted(pm, reverse=True) and out["measured_per_month"] > 0 and out["at_stake_per_month"] > 0
    c.post("/data/glow/clear-sales", json={"confirm": "DELETE"}, headers=h)
    assert c.get("/leaks/glow", headers=h).json["leaks"] == []
