import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from paysignal import shopify_app
from paysignal.server import create_app

SECRET = "shpss_app_secret"
KEY = "shop-test-key-0123456789"
H = {"X-API-Key": KEY}
SHOP = "glow-store.myshopify.com"


def order(n, status="PAID", refunds=(), email="a@x.com", days=3, test=False):
    return {"id": f"gid://shopify/Order/{n}", "name": f"#{n}", "processedAt": f"2026-10-{days:02d}T10:00:00Z",
            "createdAt": f"2026-10-{days:02d}T10:00:00Z", "currencyCode": "AED", "test": test, "cancelledAt": None,
            "displayFinancialStatus": status, "sourceName": "web",
            "totalPriceSet": {"shopMoney": {"amount": "300.00", "currencyCode": "AED"}},
            "totalDiscountsSet": {"shopMoney": {"amount": "0.0", "currencyCode": "AED"}}, "discountCodes": [],
            "lineItems": {"nodes": [{"sku": "HF-1", "title": "Facial", "quantity": 1,
                                     "originalUnitPriceSet": {"shopMoney": {"amount": "300.00", "currencyCode": "AED"}}}]},
            "refunds": [{"id": f"gid://shopify/Refund/{r}", "createdAt": "2026-10-09T10:00:00Z",
                         "totalRefundedSet": {"shopMoney": {"amount": "300.00", "currencyCode": "AED"}}} for r in refunds],
            "email": email, "phone": None, "customer": {"id": "gid://shopify/Customer/77"},
            "customerJourneySummary": {"firstVisit": None, "lastVisit": {
                "landingPage": "https://glow.ae/?fbclid=ABC&utm_content=reel1",
                "utmParameters": {"source": "instagram", "medium": "organic", "campaign": None, "content": "reel1"}}}}


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class FakeShopify:
    """Pretends to be Shopify: token endpoint + GraphQL Admin API."""
    def __init__(self, protected_ok=True):
        self.protected_ok = protected_ok
        self.calls = []
        self.pages = [[order(1001), order(1002, "REFUNDED", refunds=[501]), order(1003, "PENDING")],
                      [order(1004, test=True), order(1005, days=5)]]

    def post(self, url, timeout=None, data=None, json=None, headers=None):
        self.calls.append({"url": url, "data": data, "json": json, "headers": headers})
        if url.endswith("/admin/oauth/access_token"):
            assert data["client_secret"] == SECRET
            if data.get("grant_type") == "refresh_token":
                return Resp({"access_token": "tok-2", "scope": "read_orders", "expires_in": 3600,
                             "refresh_token": "ref-2", "refresh_token_expires_in": 7776000})
            assert data["code"] == "good-code" and data["expiring"] == "1"
            return Resp({"access_token": "tok-1", "scope": "read_orders", "expires_in": 3600,
                         "refresh_token": "ref-1", "refresh_token_expires_in": 7776000})
        assert "/admin/api/2026-10/graphql.json" in url and headers["X-Shopify-Access-Token"].startswith("tok-")
        q = json["query"]
        if "webhookSubscriptionCreate" in q:
            return Resp({"data": {"webhookSubscriptionCreate": {"webhookSubscription": {"id": "w"}, "userErrors": []}}})
        if "email" in q and not self.protected_ok:
            return Resp({"errors": [{"message": "Access denied for email field. Required access: protected customer data"}]})
        page = 0 if not json["variables"].get("cursor") else 1
        return Resp({"data": {"orders": {"nodes": self.pages[page],
                                         "pageInfo": {"hasNextPage": page == 0, "endCursor": "c1"}}}})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "client-123")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("PROADS_SECRET_KEY", "a-long-server-secret-for-tests")
    monkeypatch.setenv("PROADS_PUBLIC_URL", "https://pro.example.net")


def make(tmp_path, fake):
    app = create_app({"shop1": {"api_key": KEY}, "shop2": {"api_key": "other-key-0123456789abc"}},
                     str(tmp_path / "s.db"), start_worker=False)
    app.extensions["paysignal"]._shopify_http = fake
    return app, app.test_client()


def signed_callback(params):
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {**params, "hmac": hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()}


def wait_done(c):
    for _ in range(100):
        s = c.get("/status/shop1", headers=H).json["connections"]["shopify_app"]
        if s.get("sync_status") not in ("running", "idle"):
            return s
        time.sleep(0.05)
    raise AssertionError("import did not finish")


def connect(c):
    url = c.post("/shopify/connect/shop1", json={"shop": "glow-store"}, headers=H).json["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith(f"https://{SHOP}/admin/oauth/authorize?")
    assert q["redirect_uri"] == "https://pro.example.net/shopify/callback" and q["scope"] == "read_orders"
    return c.get("/shopify/callback", query_string=signed_callback(
        {"code": "good-code", "shop": SHOP, "state": q["state"], "timestamp": str(int(time.time()))}))


def test_normalize_shop():
    assert shopify_app.normalize_shop("Glow-Store") == SHOP
    assert shopify_app.normalize_shop("https://glow-store.myshopify.com/admin/orders") == SHOP
    assert shopify_app.normalize_shop("glow-store.myshopify.com.evil.com") is None
    assert shopify_app.normalize_shop("evil.com") is None


def test_not_set_up_gives_clear_message(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOPIFY_CLIENT_ID", raising=False)
    _, c = make(tmp_path, FakeShopify())
    r = c.post("/shopify/connect/shop1", json={"shop": "x"}, headers=H)
    assert r.status_code == 503 and "SHOPIFY_CLIENT_ID" in r.json["error"]
    assert not c.get("/status/shop1", headers=H).json["connections"]["shopify_app"]["available"]


def test_full_connect_import_flow(tmp_path, env):
    fake = FakeShopify()
    app, c = make(tmp_path, fake)
    r = connect(c)
    assert r.status_code == 302 and r.headers["Location"].endswith("#/upload?shopify=connected")
    s = wait_done(c)
    assert s["connected"] and s["shop"] == SHOP and s["imported"] == 3
    assert "60 days" in s["sync_message"]
    hooks = [x["json"]["variables"] for x in fake.calls if x["json"] and "webhookSubscriptionCreate" in x["json"]["query"]]
    assert {h["topic"] for h in hooks} == {"ORDERS_PAID", "REFUNDS_CREATE", "APP_UNINSTALLED"}
    assert hooks[0]["sub"]["uri"] == "https://pro.example.net/webhooks/shopify-app"
    totals = c.get("/status/shop1", headers=H).json["totals"]
    assert totals == {"gross": 900.0, "refunds": 300.0, "net": 600.0}       # pending + test orders skipped
    rep = c.get("/report/shop1", headers=H).json["content"]
    assert any(row["content"] == "reel1" and row["platform"] == "instagram" for row in rep)
    raw = app.extensions["paysignal"].store.db.execute("SELECT tokens FROM shopify_shops").fetchone()[0]
    assert "tok-1" not in raw and "ref-1" not in raw                        # tokens encrypted at rest


def test_import_without_customer_data_access(tmp_path, env):
    _, c = make(tmp_path, FakeShopify(protected_ok=False))
    connect(c)
    s = wait_done(c)
    assert s["sync_status"] == "done" and s["imported"] == 3 and "emails and phones" in s["sync_message"]


def test_callback_rejects_forgery_reuse_and_wrong_shop(tmp_path, env):
    _, c = make(tmp_path, FakeShopify())
    url = c.post("/shopify/connect/shop1", json={"shop": "glow-store"}, headers=H).json["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    bad = signed_callback({"code": "good-code", "shop": SHOP, "state": state, "timestamp": "1"})
    bad["hmac"] = "0" * 64
    assert "error" in c.get("/shopify/callback", query_string=bad).headers["Location"]
    other = c.get("/shopify/callback", query_string=signed_callback(
        {"code": "good-code", "shop": "other.myshopify.com", "state": state, "timestamp": "1"}))
    assert "error" in other.headers["Location"]
    again = c.get("/shopify/callback", query_string=signed_callback(
        {"code": "good-code", "shop": SHOP, "state": state, "timestamp": "1"}))
    assert "error" in again.headers["Location"]                            # state is single-use
    assert not c.get("/status/shop1", headers=H).json["connections"]["shopify_app"]["connected"]


def test_store_cannot_be_taken_by_another_account(tmp_path, env):
    _, c = make(tmp_path, FakeShopify())
    connect(c)
    wait_done(c)
    r = c.post("/shopify/connect/shop2", json={"shop": SHOP}, headers={"X-API-Key": "other-key-0123456789abc"})
    assert r.status_code == 400 and "another" in r.json["error"]


def app_webhook(c, topic, payload, secret=SECRET, path="/webhooks/shopify-app"):
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return c.post(path, data=body, headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": topic,
                                            "X-Shopify-Shop-Domain": SHOP})


def test_live_webhooks_dedupe_with_import_and_uninstall(tmp_path, env):
    _, c = make(tmp_path, FakeShopify())
    connect(c)
    wait_done(c)
    same = {"id": 1001, "total_price": "300.00", "currency": "AED", "processed_at": "2026-10-03T10:00:00Z", "line_items": []}
    assert app_webhook(c, "orders/paid", same).json["duplicates"] == 1
    new = dict(same, id=2001)
    assert app_webhook(c, "orders/paid", new).json["new"] == 1
    assert app_webhook(c, "orders/paid", new, secret="wrong").status_code == 401
    assert app_webhook(c, "app/uninstalled", {}).json["disconnected"]
    assert not c.get("/status/shop1", headers=H).json["connections"]["shopify_app"]["connected"]


def test_privacy_webhooks_delete_data(tmp_path, env):
    _, c = make(tmp_path, FakeShopify())
    connect(c)
    wait_done(c)
    p = "/webhooks/shopify-compliance"
    assert app_webhook(c, "customers/data_request", {"shop_domain": SHOP, "orders_requested": [1001]}, path=p).status_code == 200
    r = app_webhook(c, "customers/redact", {"shop_domain": SHOP, "customer": {"id": 77, "email": "a@x.com"}}, path=p).json
    assert r["deleted"] == 4                                              # 3 orders + 1 refund of that customer
    assert c.get("/status/shop1", headers=H).json["totals"]["gross"] == 0
    assert app_webhook(c, "shop/redact", {"shop_domain": SHOP}, path=p).json["received"]
    assert app_webhook(c, "shop/redact", {"shop_domain": SHOP}, secret="bad", path=p).status_code == 401


def test_expired_access_token_is_refreshed(tmp_path, env):
    fake = FakeShopify()
    app, c = make(tmp_path, fake)
    connect(c)
    wait_done(c)
    state = app.extensions["paysignal"]
    tokens = json.loads(shopify_app.decrypt(state.store.shop_for("shop1")["tokens"]))
    tokens["access_expires"] = time.time() - 1
    state.store.update_shop_tokens("shop1", shopify_app.encrypt(json.dumps(tokens)))
    client = state.shopify_client("shop1")
    assert client.token == "tok-2"
    assert json.loads(shopify_app.decrypt(state.store.shop_for("shop1")["tokens"]))["refresh_token"] == "ref-2"


def test_disconnect(tmp_path, env):
    _, c = make(tmp_path, FakeShopify())
    connect(c)
    wait_done(c)
    assert c.post("/shopify/disconnect/shop1", headers=H).json["disconnected"]
    assert c.post("/shopify/sync/shop1", headers=H).status_code == 400
