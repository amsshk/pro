import time
from datetime import datetime, timedelta, timezone

import pytest

from paysignal.server import create_app

ADMIN = {"X-API-Key": "admin-key-0123456789abc"}
CLIENTS = {"pro": {"api_key": "admin-key-0123456789abc"}}
ACCT = {"business": "Glow Clinic", "email": "owner@glow.ae", "password": "first-password-1", "terms": True, "phone": "0501234567"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_PUBLIC_URL", "https://pro.example.net")
    return create_app(CLIENTS, str(tmp_path / "x.db"), start_worker=False)


def test_check_link_shows_totals_only_and_expires(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    c.post("/import/glow-clinic", data=f"id,date,amount,currency,email\nA1,{day},250,AED,secret.buyer@mail.com\nA2,{day},100,AED,\n",
           headers={"X-API-Key": tok})
    db = app.extensions["paysignal"].store.db
    at = int(time.time()) - 86400
    db.execute("INSERT INTO capi_log (client_id, at, mode, purchases, refunds, sent, received, no_identifier, too_old, error, trace, keys)"
               " VALUES ('glow-clinic', ?, 'live', 2, 0, 2, 2, 0, 0, NULL, NULL, '{}')", (at,))
    db.commit()
    assert c.post("/admin/pro/check-link/glow-clinic", headers={"X-API-Key": tok}).status_code in (401, 403)   # admins only
    r = c.post("/admin/pro/check-link/glow-clinic", headers=ADMIN, json={})
    link = r.json["link"]
    assert link.startswith("https://pro.example.net/check/")
    page = c.get(link.replace("https://pro.example.net", ""))
    body = page.get_data(as_text=True)
    assert page.status_code == 200 and "noindex" in page.headers["X-Robots-Tag"] and page.headers["Cache-Control"] == "no-store"
    assert "Sent live to Meta: 2 accepted" in body and "350" in body
    assert "secret.buyer" not in body and "A1" not in body and "owner@glow.ae" not in body
    assert c.get("/check/wrong-token").status_code == 404
    assert c.post("/admin/pro/check-link/glow-clinic", headers=ADMIN, json={"cancel": True}).json["cancelled"] == 1
    assert c.get(link.replace("https://pro.example.net", "")).status_code == 404


def test_check_link_expired(app):
    c = app.test_client()
    c.post("/auth/signup", json=ACCT)
    link = c.post("/admin/pro/check-link/glow-clinic", headers=ADMIN, json={}).json["link"]
    db = app.extensions["paysignal"].store.db
    db.execute("UPDATE check_links SET expires_at=?", (int(time.time()) - 1,))
    db.commit()
    assert c.get(link.replace("https://pro.example.net", "")).status_code == 404
    assert "/check/" not in c.get("/robots.txt").get_data(as_text=True)   # noindex header + meta keep it off Google; readers can open it


def test_check_page_shows_shopify_webhooks(app):
    import base64, hashlib, hmac, json
    c = app.test_client()
    c.post("/auth/signup", json=ACCT)
    st = app.extensions["paysignal"]
    r = c.post("/webhooks/shopify/glow-clinic", data=b"{}", headers={"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": "x"})
    assert r.status_code == 401
    link = c.post("/admin/pro/check-link/glow-clinic", headers=ADMIN, json={}).json["link"]
    body = c.get(link.replace("https://pro.example.net", "")).get_data(as_text=True)
    assert "0 accepted, 1 rejected" in body and "no signing key saved" in body and "Shopify app: not connected" in body


def test_check_page_second_address(app):
    c = app.test_client()
    c.post("/auth/signup", json=ACCT)
    link = c.post("/admin/pro/check-link/glow-clinic", headers=ADMIN, json={}).json["link"]
    assert c.get(link.replace("https://pro.example.net/check/", "/tracking-check/")).status_code == 200


def test_check_page_counts_orders_with_contact(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    c.post("/import/glow-clinic", data=f"id,date,amount,currency,email\nA1,{day},250,AED,a@b.com\nA2,{day},100,AED,\n",
           headers={"X-API-Key": tok})
    link = c.post("/admin/pro/check-link/glow-clinic", headers=ADMIN, json={}).json["link"]
    body = c.get(link.replace("https://pro.example.net", "")).get_data(as_text=True)
    assert "1 with email, 0 with phone, 1 with neither" in body and "a@b.com" not in body
