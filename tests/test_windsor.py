from datetime import datetime, timedelta, timezone

import pytest

from paysignal.models import Payment
from paysignal.server import create_app

KEY = "w-test-key-0123456789"
H = {"X-API-Key": KEY}


class R:
    def __init__(self, body, status=200):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class Http:
    def __init__(self, rows=None, status=200, body=None):
        self.calls, self.rows, self.status, self.body = [], rows or [], status, body

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return R(self.body if self.body is not None else {"data": self.rows}, self.status)


def state_of(app):
    for f in app.view_functions.values():
        for cell in (getattr(f, "__closure__", None) or ()):
            if type(cell.cell_contents).__name__ == "App":
                return cell.cell_contents


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_SECRET_KEY", "k" * 44)
    return create_app({"shop": {"api_key": KEY, "name": "Bynawi", "currency": "AED"}}, str(tmp_path / "w.db"), start_worker=False)


def day(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


def test_spend_next_to_verified_sales_per_ad(app):
    st = state_of(app)
    st.windsor_http = http = Http([
        {"date": day(2), "account_id": "7651683849232695297", "currency": "AED", "campaign": "Sales", "campaign_id": "c1",
         "ad_group_name": "UAE", "ad_group_id": "g1", "ad_name": "Oud video", "ad_id": "A1", "spend": "120.5", "impressions": "9000", "clicks": "210"},
        {"date": day(1), "account_id": "7651683849232695297", "currency": "AED", "campaign": "Sales", "campaign_id": "c1",
         "ad_group_name": "UAE", "ad_group_id": "g1", "ad_name": "Oud video", "ad_id": "A1", "spend": "79.5", "impressions": "6000", "clicks": "140"},
        {"date": day(1), "account_id": "7651683849232695297", "currency": "AED", "campaign": "promote_17944", "campaign_id": "c2",
         "ad_group_name": "", "ad_group_id": "g2", "ad_name": "boost", "ad_id": "A2", "spend": "245.57", "impressions": "29473", "clicks": "390"}])
    c = app.test_client()
    r = c.post("/connect/shop/windsor", json={"api_key": " abcdefghijklmnop0123 "}, headers=H)
    assert r.status_code == 200 and r.json["connected"] and "2 ad(s)" in r.json["message"]
    assert http.calls[0][1]["api_key"] == "abcdefghijklmnop0123" and "ad_id" in http.calls[0][1]["fields"]
    for i in range(3):
        st.store.add(Payment(client_id="shop", source="shopify", external_id=f"o{i}", kind="purchase", amount=250.0, currency="AED",
                             occurred_at=datetime.now(timezone.utc) - timedelta(days=1), utm_source="tiktok", utm_campaign="Sales",
                             utm_content="A1", email=f"c{i}@x.ae"))
    st.store.add(Payment(client_id="shop", source="shopify", external_id="r0", kind="refund", amount=250.0, currency="AED",
                         occurred_at=datetime.now(timezone.utc), refund_of="o0"))
    res = c.get("/tiktok/shop/results", headers=H).json
    a1 = next(a for a in res["ads"] if a["ad_id"] == "A1")
    assert a1["spend"] == 200.0 and a1["orders"] == 3 and a1["kept_orders"] == 2 and a1["kept"] == 500.0 and a1["cost_per_order"] == 100.0
    promo = next(cmp for cmp in res["campaigns"] if cmp["promote"])
    assert promo["spend"] == 245.57 and promo["kept_orders"] == 0
    assert res["total"]["spend"] == 445.57 and not res["economics"]
    assert "key" not in str(c.get("/status/shop", headers=H).json["connections"]["windsor"]).replace("key_enc", "")


def test_bad_key_is_explained_and_nothing_saved(app):
    st = state_of(app)
    st.windsor_http = Http(status=401, body={"error": {"code": "authentication_error", "message": "Invalid API key provided"}})
    r = app.test_client().post("/connect/shop/windsor", json={"api_key": "x" * 30}, headers=H)
    assert r.status_code == 400 and "didn't accept" in r.json["error"] and not st.windsor.get("shop")
