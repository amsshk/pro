import base64
from urllib.parse import parse_qs, urlparse

import pytest

from paysignal.server import create_app
from test_meta_ads import PNG, FakeGraph, plan

ACCT = {"business": "Glow Clinic", "email": "owner@glow.ae", "password": "first-password-1", "terms": True}


class R:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class FakeFacebook:
    """Graph API reads for the connect flow."""
    def __init__(self):
        self.calls, self.deleted = [], []

    def get(self, url, params=None, timeout=None):
        path = url.split("/v25.0/")[1]
        self.calls.append((path, params))
        if path == "oauth/access_token":
            if params.get("code") == "bad":
                return R(400, {"error": {"message": "Invalid code"}})
            return R(200, {"access_token": "LONG" if "fb_exchange_token" in params else "SHORT", "expires_in": 5184000})
        if path == "me":
            return R(200, {"id": "9", "name": "Sara"})
        if path == "me/adaccounts":
            return R(200, {"data": [{"id": "act_11", "name": "Glow ads", "currency": "AED", "account_status": 1}]})
        if path == "me/accounts":
            return R(200, {"data": [{"id": "p1", "name": "Glow Clinic", "instagram_business_account": {"id": "ig1", "username": "glow"}},
                                    {"id": "p2", "name": "Glow Spa"}]})
        if path == "act_11/adspixels":
            return R(200, {"data": [{"id": "px1", "name": "Glow pixel"}]})
        return R(404, {"error": {"message": "unknown " + path}})

    def delete(self, url, params=None, timeout=None):
        self.deleted.append(url)


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"META_APP_ID": "app1", "META_APP_SECRET": "sec", "META_LOGIN_CONFIG_ID": "cfg1",
                 "PROADS_SECRET_KEY": "a-long-secret-key-for-tests", "PROADS_PUBLIC_URL": "https://pro.example.net"}.items():
        monkeypatch.setenv(k, v)
    for k in ("PROADS_AD_ACCOUNT_ID", "PROADS_PAGE_ID", "PROADS_ADS_TOKEN", "PROADS_META_DATASET_ID", "PROADS_META_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    a = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "x.db"), start_worker=False)
    a.extensions["paysignal"].meta_http = FakeFacebook()
    a.extensions["paysignal"].ads_http = FakeGraph()
    return a


def connect(c, tok):
    url = c.post("/meta/connect/glow-clinic", headers={"X-API-Key": tok}).json["url"]
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.facebook.com/v25.0/dialog/oauth") and q["config_id"] == ["cfg1"]
    assert q["redirect_uri"] == ["https://pro.example.net/meta/callback"]
    return c.get("/meta/callback", query_string={"code": "good", "state": q["state"][0]})


def test_client_connects_picks_assets_and_publishes_in_own_account(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    H = {"X-API-Key": tok}
    r = connect(c, tok)
    assert r.status_code == 302 and r.headers["Location"].endswith("meta=connected")
    v = c.get("/meta/glow-clinic", headers=H).json
    assert v["connected"] and v["user"] == "Sara" and v["ad_account"] == "act_11" and v["page_id"] is None
    assert "token" not in v and "LONG" not in str(v)
    assert not v["ready"]
    assert c.get("/meta/glow-clinic/pixels", query_string={"ad_account": "act_99"}, headers=H).status_code == 400
    px = c.get("/meta/glow-clinic/pixels", query_string={"ad_account": "act_11"}, headers=H).json["pixels"]
    assert px == [{"id": "px1", "name": "Glow pixel"}]
    assert c.post("/meta/glow-clinic/select", json={"ad_account": "act_11", "page_id": "nope"}, headers=H).status_code == 400
    v = c.post("/meta/glow-clinic/select", json={"ad_account": "act_11", "page_id": "p1", "pixel_id": "px1"}, headers=H).json
    assert v["ready"] and v["instagram_id"] == "ig1"
    st = c.get("/status/glow-clinic", headers=H).json
    assert st["connections"]["ads_publish"]["configured"] and st["connections"]["meta_login"]["ready"]
    assert st["connections"]["meta_capi"]["live"]                       # sales now go to their own Pixel
    assert "LONG" not in str(st)
    # publish goes to THEIR ad account, page and pixel, with their token
    r = c.post("/campaign/glow-clinic/publish", json={"plan": plan(), "images": {"0-0": PNG, "0-1": PNG},
                                                      "landing_url": "glow.ae"}, headers=H)
    assert r.status_code == 200, r.json
    g = app.extensions["paysignal"].ads_http
    assert g.posts[0][0] == "act_11/campaigns" and g.posts[0][1]["access_token"] == "LONG"
    assert '"pixel_id": "px1"' in g.posts[1][1]["promoted_object"]
    story = g.posts[3][1]["object_story_spec"]
    assert '"page_id": "p1"' in story and '"instagram_user_id": "ig1"' in story and "https://glow.ae" in story
    # disconnect forgets everything and asks Facebook to remove the permission
    assert c.post("/meta/glow-clinic/disconnect", headers=H).json == {"connected": False}
    assert not c.get("/meta/glow-clinic", headers=H).json["connected"]
    assert not c.get("/status/glow-clinic", headers=H).json["connections"]["meta_capi"]["live"]


def test_callback_rejects_bad_state_and_failed_exchange(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    assert "meta=error" in c.get("/meta/callback", query_string={"code": "x", "state": "forged"}).headers["Location"]
    url = c.post("/meta/connect/glow-clinic", headers={"X-API-Key": tok}).json["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    assert "meta=error" in c.get("/meta/callback", query_string={"code": "bad", "state": state}).headers["Location"]
    assert "meta=error" in c.get("/meta/callback", query_string={"code": "good", "state": state}).headers["Location"]  # single use
    assert "cancelled" in c.get("/meta/callback", query_string={"error": "access_denied"}).headers["Location"]


def test_deleting_the_account_removes_the_connection(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    connect(c, tok)
    state = app.extensions["paysignal"]
    assert state.meta_conns.get("glow-clinic")
    state.accounts.delete_everything("glow-clinic")
    assert state.meta_conns.get("glow-clinic") is None


def test_finds_pages_and_ad_accounts_owned_by_a_business_portfolio():
    from paysignal import meta_connect

    class OnlyBusiness(FakeFacebook):
        def get(self, url, params=None, timeout=None):
            path = url.split("/v25.0/")[1]
            data = {"me/adaccounts": [], "me/accounts": [], "me/businesses": [{"id": "b1", "name": "Glow LLC"}],
                    "b1/owned_pages": [{"id": "p9", "name": "Glow Page", "instagram_business_account": {"id": "ig9", "username": "glow"}}],
                    "b1/owned_ad_accounts": [{"id": "act_9", "name": "Glow ads", "currency": "AED", "account_status": 1}],
                    "b1/client_pages": [{"id": "p9", "name": "Glow Page"}], "b1/client_ad_accounts": []}
            if path in data:
                return R(200, {"data": data[path]})
            return super().get(url, params, timeout)

    found = meta_connect.discover(OnlyBusiness(), "tok")
    assert [p["id"] for p in found["pages"]] == ["p9"] and found["pages"][0]["instagram_id"] == "ig9"
    assert [a["id"] for a in found["ad_accounts"]] == ["act_9"]
