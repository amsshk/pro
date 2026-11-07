import random
import time
from datetime import date, timedelta

from paysignal import buyers
from paysignal.server import create_app

TODAY = date.today()


def history_csv(n=160, seed=4):
    """Serum buyers come back; FLASH buyers don't. Everyone else is in between."""
    rnd = random.Random(seed)
    rows = ["order id,date,amount,currency,status,promo code,product,email"]
    k = 0
    for c in range(n):
        first = TODAY - timedelta(days=rnd.randint(70, 200))
        product = "Serum" if c % 3 == 0 else ("Cleanser" if c % 3 == 1 else "Mask")
        code = "FLASH" if c % 4 == 0 else ""
        k += 1
        rows.append(f"o{k},{first},{200 if code else 260},AED,paid,{code},{product},c{c}@x.ae")
        repeats = 3 if product == "Serum" and not code else (0 if code else 1)
        for r in range(repeats):
            k += 1
            rows.append(f"o{k},{first + timedelta(days=10 + r * 12)},250,AED,paid,,{product},c{c}@x.ae")
    return "\n".join(rows) + "\n"


def app_with_sales(tmp_path, csv):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Glow", "email": "o@glow.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    c.post("/import/glow", data=csv, headers=h)
    return app, c, h


def test_needs_evidence(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv(n=20))
    d = c.get("/buyers/glow", headers=h).json
    assert d["analysis"]["ready"] is False and "customers" in d["analysis"]["needs"] and d["suggestions"] == []


def test_finds_better_buyers_from_facts_and_suggests_a_test(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    d = c.get("/buyers/glow", headers=h).json
    an = d["analysis"]
    assert an["ready"] and an["window_days"] == 60 and "net of refunds" in an["value_basis"]
    dims = {x["key"]: {s["name"]: s for s in x["segments"]} for x in an["dimensions"]}
    assert dims["product"]["Serum"]["verdict"] == "better" and dims["offer"]["Code FLASH"]["verdict"] == "worse"
    assert dims["offer"]["Full price"]["interval"][0] > 0                        # 95% interval clear of zero
    hyp = {(s["dimension"], s["segment"]) for s in d["suggestions"]}
    assert ("product", "Serum") in hyp and ("offer", "Full price") in hyp
    assert all(s["evidence"]["customers"] >= buyers.MIN_CUSTOMERS for s in d["suggestions"])


def test_experiment_history_and_no_repeat_of_rejected(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    st = app.extensions["paysignal"]
    assert c.post("/buyers/glow/experiments", json={"dimension": "product", "segment": "Made up"}, headers=h).status_code == 404
    eid = c.post("/buyers/glow/experiments", json={"dimension": "product", "segment": "Serum"}, headers=h).json["id"]
    assert c.post("/buyers/glow/experiments", json={"dimension": "product", "segment": "Serum"}, headers=h).status_code == 409
    st.launches.add("glow", {"campaign_id": "c1", "name": "Control", "ad_sets": [], "ads": [], "status": "ACTIVE"})
    st.launches.add("glow", {"campaign_id": "c2", "name": "Serum test", "ad_sets": [], "ads": [], "status": "ACTIVE"})
    assert c.post(f"/buyers/glow/experiments/{eid}/start", json={"control_campaign": "c1", "variant_campaign": "c1"}, headers=h).status_code == 400
    assert c.post(f"/buyers/glow/experiments/{eid}/start", json={"control_campaign": "c1", "variant_campaign": "c2"}, headers=h).json["started"]
    e = c.get("/buyers/glow", headers=h).json["experiments"][0]
    assert e["status"] == "running" and e["result"]["status"] == "running"
    assert c.post(f"/buyers/glow/experiments/{eid}/decide", json={"decision": "reject", "note": "no lift"}, headers=h).json
    d = c.get("/buyers/glow", headers=h).json
    assert ("product", "Serum") not in {(s["dimension"], s["segment"]) for s in d["suggestions"]}
    assert d["experiments"][0]["decision"] == "reject" and d["experiments"][0]["note"] == "no lift"


def test_evaluate_needs_spend_then_decides():
    t0 = time.time() - 3 * 86400
    arm = lambda s, k, v: {"spend": s, "purchases": k, "value": v}
    assert buyers.evaluate({"control": arm(100, 1, 200), "variant": arm(100, 3, 600)}, 600, t0)["status"] == "running"
    win = buyers.evaluate({"control": arm(1000, 4, 800), "variant": arm(1000, 16, 3600)}, 600, t0)
    assert win["suggested"] == "keep" and win["confidence_variant_better"] >= 0.9
    lose = buyers.evaluate({"control": arm(1000, 16, 3600), "variant": arm(1000, 4, 800)}, 600, t0)
    assert lose["suggested"] == "reject"
    tie = buyers.evaluate({"control": arm(1000, 8, 1600), "variant": arm(1000, 9, 1700)}, 600, t0)
    assert tie["status"] == "running"
    late = buyers.evaluate({"control": arm(1000, 8, 1600), "variant": arm(1000, 9, 1700)}, 600, time.time() - 15 * 86400)
    assert late["suggested"] == "inconclusive"


def test_planned_test_is_not_suggested_twice(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    c.post("/buyers/glow/experiments", json={"dimension": "product", "segment": "Serum"}, headers=h)
    d = c.get("/buyers/glow", headers=h).json
    assert ("product", "Serum") not in {(s["dimension"], s["segment"]) for s in d["suggestions"]}
    assert d["experiments"][0]["status"] == "planned"
