import json

from paysignal.server import create_app


class R:
    def __init__(self, body):
        self.status_code, self.body = 200, body

    def json(self):
        return self.body


class TT:
    def __init__(self):
        self.calls = []

    def get(self, url, params, headers, timeout):
        self.calls.append(params)
        dims = json.loads(params.get("dimensions", "[]"))
        if url.endswith("campaign/get/"):
            return R({"code": 0, "data": {"list": [
                {"campaign_id": 1, "campaign_name": "BYNAWI GCC Purchase Conversions", "operation_status": "ENABLE", "secondary_status": "CAMPAIGN_STATUS_ENABLE"},
                {"campaign_id": 2, "campaign_name": "promote_1794", "operation_status": "ENABLE", "secondary_status": "CAMPAIGN_STATUS_ENABLE"},
                {"campaign_id": 3, "campaign_name": "Old", "operation_status": "DISABLE"}], "page_info": {"total_page": 1}}})
        if params.get("data_level") == "AUCTION_CAMPAIGN":
            return R({"code": 0, "data": {"list": [{"dimensions": {"campaign_id": "1"}, "metrics": {"spend": "180", "conversion": "3"}},
                                                    {"dimensions": {"campaign_id": "2"}, "metrics": {"spend": "20", "conversion": "0"}}]}})
        if params.get("report_type") == "BASIC":
            return R({"code": 0, "data": {"list": [
                {"dimensions": {"stat_time_day": "2026-11-15 00:00:00"}, "metrics": {"spend": "120.5", "impressions": "9000", "clicks": "150", "conversion": "2"}},
                {"dimensions": {"stat_time_day": "2026-11-16 00:00:00"}, "metrics": {"spend": "79.5", "impressions": "6000", "clicks": "90", "conversion": "1"}}]}})
        if dims == ["country_code"]:
            return R({"code": 40002, "message": "dimension needs an id"})          # first shape refused: retried
        if "country_code" in dims:
            return R({"code": 0, "data": {"list": [{"dimensions": {"country_code": "AE"}, "metrics": {"clicks": "200", "conversion": "3"}},
                                                    {"dimensions": {"country_code": "QA"}, "metrics": {"clicks": "40", "conversion": "0"}}]}})
        return R({"code": 0, "data": {"list": [{"dimensions": {"placement": "PLACEMENT_TIKTOK"}, "metrics": {"clicks": "240", "conversion": "3"}}]}})


def test_tiktok_live_card(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_SECRET_KEY", "test-secret-key-for-encryption-0123456789")
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    st = app.extensions["paysignal"]
    st.tiktok_api_http = TT()
    c = app.test_client()
    h = {"X-API-Key": c.post("/auth/signup", json={"business": "Bynawi", "email": "o@bynawi.ae", "password": "first-password-1", "terms": True}).json["token"]}
    assert c.get("/ads/bynawi/tiktok", headers=h).json == {"connected": False}
    from paysignal import shopify_app
    with st.lock:
        st.tiktok_ads.save("bynawi", shopify_app.encrypt("tok"), None, 0, [{"id": "765", "name": "Bynawi0615", "currency": "AED"}])
        st.tiktok_ads.choose("bynawi", "765")
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    c.post("/upload/csv/bynawi", data=f"id,date,amount,currency,email,utm_source,utm_medium\n1,{today},340,AED,a@x.ae,tiktok,paid\n2,{today},160,AED,b@x.ae,,\n", headers=h)
    r = c.get("/ads/bynawi/tiktok?period=7", headers=h).json
    assert r["connected"] and not r.get("error"), r
    assert r["totals"]["spend"] == 200.0 and r["totals"]["results"] == 3 and r["totals"]["impressions"] == 15000
    assert r["truth"]["orders"] == 2 and r["truth"]["tracked"] == 1 and r["truth"]["cost_per_tracked_order"] == 200.0
    assert r["countries"][0] == {"name": "AE", "clicks": 200, "results": 3} and r["placements"][0]["name"] == "TikTok"
    names = [c["name"] for c in r["campaigns"]]
    assert names[0] == "BYNAWI GCC Purchase Conversions" and r["campaigns"][0]["spend"] == 180.0 and names[1] == "promote_1794"
    assert len(r["dates"]) == 7 and sum(r["series"]["results"]) in (0, 1, 2, 3)
    n = len(st.tiktok_api_http.calls)
    c.get("/ads/bynawi/tiktok?period=7", headers=h)
    assert len(st.tiktok_api_http.calls) == n                        # cached for 10 minutes
