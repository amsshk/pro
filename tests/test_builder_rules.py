from paysignal import builder_rules as br
from tests.test_tiktok_api import H, PLAN, setup_publish, app  # noqa: F401


def camp(cid="C1", status="ACTIVE", countries=("AE",), budget=150, at=1, product="N6", offer="none"):
    return {"campaign_id": cid, "name": "Bynawi " + cid, "status": status, "countries": list(countries), "daily_budget": budget, "created_at": at,
            "product_focus": product, "offer": offer}


def test_cap_by_budget():
    assert [br.campaign_cap(b) for b in (50, 75, 100, 150, 200, 300, 400, 1000)] == [1, 1, 2, 2, 3, 3, 4, 4]


def test_default_is_consolidation():
    assert br.decide(["AE"], 150, [], "N6")["action"] == "create_new_campaign"            # nothing to reuse yet
    d = br.decide(["AE"], 100, [camp()], "N6")
    assert d["action"] == "create_adgroup_in_existing_campaign" and d["campaign"]["campaign_id"] == "C1"
    assert br.decide(["AE"], 100, [camp(status="DELETED")], "N6")["action"] == "create_new_campaign"
    assert br.decide(["AE"], 100, [camp()], "N6", force_new=True)["action"] == "create_new_campaign"


def test_other_market_gets_a_campaign_only_while_budget_allows():
    d = br.decide(["SA"], 50, [camp(budget=20)], "N6")                 # total 70 -> cap 1, one running: consolidate
    assert d["action"] == "create_adgroup_in_existing_campaign" and "consolidating" in d["reason"]
    d = br.decide(["SA"], 200, [camp(budget=150)], "N6")               # total 350 -> cap 4: a second market may have its own
    assert d["action"] == "create_new_campaign" and "market" in d["reason"]


def test_publish_into_existing_campaign_keeps_it_and_waits_for_go_live(app):  # noqa: F811
    c, st, ids = setup_publish(app)
    body = {"plan": PLAN, "media": {"0-0": f"media:{ids[0]}", "0-1": f"media:{ids[1]}"}, "landing_url": "bynawi.com",
            "pixel_id": "777", "event": "add_to_cart"}
    first = c.post("/campaign/shop/publish-tiktok", json=body, headers=H).json
    assert c.post("/campaign/shop/tiktok-launch/C1", json={"action": "live", "confirm": True}, headers=H).json["status"] == "ACTIVE"
    n_before = sum(1 for _, p, _, _ in st.tiktok_api_http.calls if p == "campaign/create")
    r = c.post("/campaign/shop/publish-tiktok", json={**body, "existing_campaign_id": "C1"}, headers=H)
    assert r.status_code == 200, r.json
    assert sum(1 for _, p, _, _ in st.tiktok_api_http.calls if p == "campaign/create") == n_before      # no new campaign
    g = [b for _, p, b, _ in st.tiktok_api_http.calls if p == "adgroup/create"][-1]
    assert g["campaign_id"] == "C1" and g["operation_status"] == "DISABLE"                               # never spends silently
    assert r.json["added_to_existing"] and r.json["status"] == "PAUSED" and r.json["adgroup_ids"] == ["G1"]
    assert len(st.tiktok_launches.get("shop", "C1")["ad_groups"]) == 2
    go = c.post("/campaign/shop/tiktok-launch/C1", json={"action": "live", "confirm": True, "adgroup_ids": ["G1"]}, headers=H)
    assert go.status_code == 200 and go.json["status"] == "ACTIVE"
    assert any(p == "adgroup/status/update" and b["operation_status"] == "ENABLE" for _, p, b, _ in st.tiktok_api_http.calls)
    assert c.post("/campaign/shop/publish-tiktok", json={**body, "existing_campaign_id": "NOPE"}, headers=H).status_code == 400


def test_different_product_or_offer_gets_its_own_campaign():
    d = br.decide(["AE"], 300, [camp(product="N°6 Extrait")], "All perfumes")           # the Bynawi case
    assert d["action"] == "create_new_campaign" and "product is different" in d["reason"]
    d = br.decide(["AE"], 400, [camp(product="N°6 Extrait", budget=100)], "All perfumes")
    assert d["action"] == "create_new_campaign" and "product is different" in d["reason"]
    d = br.decide(["AE"], 400, [camp(product="N6", budget=100)], "N6", "Free delivery")
    assert d["action"] == "create_new_campaign" and "offer is different" in d["reason"]
    legacy = camp(product=None, budget=100)
    d = br.decide(["AE"], 400, [legacy], "N6")
    assert d["action"] == "create_new_campaign" and "no product recorded" in d["reason"]
    assert br.decide(["AE"], 100, [camp(product=" n6 ")], "N6")["action"] == "create_adgroup_in_existing_campaign"   # same wording, any case
