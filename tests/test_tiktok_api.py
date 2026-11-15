from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from paysignal.server import create_app

KEY = "tta-test-key-0123456789"
H = {"X-API-Key": KEY}


class R:
    def __init__(self, body, status=200):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class FakeTikTok:
    def __init__(self):
        self.calls = []
        self.fail_report = None

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append(("POST", url, json, headers))
        if url.endswith("/oauth2/access_token/"):
            if json["auth_code"] == "bad":
                return R({"code": 40001, "message": "auth_code invalid"})
            return R({"code": 0, "message": "OK", "data": {"access_token": "act.secret", "advertiser_ids": ["7651683849232695297", "111"]}})
        return R({"code": 0, "data": {}})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        assert headers["Access-Token"] == "act.secret"
        if "advertiser/info" in url:
            return R({"code": 0, "data": {"list": [{"advertiser_id": 7651683849232695297, "name": "Bynawi0615", "currency": "AED"},
                                                   {"advertiser_id": 111, "name": "Other", "currency": "USD"}]}})
        if "report/integrated/get" in url:
            from datetime import date
            span = (date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])).days + 1
            if span > 30:
                return R({"code": 40002, "message": "max time span is 30 days when use stat_time_day"})
            self.spans = getattr(self, "spans", []) + [span]
            if self.fail_report:
                return R({"code": self.fail_report, "message": "Access token expired"})
            d = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
            return R({"code": 0, "data": {"page_info": {"page": 1, "total_page": 1}, "list": [
                {"dimensions": {"ad_id": "1873948404761793", "stat_time_day": d + " 00:00:00"},
                 "metrics": {"spend": "88.50", "impressions": "12000", "clicks": "140", "campaign_name": "Sales",
                             "campaign_id": "1873948398763138", "adgroup_name": "UAE", "adgroup_id": "1", "ad_name": "Oud"}}]}})
        return R({"code": 40002, "message": "unknown"})


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_SECRET_KEY", "k" * 44)
    monkeypatch.setenv("TIKTOK_APP_ID", "7708410175098454037")
    monkeypatch.setenv("TIKTOK_APP_SECRET", "s3cret")
    a = create_app({"shop": {"api_key": KEY, "name": "Bynawi", "currency": "AED"}}, str(tmp_path / "t.db"), start_worker=False)
    a.extensions["paysignal"].tiktok_api_http = FakeTikTok()
    return a


def connect(c, code="good"):
    url = c.post("/tiktok/connect/shop", headers=H).json["url"]
    q = parse_qs(urlparse(url).query)
    assert q["app_id"] == ["7708410175098454037"] and q["redirect_uri"][0].endswith("/tiktok/callback")
    return c.get("/tiktok/callback", query_string={"auth_code": code, "state": q["state"][0]})


def test_connect_choose_account_and_read_spend(app):
    c = app.test_client()
    r = connect(c)
    assert "tiktok=connected" in r.headers["Location"]
    st = c.get("/status/shop", headers=H).json["connections"]["tiktok_ads"]
    assert st["connected"] and st["advertiser_id"] is None and len(st["advertisers"]) == 2 and "act." not in str(st)
    r = c.post("/tiktok/shop/advertiser", json={"advertiser_id": "7651683849232695297"}, headers=H).json
    assert r["advertiser_id"] == "7651683849232695297" and r["currency"] == "AED" and "1 ad(s)" in r["message"]
    res = c.get("/tiktok/shop/results", headers=H).json
    assert res["ads"][0]["spend"] == 88.5 and res["ads"][0]["campaign"] == "Sales"
    assert len(app.extensions["paysignal"].tiktok_api_http.spans) == 4      # 91 days asked in 30-day pieces
    assert c.post("/tiktok/shop/advertiser", json={"advertiser_id": "999"}, headers=H).status_code == 400


def test_cross_browser_and_bad_code_rejected(app):
    c = app.test_client()
    url = c.post("/tiktok/connect/shop", headers=H).json["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    other = app.test_client()
    assert "same+browser" in other.get("/tiktok/callback", query_string={"auth_code": "good", "state": state}).headers["Location"]
    assert "tiktok=error" in connect(app.test_client(), "bad").headers["Location"]


def test_revoked_token_asks_to_reconnect(app):
    c = app.test_client()
    connect(c)
    st = app.extensions["paysignal"]
    st.tiktok_api_http.fail_report = 40105
    c.post("/tiktok/shop/advertiser", json={"advertiser_id": "111"}, headers=H)
    s = c.get("/status/shop", headers=H).json["connections"]["tiktok_ads"]
    assert s["status"] == "expired" and "Connect the TikTok ad account again" in s["message"]
