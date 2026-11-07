import json
import time

import pytest

from paysignal import autopilot, buyers
from test_buyers import app_with_sales, history_csv
from test_meta_ads import FakeGraph, R

LINK = "https://glow.ae/?utm_source=meta&utm_medium=paid&utm_campaign=hydra-uae&utm_content=hydra-uae-1-1"


class Meta(FakeGraph):
    def __init__(self):
        super().__init__()
        self.writes = []

    def get(self, url, params, timeout):
        if url.endswith("/adsets"):
            return R(200, {"data": [{"id": "s1", "daily_budget": "4000", "optimization_goal": "OFFSITE_CONVERSIONS",
                                     "promoted_object": {"custom_event_type": "PURCHASE"},
                                     "targeting": {"geo_locations": {"countries": ["AE"]}}}]})
        if url.endswith("/ads"):
            ad = "a9" if "/c9/" in url else "a1"
            return R(200, {"data": [{"id": ad, "name": "Ad 1", "creative": {"id": "cr0", "object_story_spec": {
                "page_id": "p1", "link_data": {"message": "Old text", "name": "Old headline", "link": LINK,
                                               "call_to_action": {"type": "BOOK_NOW", "value": {"link": LINK}}}}}}]})
        return super().get(url, params, timeout)

    def post(self, url, data, timeout):
        path = url.split("/v25.0/")[1]
        self.writes.append((path, data))
        if path.endswith("/copies"):
            return R(200, {"copied_campaign_id": "c9"})
        if path.endswith("/adcreatives"):
            return R(200, {"id": "cr9"})
        if path.endswith("/ad_studies"):
            return R(200, {"id": "st1"})
        return R(200, {"success": True})


def test_sample_size_and_honest_affordability():
    assert autopilot.purchases_needed(0.3) == 202 and autopilot.purchases_needed(1.0) < 40
    ok = autopilot.test_budget(50, 120, 400)
    assert ok["affordable"] and ok["purchases_per_arm"] == 202 and ok["budget"] == 20200 and ok["days"] == 51
    small = autopilot.test_budget(50, 120, 60)
    assert small["affordable"] and small["detectable_uplift"] > 0.3 and "big change" in small["note"]
    no = autopilot.test_budget(200, 100, 20)
    assert no["affordable"] is False and "won't pretend" in no["why"]
    assert autopilot.test_budget(None, None, 40)["affordable"] is None


def test_meta_writes_can_never_touch_budgets():
    mt = autopilot.MetaTest(Meta(), "t", "act_1")
    with pytest.raises(autopilot.MetaTestError, match="never changes budgets"):
        mt.post("c1", {"daily_budget": 9000}, "x")


def test_three_different_proposals_and_next_best(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    c.post("/engines/glow/economics", json={"price": 260, "product_cost": 80, "desired_profit": 40, "daily_budget": 300}, headers=h)
    d = c.get("/autopilot/glow", headers=h).json
    kinds = [p["kind"] for p in d["proposals"]]
    assert len(kinds) == len(set(kinds)) >= 2                                   # genuinely different changes
    p = d["proposals"][0]
    assert p["why"] and p["unchanged"] and "Not a promise" in p["benefit_text"] and "settled" in p["measure"].lower()
    assert d["next_best"]["key"] in {x["key"] for x in d["proposals"]} and d["next_best"]["reasons"]
    assert "Budget (never raised by ProAds)" in p["unchanged"]


def test_full_journey_with_two_approvals_and_clean_controls(tmp_path, monkeypatch):
    app, c, h = app_with_sales(tmp_path, history_csv())
    st = app.extensions["paysignal"]
    c.post("/engines/glow/economics", json={"price": 260, "product_cost": 80, "desired_profit": 40, "daily_budget": 300}, headers=h)
    meta = Meta()
    st.ads_http = meta
    st.ads_settings = lambda cid, admin: {"token": "t", "ad_account": "act_1"}
    st.launches.add("glow", {"campaign_id": "c1", "name": "Hydra UAE", "ad_sets": ["s1"], "ads": ["a1"], "status": "PAUSED"})
    key = c.get("/autopilot/glow", headers=h).json["proposals"][0]["key"]
    assert c.post("/autopilot/glow/prepare", json={"key": key, "control_campaign": "c1"}, headers=h).status_code == 400  # no AI: must write it
    r = c.post("/autopilot/glow/prepare", json={"key": key, "control_campaign": "c1",
                                                 "change": {"message": "New text about Serum"}}, headers=h).json
    assert meta.writes == []                                                    # preparing creates nothing
    assert r["plan"]["preview"] == [{"ad": "Ad 1", "field": "Ad text", "control": "Old text", "variant": "New text about Serum"}]
    eid = r["id"]
    other = c.get("/autopilot/glow", headers=h).json["proposals"]
    if other:
        assert c.post("/autopilot/glow/prepare", json={"key": other[0]["key"], "control_campaign": "c1",
                                                       "change": {"message": "x"}}, headers=h).status_code == 409  # control busy
    assert c.post(f"/autopilot/glow/experiments/{eid}/approve", json={}, headers=h).status_code == 400
    out = c.post(f"/autopilot/glow/experiments/{eid}/approve", json={"confirm": True}, headers=h).json
    assert out["variant_campaign"] == "c9" and out["split_test_id"] == "st1" and out["live"] is False
    paths = [p for p, _ in meta.writes]
    assert paths == ["c1/copies", "act_1/adcreatives", "a9", "act_1/ad_studies"]
    copy = dict(meta.writes[0][1])
    assert copy["status_option"] == "PAUSED" and copy["deep_copy"] == "true"
    spec = json.loads(meta.writes[1][1]["object_story_spec"])["link_data"]
    assert spec["message"] == "New text about Serum" and spec["name"] == "Old headline"       # one change only
    assert f"utm_campaign=hydra-uae-test{eid}" in spec["link"] and spec["call_to_action"]["value"]["link"] == spec["link"]
    cells = json.loads(meta.writes[3][1]["cells"])
    assert [x["campaigns"] for x in cells] == [["c1"], ["c9"]] and all(x["treatment_percentage"] == 50 for x in cells)
    assert not any(k in d for _, d in meta.writes for k in ("daily_budget", "lifetime_budget", "bid_amount"))
    assert {l["campaign_id"]: l["status"] for l in st.launches.list("glow")}["c9"] == "PAUSED"
    assert c.post(f"/autopilot/glow/experiments/{eid}/start", json={"confirm": True}, headers=h).status_code == 409  # control off
    st.launches.set_status("glow", "c1", "ACTIVE")
    n = len(meta.writes)
    assert c.post(f"/autopilot/glow/experiments/{eid}/start", json={"confirm": True}, headers=h).json["status"] == "running"
    assert meta.writes[n:] == [("c9", {"status": "ACTIVE", "access_token": "t"})]           # only the variant switched on
    e = next(x for x in c.get("/autopilot/glow", headers=h).json["experiments"] if x["id"] == eid)
    assert e["status"] == "running" and e["result"]["suggested"] is None
    assert c.post(f"/autopilot/glow/experiments/{eid}/pause-variant", json={"confirm": True}, headers=h).json["variant"] == "PAUSED"


def test_no_confidence_with_too_few_purchases():
    arm = lambda s, v: {"spend": s, "values": v}
    few = {"control": arm(2000, [200] * 3), "variant": arm(2000, [200] * 12)}
    r = buyers.evaluate(few, few, 600, time.time() - 86400)
    assert r["suggested"] is None and "Too few settled purchases" in r["why"] and "interval" not in r["settled"]


def test_owner_can_reject_a_proposal_for_good(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    c.post("/engines/glow/economics", json={"price": 260, "product_cost": 80, "desired_profit": 40, "daily_budget": 300}, headers=h)
    key = c.get("/autopilot/glow", headers=h).json["proposals"][0]["key"]
    assert c.post("/autopilot/glow/dismiss", json={"key": key, "reason": "not our style"}, headers=h).json["dismissed"]
    assert key not in {p["key"] for p in c.get("/autopilot/glow", headers=h).json["proposals"]}


def test_estimates_are_capped_and_never_promised(tmp_path):
    app, c, h = app_with_sales(tmp_path, history_csv())
    c.post("/engines/glow/economics", json={"price": 260, "product_cost": 80, "desired_profit": 40, "daily_budget": 300}, headers=h)
    d = c.get("/autopilot/glow", headers=h).json
    assert all(p["benefit"][1] <= autopilot.BENEFIT_CAP for p in d["proposals"])
    assert d["next_best"]["reasons"][0].startswith("Evidence: ") and "strength" not in d["next_best"]["reasons"][0]
    offer = next((p for p in d["proposals"] if p["kind"] == "offer"), None)
    if offer:
        assert "Ad text and image" not in offer["unchanged"] and "Image" in offer["unchanged"]
