import base64
import json

import pytest

from paysignal import meta_ads
from paysignal.ai import MetaAI
from paysignal.assistant import tidy_plan
from paysignal.server import create_app

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 100).decode()
ADMIN = {"X-API-Key": "admin-key-0123456789abc"}


class R:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class FakeGraph:
    def __init__(self, fail_on=None, currency="AED"):
        self.posts, self.deleted, self.n = [], [], 0
        self.fail_on, self.currency = fail_on, currency

    def get(self, url, params, timeout):
        if url.endswith("/insights"):
            self.insights = getattr(self, "insights", []) + [(url, params)]
            return R(200, {"data": [{"spend": "6000.50", "account_currency": self.currency}]})
        return R(200, {"name": "Mine", "currency": self.currency, "account_status": 1})

    def post(self, url, data, timeout):
        self.posts.append((url.split("/v25.0/")[1], data))
        if self.fail_on and self.fail_on in url:
            return R(400, {"error": {"message": "Invalid parameter", "error_user_title": "Bad targeting",
                                     "error_user_msg": "Age range is not allowed."}})
        self.n += 1
        if url.endswith("/adimages"):
            return R(200, {"images": {"bytes": {"hash": f"hash{self.n}"}}})
        return R(200, {"id": str(1000 + self.n)} if not url.split("/")[-1].isdigit() else {"success": True})

    def delete(self, url, params, timeout):
        self.deleted.append(url.split("/")[-1])


def plan():
    p = {"name": "ProAds - UAE sign-ups", "objective": "OUTCOME_SALES", "daily_budget": 40, "currency": "AED",
         "targeting": {"countries": ["ae"], "age_min": 30},
         "ad_sets": [{"name": "UAE broad", "budget_share": 1, "ads": [
             {"headline": "Find buyers, not viewers", "primary_text": "Free audit in week one.", "call_to_action": "sign up"},
             {"headline": "Which posts sell?", "primary_text": "See it from your sales.", "call_to_action": "WHATSAPP_MESSAGE"}]}],
         "organic_plan": [{"day": "Mon", "platform": "instagram", "hook": "x", "caption": "y"}]}
    return tidy_plan(p, "signup")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"PROADS_AD_ACCOUNT_ID": "123", "PROADS_PAGE_ID": "555", "PROADS_ADS_TOKEN": "tok",
                 "PROADS_META_DATASET_ID": "777", "PROADS_PUBLIC_URL": "https://pro.example.net"}.items():
        monkeypatch.setenv(k, v)


def test_tidy_plan_makes_it_publishable():
    p = plan()
    assert p["objective"] == "OUTCOME_LEADS" and p["targeting"] == {"countries": ["AE"], "age_min": 25}
    assert [a["call_to_action"] for a in p["ad_sets"][0]["ads"]] == ["SIGN_UP", "SIGN_UP"]


def test_publish_creates_everything_paused_for_signups():
    g = FakeGraph()
    out = meta_ads.Publisher(g).publish(plan(), {"0-0": PNG, "0-1": PNG}, "https://pro.example.net/", "AED")
    steps = [u.split("/")[-1] for u, _ in g.posts]
    assert steps == ["campaigns", "adsets", "adimages", "adcreatives", "ads", "adimages", "adcreatives", "ads"]
    camp, adset = g.posts[0][1], g.posts[1][1]
    assert camp["status"] == "PAUSED" and camp["objective"] == "OUTCOME_LEADS" and camp["daily_budget"] == "4000"
    assert json.loads(adset["promoted_object"]) == {"pixel_id": "777", "custom_event_type": "COMPLETE_REGISTRATION"}
    assert json.loads(adset["targeting"])["geo_locations"] == {"countries": ["AE"]}
    story = json.loads(g.posts[3][1]["object_story_spec"])
    assert story["page_id"] == "555" and story["link_data"]["call_to_action"]["type"] == "SIGN_UP"
    assert "utm_medium=paid" in story["link_data"]["link"] and "utm_source=meta" in story["link_data"]["link"]
    assert all(d["status"] == "PAUSED" for u, d in g.posts if u.endswith("/ads"))
    assert out["status"] == "PAUSED" and len(out["ads"]) == 2 and "adsmanager" in out["manager_url"]


def test_failure_deletes_the_half_built_campaign_and_explains():
    g = FakeGraph(fail_on="/adsets")
    with pytest.raises(meta_ads.PublishError, match="Age range is not allowed"):
        meta_ads.Publisher(g).publish(plan(), {"0-0": PNG, "0-1": PNG}, "https://pro.example.net/", "AED")
    assert g.deleted == ["1001"]


def test_budget_uses_the_ad_account_currency():
    g = FakeGraph(currency="THB")
    out = meta_ads.Publisher(g).publish(plan(), {"0-0": PNG, "0-1": PNG}, "https://pro.example.net/", "AED")
    assert out["currency"] == "THB" and g.posts[0][1]["daily_budget"] == "4000"
    g = FakeGraph(currency="JPY")
    meta_ads.Publisher(g).publish(plan(), {"0-0": PNG, "0-1": PNG}, "https://pro.example.net/", "AED")
    assert g.posts[0][1]["daily_budget"] == "40"                        # yen has no cents


def test_refuses_before_spending_anything():
    g = FakeGraph()
    with pytest.raises(meta_ads.PublishError, match="has no image"):
        meta_ads.Publisher(FakeGraph()).publish(plan(), {"0-0": PNG}, "https://pro.example.net/", "AED")
    with pytest.raises(meta_ads.PublishError, match="JPEG or PNG"):
        meta_ads.decode_image(base64.b64encode(b"<svg>").decode())
    assert g.posts == []


def test_endpoints_need_a_connection_and_go_live_needs_confirmation(tmp_path):
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}, "other": {"api_key": "other-key-0123456789abc"}},
                     str(tmp_path / "x.db"), start_worker=False)
    state = app.extensions["paysignal"]
    state.ads_http = g = FakeGraph()
    c = app.test_client()
    body = {"plan": plan(), "images": {"0-0": PNG, "0-1": PNG}}
    r = c.post("/campaign/other/publish", json=body, headers={"X-API-Key": "other-key-0123456789abc"})
    assert r.status_code == 409 and "Connect your Facebook" in r.json["error"]          # not connected yet
    r = c.post("/campaign/pro/publish", json=body, headers=ADMIN)
    assert r.status_code == 200 and r.json["status"] == "PAUSED"
    cid = r.json["campaign_id"]
    before = len(g.posts)
    assert c.post(f"/campaign/pro/launch/{cid}", json={"action": "live"}, headers=ADMIN).status_code == 400
    assert len(g.posts) == before                                   # nothing switched on without confirmation
    r = c.post(f"/campaign/pro/launch/{cid}", json={"action": "live", "confirm": True}, headers=ADMIN)
    assert r.status_code == 200 and r.json["status"] == "ACTIVE"
    assert {d["status"] for _, d in g.posts[before:]} == {"ACTIVE"} and len(g.posts) - before == 4
    assert c.get("/campaign/pro/launches", headers=ADMIN).json["launches"][0]["status"] == "ACTIVE"
    assert c.post("/campaign/pro/launch/999", json={"action": "pause"}, headers=ADMIN).status_code == 404


def test_own_campaign_is_planned_for_signups(tmp_path):
    from test_ai import Session, ok
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "x.db"), start_worker=False)
    p = {k: v for k, v in plan().items() if k not in ("conversion",)}
    s = Session(lambda m, b: ok(json.dumps(p)))
    app.extensions["paysignal"].ai = MetaAI(api_key="k", session=s)
    r = app.test_client().post("/campaign/pro", json={"daily_budget": 40}, headers=ADMIN)
    brief = s.calls[0]["body"]["messages"][1]["content"]
    assert '"conversion": "signup"' in brief and "ProAds (pro.ezdevsoft.net)" in brief
    assert r.json["plan"]["objective"] == "OUTCOME_LEADS" and r.json["can_publish"]
    assert r.json["landing_url"] == "https://pro.example.net/"
