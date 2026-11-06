import json
from datetime import date, datetime, timezone

from paysignal.server import create_app
from test_meta_ads import PNG, FakeGraph, R, plan

ADMIN = {"X-API-Key": "admin-key-0123456789abc"}


class InsightsGraph(FakeGraph):
    def get(self, url, params, timeout):
        if url.endswith("/insights"):
            acts = [{"action_type": "offsite_conversion.fb_pixel_complete_registration", "value": "4"},
                    {"action_type": "complete_registration", "value": "4"}, {"action_type": "link_click", "value": "30"}]
            base = {"spend": "50.5", "impressions": "4000", "reach": "3000", "clicks": "40", "actions": acts,
                    "account_currency": "THB"}
            if params.get("breakdowns") == "country":
                return R(200, {"data": [{**base, "country": "AE"}, {**base, "country": "SA", "actions": acts[:1], "clicks": "10"}]})
            if params.get("breakdowns"):
                return R(200, {"data": [{**base, "publisher_platform": "instagram", "platform_position": "instagram_reels"}]})
            return R(200, {"data": [{**base, "date_start": datetime.now(timezone.utc).date().isoformat()}]})
        return super().get(url, params, timeout)


def test_home_ads_report_with_truth_check(tmp_path, monkeypatch):
    for k, v in {"PROADS_AD_ACCOUNT_ID": "123", "PROADS_PAGE_ID": "555", "PROADS_ADS_TOKEN": "tok",
                 "PROADS_META_DATASET_ID": "777"}.items():
        monkeypatch.setenv(k, v)
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "x.db"), start_worker=False)
    st = app.extensions["paysignal"]
    st.ads_http = InsightsGraph()
    c = app.test_client()
    assert c.get("/ads/pro", headers=ADMIN).json == {"has_campaigns": False}
    c.post("/campaign/pro/publish", json={"plan": plan(), "images": {"0-0": PNG, "0-1": PNG}}, headers=ADMIN)
    # one sign-up that came from a ProAds ad, one that didn't
    c.post("/auth/signup", json={"business": "Glow", "email": "a@x.ae", "password": "first-password-1", "terms": True,
                                 "ads_consent": True, "source_url": "https://pro.example.net/?utm_source=meta&utm_medium=paid"})
    c.post("/auth/signup", json={"business": "Bean", "email": "b@x.ae", "password": "first-password-1", "terms": True})
    r = c.get("/ads/pro?period=7", headers=ADMIN).json
    assert r["kind"] == "signup" and r["totals"]["results"] == 4 and r["totals"]["spend"] == 50.5   # no double counting
    assert r["totals"]["cost_per_result"] == 12.62 and r["currency"] == "THB"
    assert [x["name"] for x in r["countries"]] == ["AE", "SA"] and r["placements"][0]["name"] == "Instagram Reels"
    assert r["truth"]["real"] == 1 and r["truth"]["all_signups"] == 2
    assert sum(r["series"]["results"]) == 4
