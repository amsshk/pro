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
    assert all(x <= 30 for x in app.extensions["paysignal"].tiktok_api_http.spans)      # 91 days asked in pieces
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


class Publishing(FakeTikTok):
    def __init__(self, fail_at=None):
        super().__init__()
        self.fail_at = fail_at

    def post(self, url, json=None, timeout=None, headers=None, data=None, files=None):
        path = url.split("/open_api/v1.3/")[-1].strip("/")
        self.calls.append(("POST", path, json if json is not None else data, headers))
        if path == "oauth2/access_token":
            return super().post(url, json=json)
        if path == self.fail_at:
            return R({"code": 40002, "message": f"bad {path}"})
        reply = {"campaign/create": {"campaign_id": "C1"}, "adgroup/create": {"adgroup_id": "G1"},
                 "file/video/ad/upload": [{"video_id": "V1"}], "file/image/ad/upload": {"image_id": "I1"},
                 "identity/create": {"identity_id": "ID1"}, "ad/create": {"ad_ids": ["A1", "A2"]}}.get(path, {})
        return R({"code": 0, "data": reply})

    def get(self, url, params=None, headers=None, timeout=None):
        if "pixel/list" in url:
            return R({"code": 0, "data": {"pixels": [{"pixel_id": "777", "pixel_code": "DBS29OJC77U7CBT60R3G", "pixel_name": "Verified"}]}})
        if "file/video/ad/info" in url:
            return R({"code": 0, "data": {"list": [{"video_id": "V1", "video_cover_url": "https://p16.tiktokcdn.com/cover.jpg"}]}})
        if "identity/get" in url:
            return R({"code": 0, "data": {"identity_list": []}})
        return super().get(url, params=params, headers=headers, timeout=timeout)


PLAN = {"name": "Bynawi Oud", "daily_budget": 100, "platform": "tiktok", "optimization": "add_to_cart",
        "targeting": {"countries": ["AE"], "age_min": 18},
        "ad_sets": [{"name": "UAE broad", "budget_share": 1, "ads": [
            {"headline": "Oud that lasts all day", "primary_text": "Real oud, real compliments.", "call_to_action": "SHOP_NOW"},
            {"headline": "Gift box unboxing", "primary_text": "The gift they will remember.", "call_to_action": "BUY_NOW"}]}]}


def setup_publish(app, fail_at=None):
    st = app.extensions["paysignal"]
    st.tiktok_api_http = Publishing(fail_at)
    c = app.test_client()
    connect(c)
    c.post("/tiktok/shop/advertiser", json={"advertiser_id": "7651683849232695297"}, headers=H)
    ids = []
    for n in (1, 2):
        it = st.media.add("shop", {"source": "upload", "origin": "upload", "url": f"upload://v{n}-{id(st.tiktok_api_http)}", "kind": "video", "width": 1080, "height": 1920,
                                   "quality": "good", "title": f"v{n}", "price": None, "poster": None, "ai": "none", "approved": 1}, b"\x00\x01video" * 10)
        ids.append(it)
    assert all(ids), ids
    return c, st, ids


def test_publish_creates_campaign_off_with_tracking_link_and_rolls_back_on_failure(app):
    c, st, ids = setup_publish(app)
    assert c.get("/tiktok/shop/pixels", headers=H).json["pixels"][0]["pixel_id"] == "777"
    body = {"plan": PLAN, "media": {"0-0": f"media:{ids[0]}", "0-1": f"media:{ids[1]}"}, "landing_url": "bynawi.com",
            "pixel_id": "777", "event": "add_to_cart"}
    r = c.post("/campaign/shop/publish-tiktok", json=body, headers=H)
    assert r.status_code == 200, r.json
    calls = {p: b for _, p, b, _ in st.tiktok_api_http.calls if isinstance(p, str)}
    assert calls["campaign/create"]["operation_status"] == "DISABLE" and calls["campaign/create"]["objective_type"] == "WEB_CONVERSIONS"
    g = calls["adgroup/create"]
    assert g["pixel_id"] == "777" and g["optimization_event"] == "ON_WEB_CART" and g["location_ids"] == ["290557"] and g["budget"] == 100
    assert g["skip_learning_phase"] is False                            # TikTok must learn who buys
    assert [x["call_to_action"] for x in calls["ad/create"]["creatives"]] == ["SHOP_NOW", "SHOP_NOW"]    # BUY_NOW isn't a TikTok button
    cr = calls["ad/create"]["creatives"][0]
    assert "utm_source=tiktok" in cr["landing_page_url"] and cr["video_id"] == "V1" and cr["image_ids"] == ["I1"] and cr["identity_id"] == "ID1"
    live = c.post("/campaign/shop/tiktok-launch/C1", json={"action": "live"}, headers=H)
    assert live.status_code == 400                                     # needs confirm
    assert c.post("/campaign/shop/tiktok-launch/C1", json={"action": "live", "confirm": True}, headers=H).json["status"] == "ACTIVE"

    c2, st2, ids2 = setup_publish(app, fail_at="ad/create")
    body["media"] = {"0-0": f"media:{ids2[0]}", "0-1": f"media:{ids2[1]}"}
    r = c2.post("/campaign/shop/publish-tiktok", json=body, headers=H)
    assert r.status_code == 502 and "bad ad/create" in r.json["error"]
    dels = [b for _, p, b, _ in st2.tiktok_api_http.calls if p == "campaign/status/update"]
    assert dels and dels[-1]["operation_status"] == "DELETE"


def test_tiktok_budget_minimum_checked_before_ai(app, monkeypatch):
    from paysignal import billing
    monkeypatch.setattr(billing, "fx_rate", lambda a, b, http=None: 3.6725)
    r = app.test_client().post("/campaign/shop", json={"daily_budget": 10, "platform": "tiktok"}, headers=H)
    assert r.status_code == 400 and "minimum" in r.json["error"]


def test_tiktok_library_videos_reused_without_upload(app):
    st = app.extensions["paysignal"]

    class Lib(Publishing):
        def get(self, url, params=None, headers=None, timeout=None):
            if "file/video/ad/search" in url:
                return R({"code": 0, "data": {"page_info": {"total_page": 1}, "list": [
                    {"video_id": "v_lib1", "file_name": "oud.mp4", "video_cover_url": "https://p16.tiktokcdn.com/c1.jpg", "width": 720, "height": 1280},
                    {"video_id": "v_lib2", "file_name": "gift.mp4", "video_cover_url": "https://p16.tiktokcdn.com/c2.jpg"}]}})
            return super().get(url, params=params, headers=headers, timeout=timeout)
    st.tiktok_api_http = Lib()
    c = app.test_client()
    connect(c)
    c.post("/tiktok/shop/advertiser", json={"advertiser_id": "7651683849232695297"}, headers=H)
    r = c.post("/media/shop/tiktok", headers=H).json
    assert r == {"added": 2, "found": 2}
    assert c.post("/media/shop/tiktok", headers=H).json["added"] == 0           # no duplicates
    ids = [it["id"] for it in st.media.list("shop") if it["source"] == "tiktok"]
    body = {"plan": PLAN, "media": {"0-0": f"media:{ids[0]}", "0-1": f"media:{ids[1]}"}, "landing_url": "bynawi.com",
            "pixel_id": "777", "event": "add_to_cart"}
    assert c.post("/campaign/shop/publish-tiktok", json=body, headers=H).status_code == 200
    paths = [p for _, p, _, _ in st.tiktok_api_http.calls]
    assert "file/video/ad/upload" not in paths
    cr = [b for _, p, b, _ in st.tiktok_api_http.calls if p == "ad/create"][0]["creatives"]
    assert [x["video_id"] for x in cr] == ["v_lib1", "v_lib2"]


def test_own_tiktok_posts_become_spark_ads(app):
    st = app.extensions["paysignal"]

    class Posts(Publishing):
        def get(self, url, params=None, headers=None, timeout=None):
            if "identity/get" in url:
                if params["identity_type"] == "BC_AUTH_TT":
                    return R({"code": 0, "data": {"identity_list": [{"identity_id": "tt1", "identity_type": "BC_AUTH_TT",
                                                                     "identity_authorized_bc_id": "7651694019151560725", "display_name": "bynawi.ae"}]}})
                return R({"code": 40001, "message": "no permission"})
            if "identity/video/get" in url:
                assert params["identity_authorized_bc_id"] == "7651694019151560725"
                return R({"code": 0, "data": {"has_more": False, "video_list": [
                    {"item_id": "7300000000000000001", "text": "Royal oud unboxing", "video_info": {"poster_url": "https://p16.tiktokcdn.com/a.jpg"}},
                    {"item_id": "7300000000000000002", "text": "Gift set", "video_info": {"poster_url": "https://p16.tiktokcdn.com/b.jpg"}}]}})
            return super().get(url, params=params, headers=headers, timeout=timeout)
    st.tiktok_api_http = Posts()
    c = app.test_client()
    connect(c)
    c.post("/tiktok/shop/advertiser", json={"advertiser_id": "7651683849232695297"}, headers=H)
    r = c.post("/media/shop/tiktok-posts", headers=H).json
    assert r["found"] == 2 and r["added"] == 2 and r["why"] == ""
    ids = [it["id"] for it in st.media.list("shop") if str(it["url"]).startswith("tiktok-post:")]
    body = {"plan": PLAN, "media": {"0-0": f"media:{ids[0]}", "0-1": f"media:{ids[1]}"}, "landing_url": "bynawi.com",
            "pixel_id": "777", "event": "add_to_cart"}
    assert c.post("/campaign/shop/publish-tiktok", json=body, headers=H).status_code == 200
    cr = [b for _, p, b, _ in st.tiktok_api_http.calls if p == "ad/create"][0]["creatives"]
    assert cr[0]["identity_type"] == "BC_AUTH_TT" and cr[0]["tiktok_item_id"] == "7300000000000000001"
    assert cr[0]["identity_authorized_bc_id"] == "7651694019151560725" and "video_id" not in cr[0]
    assert not any(p in ("file/video/ad/upload", "file/image/ad/upload") for _, p, _, _ in st.tiktok_api_http.calls)


def test_posts_refused_explains_how_to_fix(app):
    st = app.extensions["paysignal"]

    class NoPosts(Publishing):
        def get(self, url, params=None, headers=None, timeout=None):
            if "identity/get" in url:
                return R({"code": 40001, "message": "permission denied"})
            return super().get(url, params=params, headers=headers, timeout=timeout)
    st.tiktok_api_http = NoPosts()
    c = app.test_client()
    connect(c)
    c.post("/tiktok/shop/advertiser", json={"advertiser_id": "7651683849232695297"}, headers=H)
    r = c.post("/media/shop/tiktok-posts", headers=H).json
    assert r["found"] == 0 and "permission denied" in r["why"] and "Business Center" in r["why"]
