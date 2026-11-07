import time
import urllib.parse

from paysignal import engines, scaling
from test_billing import app  # noqa: F401  (fixture)

ECON = engines.economics({"price": 299, "other_costs": 40, "desired_profit": 100})   # break-even 259, target 159
NOW = 1_800_000_000


def camp(**kw):
    c = {"campaign_id": "c1", "name": "Launch", "status": "ACTIVE",
         "budget": {"level": "campaign", "daily": 100.0, "lifetime": False, "adsets": [], "campaign_id": "c1"},
         "learning": ["SUCCESS"], "spend": 1400.0, "sales_settled": 14, "sales_recent": 2, "refund_rate": 0.0,
         "tracked": True, "currency": "AED"}
    c.update(kw)
    return c


def test_confidently_profitable_scales_one_step_within_cap():
    r = scaling.recommend(camp(), ECON, 1000, 100, None, NOW)
    assert r["action"] == "scale_up" and r["new_daily"] == 120.0 and r["rec_id"]
    r = scaling.recommend(camp(), ECON, 110, 100, None, NOW)                # only 10 of room left
    assert r["action"] == "scale_up" and r["new_daily"] == 110.0


def test_guards_block_scale_up():
    assert scaling.recommend(camp(), ECON, None, 100, None, NOW)["blockers"]                 # no maximum chosen
    assert scaling.recommend(camp(learning=["LEARNING"]), ECON, 1000, 100, None, NOW)["action"] == "hold"
    assert scaling.recommend(camp(learning=["LEARNING_LIMITED"]), ECON, 1000, 100, None, NOW)["action"] == "scale_up"
    recent = {"action": "scale_up", "at": NOW - 3 * 86400, "before_daily": 80.0}
    assert scaling.recommend(camp(), ECON, 1000, 100, recent, NOW)["action"] == "hold"        # cooling down
    assert scaling.recommend(camp(tracked=False), ECON, 1000, 100, None, NOW)["blockers"]      # no verified sales
    assert scaling.recommend(camp(), None, 1000, 100, None, NOW)["blockers"]                   # no economics
    life = camp(budget={"level": "campaign", "daily": 100.0, "lifetime": True, "adsets": [], "campaign_id": "c1"})
    assert scaling.recommend(life, ECON, 1000, 100, None, NOW)["action"] == "hold"
    assert scaling.recommend(camp(spend=300.0, sales_settled=2, sales_recent=0), ECON, 1000, 100, None, NOW)["action"] == "hold"
    # missing spend data must never look like "free sales"
    r = scaling.recommend(camp(spend=0.0), ECON, 1000, 100, None, NOW)
    assert r["action"] == "hold" and "no recorded spend" in r["blockers"][0]
    assert scaling.recommend(camp(spend=200.0, sales_settled=9), ECON, 1000, 100, None, NOW)["action"] == "hold"


def test_refund_rate_discounts_recent_sales():
    # 16 sales on 1,600 is profitable; but if 12 are recent and 60% usually get refunded, it's 8.8: over target
    assert scaling.recommend(camp(spend=1600.0, sales_settled=4, sales_recent=12), ECON, 1000, 100, None, NOW)["action"] == "scale_up"
    r = scaling.recommend(camp(spend=1600.0, sales_settled=4, sales_recent=12, refund_rate=0.6), ECON, 1000, 100, None, NOW)
    assert r["action"] == "hold" and r["verdict"]["verified_sales"] == 8.8


def test_losing_pauses_or_reduces_and_bad_scale_up_is_undone():
    assert scaling.recommend(camp(spend=1400.0, sales_settled=1, sales_recent=0), ECON, 1000, 100, None, NOW)["action"] == "pause"
    r = scaling.recommend(camp(spend=1400.0, sales_settled=4, sales_recent=0), ECON, 1000, 100, None, NOW)
    assert r["action"] == "scale_down" and r["new_daily"] == 80.0
    last = {"action": "scale_up", "at": NOW - 8 * 86400, "before_daily": 80.0}
    r = scaling.recommend(camp(after_change={"spend": 900.0, "sales": 0}), ECON, 1000, 100, last, NOW)
    assert r["action"] == "undo" and r["new_daily"] == 80.0


def test_split_budget_minor_units():
    b = {"level": "adset", "daily": 100.0, "campaign_id": "c1", "adsets": [{"id": "a1", "daily": 60.0}, {"id": "a2", "daily": 40.0}]}
    assert scaling.split_budget(b, 120.0, "THB") == [("a1", 7200), ("a2", 4800)]
    assert scaling.split_budget({**b, "level": "campaign"}, 120.0, "JPY") == [("c1", 120)]


class R:
    def __init__(self, body, status=200):
        self.b, self.status_code, self.content = body, status, b"x"

    def json(self):
        return self.b


class FakeMeta:
    def __init__(self):
        self.daily = {"c1": 10000}            # minor units: 100.00 THB
        self.status = "ACTIVE"
        self.posts = []

    def get(self, url, params=None, timeout=None):
        obj = url.rstrip("/").split("/")[-1]
        f = params.get("fields", "")
        if f == "status":
            return R({"status": self.status})
        return R({"status": self.status, "effective_status": self.status, "daily_budget": str(self.daily[obj]),
                  "adsets": {"data": [{"id": "s1", "effective_status": "ACTIVE", "learning_stage_info": {"status": "SUCCESS"}}]}})

    def post(self, url, data=None, timeout=None):
        obj = url.rstrip("/").split("/")[-1]
        self.posts.append((obj, dict(data)))
        if "daily_budget" in data:
            self.daily[obj] = int(data["daily_budget"])
        if "status" in data:
            self.status = data["status"]
        return R({"success": True})


def test_approve_apply_verify_and_undo(app):
    state = app.extensions["paysignal"]
    meta = FakeMeta()
    state.ads_http = meta
    state.ads_settings = lambda cid, admin: {"token": "t", "ad_account": "act_1", "page_id": "p", "pixel_id": "x",
                                             "instagram_id": "", "version": "v25.0"}
    c = app.test_client()
    H = {"X-API-Key": "admin-key-0123456789abc"}
    state.economics.save("pro", {"price": 299, "other_costs": 40, "desired_profit": 100})
    state.config("pro")["currency"] = "THB"
    state.launches.add("pro", {"campaign_id": "c1", "name": "Launch THB", "utm_slug": "launch-thb", "status": "ACTIVE"})
    today = time.strftime("%Y-%m-%d")
    state.ledger.record("pro", "c1", "act_1", [{"day": today, "spend": 1400.0, "currency": "THB"}])
    old = int(time.time()) - 20 * 86400
    for i in range(16):
        state.revenue.add({"invoice_id": f"in{i}", "client_id": f"x{i}", "kind": "first", "amount": 299.0, "currency": "THB",
                           "paid_at": int(time.time()) - (13 if i < 14 else 1) * 86400,
                           "utm": {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "launch-thb"}})
    d = c.get("/scaling/pro", headers=H).json
    rec = d["recommendations"][0]
    assert rec["action"] == "hold" and rec["blockers"] and rec["would"]["new_daily"] == 120.0     # no maximum yet
    assert meta.posts == []
    c.post("/scaling/pro/settings", headers=H, json={"max_daily": 500})
    rec = c.get("/scaling/pro", headers=H).json["recommendations"][0]
    assert rec["action"] == "scale_up" and rec["new_daily"] == 120.0
    assert c.post("/scaling/pro/apply", headers=H, json={"campaign_id": "c1", "rec_id": rec["rec_id"]}).status_code == 400
    assert c.post("/scaling/pro/apply", headers=H, json={"campaign_id": "c1", "rec_id": "stale", "confirm": True}).status_code == 409
    assert meta.posts == []                                        # nothing changed in Meta without a fresh, confirmed approval
    r = c.post("/scaling/pro/apply", headers=H, json={"campaign_id": "c1", "rec_id": rec["rec_id"], "confirm": True}).json
    assert r["ok"] and meta.daily["c1"] == 12000 and meta.posts == [("c1", {"daily_budget": 12000, "access_token": "t"})]
    assert not any("bid" in k for _, d_ in meta.posts for k in d_)
    after = c.get("/scaling/pro", headers=H).json
    assert after["recommendations"][0]["action"] == "hold"        # 7-day cool-down
    assert after["history"][0]["action"] == "scale_up" and after["history"][0]["verified"] == 1
    u = c.post(f"/scaling/pro/undo/{after['history'][0]['id']}", headers=H, json={"confirm": True}).json
    assert u["ok"] and meta.daily["c1"] == 10000
    assert c.post(f"/scaling/pro/undo/{after['history'][0]['id']}", headers=H, json={"confirm": True}).status_code == 404
