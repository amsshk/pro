"""Auto buyer audiences in Meta (Premium)."""
import json

import pytest

from paysignal import audience_sync
from paysignal.audiences import META_MIN
from paysignal.server import create_app

ADMIN_KEY = "admin-key-0123456789abcdef"
SHOP_KEY = "shop-key-0123456789abcdef"
CLIENTS = {"pro": {"api_key": ADMIN_KEY, "name": "ProAds", "currency": "AED"},
           "shop": {"api_key": SHOP_KEY, "name": "Shop", "currency": "AED"}}


class R:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class Graph:
    def __init__(self, terms=False, gone_once=False):
        self.posts, self.n, self.terms, self.gone_once = [], 0, terms, gone_once

    def get(self, url, params, timeout):
        return R(200, {})

    def post(self, url, data, timeout):
        path = url.split("/v25.0/")[1]
        self.posts.append((path, data))
        if self.terms:
            return R(400, {"error": {"code": 200, "error_subcode": 1870034, "message": "You need to accept the Custom Audience Terms"}})
        if path.endswith("/usersreplace"):
            if self.gone_once:
                self.gone_once = False
                return R(400, {"error": {"code": 100, "message": "Object with ID '1' does not exist"}})
            return R(200, {"num_received": len(json.loads(data["payload"])["data"])})
        self.n += 1
        return R(200, {"id": f"aud{self.n}"})


def orders(n):
    rows = "\n".join(f"{i},2026-10-{1 + i % 27:02d},300,AED,b{i}@x.com" for i in range(n))
    return ("id,date,amount,currency,email\n" + rows + "\n").encode()


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"PROADS_AD_ACCOUNT_ID": "123", "PROADS_PAGE_ID": "555", "PROADS_ADS_TOKEN": "tok", "PROADS_META_DATASET_ID": "777"}.items():
        monkeypatch.setenv(k, v)
    a = create_app(CLIENTS, str(tmp_path / "a.db"), start_worker=False)
    return a


def state_of(a):
    return a.config["STATE"] if "STATE" in a.config else next(c.cell_contents for c in a.view_functions["audience_sync_status"].__closure__
                                                                 if type(c.cell_contents).__name__ == "App")


def test_creates_list_replaces_members_and_adds_lookalike(app):
    st = state_of(app)
    st.ads_http = g = Graph()
    c = app.test_client()
    H = {"X-API-Key": ADMIN_KEY}
    c.post("/upload/csv/pro", data=orders(META_MIN + 5), headers=H)
    r = c.post("/audiences/pro/meta-sync", json={"enable": True}, headers=H).json
    assert r["result"]["ok"] and r["enabled"] and r["audience_id"] == "aud1" and r["lookalike_id"] == "aud2"
    steps = [p for p, _ in g.posts]
    assert steps == ["act_123/customaudiences", "aud1/usersreplace", "act_123/customaudiences"]
    payload = json.loads(g.posts[1][1]["payload"])
    assert payload["schema"] == ["EMAIL", "PHONE"] and len(payload["data"]) == META_MIN + 5
    assert all(len(row[0]) == 64 and "@" not in row[0] for row in payload["data"])           # hashed
    assert json.loads(g.posts[2][1]["lookalike_spec"])["country"] == "AE"
    # second sync reuses both audiences
    c.post("/audiences/pro/meta-sync", json={"enable": True}, headers=H)
    assert [p for p, _ in g.posts][3:] == ["aud1/usersreplace"]
    assert st.audience_sync.due(now=10**12) == ["pro"] and st.audience_sync.due() == []
    off = c.post("/audiences/pro/meta-sync", json={"enable": False}, headers=H).json
    assert not off["enabled"] and off["audience_id"] == "aud1"


def test_small_list_has_no_lookalike_and_deleted_audience_is_recreated(app):
    st = state_of(app)
    st.ads_http = g = Graph(gone_once=True)
    c = app.test_client()
    H = {"X-API-Key": ADMIN_KEY}
    c.post("/upload/csv/pro", data=orders(10), headers=H)
    st.audience_sync.save("pro", ad_account="123", audience_id="old", enabled=True)
    r = c.post("/audiences/pro/meta-sync", json={"enable": True}, headers=H).json
    assert r["result"]["ok"] and r["audience_id"] == "aud1" and r["lookalike_id"] is None
    assert [p for p, _ in g.posts] == ["old/usersreplace", "act_123/customaudiences", "aud1/usersreplace"]


def test_terms_not_accepted_gives_the_link(app):
    st = state_of(app)
    st.ads_http = Graph(terms=True)
    c = app.test_client()
    H = {"X-API-Key": ADMIN_KEY}
    c.post("/upload/csv/pro", data=orders(3), headers=H)
    r = c.post("/audiences/pro/meta-sync", json={"enable": True}, headers=H).json
    assert not r["result"]["ok"] and "customaudiences/tos/?act=123" in r["result"]["error"] and "Accept" in r["last_error"]


def test_needs_premium_and_valid_country(app):
    c = app.test_client()
    r = c.post("/audiences/shop/meta-sync", json={"enable": True}, headers={"X-API-Key": SHOP_KEY})
    assert r.status_code == 402 and r.json["needs_tier"] == "premium" and "Auto buyer audiences" in r.json["error"]
    assert c.get("/audiences/shop/meta-sync", headers={"X-API-Key": SHOP_KEY}).json["premium"] is False
    assert c.post("/audiences/pro/meta-sync", json={"enable": True, "country": "UAE"}, headers={"X-API-Key": ADMIN_KEY}).status_code == 400


def test_country_from_currency():
    assert audience_sync.country_for("THB") == "TH" and audience_sync.country_for("xyz") == "AE"
