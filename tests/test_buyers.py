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


class R:
    def __init__(self, body, status=200):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


def adset(**kw):
    a = {"optimization_goal": "OFFSITE_CONVERSIONS", "promoted_object": {"custom_event_type": "PURCHASE"},
         "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 7}], "daily_budget": "4000",
         "targeting": {"geo_locations": {"countries": ["AE"]}, "age_min": 25, "publisher_platforms": ["facebook", "instagram"]}}
    a.update(kw)
    return a


class Graph:
    def __init__(self, variant):
        self.variant = variant

    def get(self, url, params, timeout):
        return R({"data": [adset()] if "/c1/" in url else [self.variant]})


def started(app, c, h, variant=None, **body):
    st = app.extensions["paysignal"]
    st.ads_settings = lambda cid, admin: {"token": "t", "ad_account": "act_1"}
    st.ads_http = Graph(variant or adset())
    st.launches.add("glow", {"campaign_id": "c1", "name": "Control", "ad_sets": [], "ads": [], "status": "ACTIVE"})
    st.launches.add("glow", {"campaign_id": "c2", "name": "Serum test", "ad_sets": [], "ads": [], "status": "ACTIVE"})
    eid = c.post("/buyers/glow/experiments", json={"dimension": "product", "segment": "Serum"}, headers=h).json["id"]
    r = c.post(f"/buyers/glow/experiments/{eid}/start", json={"control_campaign": "c1", "variant_campaign": "c2", **body}, headers=h)
    return st, eid, r


def test_experiment_history_and_no_repeat_of_rejected(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    assert c.post("/buyers/glow/experiments", json={"dimension": "product", "segment": "Made up"}, headers=h).status_code == 404
    st, eid, r = started(app, c, h, split_test_id="6012345")
    assert r.json["started"] and all(x["ok"] for x in r.json["checks"])
    assert c.post("/buyers/glow/experiments", json={"dimension": "product", "segment": "Serum"}, headers=h).status_code == 409
    e = c.get("/buyers/glow", headers=h).json["experiments"][0]
    assert e["status"] == "running" and e["result"]["status"] == "collecting" and e["split_test_id"] == "6012345"
    assert c.post(f"/buyers/glow/experiments/{eid}/decide", json={"decision": "reject", "note": "no lift"}, headers=h).json
    d = c.get("/buyers/glow", headers=h).json
    assert ("product", "Serum") not in {(s["dimension"], s["segment"]) for s in d["suggestions"]}
    assert d["experiments"][0]["decision"] == "reject" and d["experiments"][0]["note"] == "no lift"


def test_unfair_test_is_refused_unless_forced(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    unfair = adset(promoted_object={"custom_event_type": "ADD_TO_CART"},
                   targeting={"geo_locations": {"countries": ["SA"]}, "age_min": 25, "publisher_platforms": ["facebook", "instagram"]})
    st, eid, r = started(app, c, h, variant=unfair)
    assert r.status_code == 409 and r.json["can_force"]
    fails = {x["key"] for x in r.json["checks"] if not x["ok"]}
    assert fails == {"event", "audience", "split"}
    r = c.post(f"/buyers/glow/experiments/{eid}/start", json={"control_campaign": "c1", "variant_campaign": "c2", "force": True}, headers=h)
    assert r.json["started"]


def test_evaluate_waits_for_spend_settled_data_and_never_forces_a_verdict():
    t0 = time.time() - 3 * 86400
    arm = lambda spend, vals: {"spend": spend, "values": vals}
    both = lambda c, v: {"control": c, "variant": v}
    small = both(arm(100, [200]), arm(100, [200, 200, 200]))
    assert buyers.evaluate(small, small, 600, t0)["status"] == "collecting"
    win = both(arm(2000, [200] * 6), arm(2000, [220] * 24))
    r = buyers.evaluate(win, win, 600, t0)
    assert r["suggested"] == "keep" and r["settled"]["interval"][0] > 0
    lose = both(arm(2000, [200] * 24), arm(2000, [200] * 6))
    assert buyers.evaluate(lose, lose, 600, t0)["suggested"] == "reject"
    early = buyers.evaluate(win, both(arm(300, [200]), arm(300, [220] * 2)), 600, t0)
    assert early["status"] == "provisional" and early["suggested"] is None
    one_whale = both(arm(2000, [200] * 10), arm(2000, [200] * 9 + [5000]))
    assert buyers.evaluate(one_whale, one_whale, 600, t0)["suggested"] is None
    tie = both(arm(2000, [200] * 10), arm(2000, [200] * 11))
    late = buyers.evaluate(tie, tie, 600, time.time() - 30 * 86400)
    assert late["suggested"] is None and "inconclusive so far" in late["why"]
    refunded = both(arm(2000, [200] * 10), arm(2000, [0.0] * 10))
    assert buyers.evaluate(refunded, refunded, 600, t0)["suggested"] == "reject"


def test_bootstrap_handles_skew():
    lo, hi, p = buyers.boot_diff([100.0] * 30 + [5000.0], [100.0] * 31)
    assert lo <= 0 <= hi


def test_planned_test_is_not_suggested_twice(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    c.post("/buyers/glow/experiments", json={"dimension": "product", "segment": "Serum"}, headers=h)
    d = c.get("/buyers/glow", headers=h).json
    assert ("product", "Serum") not in {(s["dimension"], s["segment"]) for s in d["suggestions"]}
    assert d["experiments"][0]["status"] == "planned"
