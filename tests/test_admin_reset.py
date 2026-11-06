import time

import pytest

from paysignal import mailer
from paysignal.server import create_app

ADMIN = {"X-API-Key": "admin-key-0123456789abc"}
CLIENTS = {"pro": {"api_key": "admin-key-0123456789abc"}, "other": {"api_key": "other-key-0123456789abc"}}
ACCT = {"business": "Glow Clinic", "email": "owner@glow.ae", "password": "first-password-1", "terms": True,
        "phone": "0501234567"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("PROADS_PUBLIC_URL", "https://pro.example.net")
    return create_app(CLIENTS, str(tmp_path / "x.db"), start_worker=False)


def link_token(link):
    assert link.startswith("https://pro.example.net/app#/reset?token=")
    return link.split("token=", 1)[1]


def test_admin_sees_accounts_and_others_cannot(app):
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    c.post("/import/glow-clinic", data="id,date,amount,currency\n1,2026-10-01,100,AED\n", headers={"X-API-Key": tok})
    assert c.get("/status/pro", headers=ADMIN).json["admin"]
    d = c.get("/admin/pro/accounts", headers=ADMIN).json
    a = d["accounts"][0]
    assert a["email"] == "owner@glow.ae" and a["orders"] == 1 and a["activated_at"] and a["signed_in"]
    assert d["totals"]["accounts"] == 1 and d["totals"]["activated"] == 1 and not d["email_on"]
    assert "pw_hash" not in a
    assert c.get("/admin/other/accounts", headers={"X-API-Key": "other-key-0123456789abc"}).status_code == 403
    assert c.get("/admin/glow-clinic/accounts", headers={"X-API-Key": tok}).status_code == 403
    assert not c.get("/status/glow-clinic", headers={"X-API-Key": tok}).json["admin"]


def test_reset_link_flow(app):
    c = app.test_client()
    old = c.post("/auth/signup", json=ACCT).json["token"]
    link = c.post("/admin/pro/reset-link/glow-clinic", headers=ADMIN).json["link"]
    tok = link_token(link)
    assert c.post("/auth/reset", json={"token": tok, "password": "short"}).status_code == 400
    r = c.post("/auth/reset", json={"token": tok, "password": "brand-new-password"})
    assert r.status_code == 200 and r.json["client_id"] == "glow-clinic"
    assert c.get("/status/glow-clinic", headers={"X-API-Key": old}).status_code == 401      # old sessions ended
    assert c.post("/auth/reset", json={"token": tok, "password": "another-password-9"}).status_code == 400  # single use
    assert c.post("/auth/login", json={"login": "owner@glow.ae", "password": "first-password-1"}).status_code == 401
    assert c.post("/auth/login", json={"login": "owner@glow.ae", "password": "brand-new-password"}).status_code == 200


def test_reset_link_expires(app):
    c = app.test_client()
    c.post("/auth/signup", json=ACCT)
    tok = link_token(c.post("/admin/pro/reset-link/glow-clinic", headers=ADMIN).json["link"])
    app.extensions["paysignal"].store.db.execute("UPDATE password_resets SET expires=?", (time.time() - 1,))
    assert c.post("/auth/reset", json={"token": tok, "password": "brand-new-password"}).status_code == 400


def test_forgot_without_email_setup(app):
    r = app.test_client().post("/auth/forgot", json={"email": "owner@glow.ae"}).json
    assert r["sent"] is False and "isn't switched on" in r["message"]


def test_forgot_sends_email_and_never_reveals_accounts(app, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "ProAds <hello@example.com>")
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, text: sent.append((to, subject, text)) or True)
    c = app.test_client()
    c.post("/auth/signup", json=ACCT)
    known = c.post("/auth/forgot", json={"email": "OWNER@glow.ae"}).json
    unknown = c.post("/auth/forgot", json={"email": "nobody@glow.ae"}).json
    assert known == unknown                                                  # same answer either way
    for _ in range(50):
        if sent:
            break
        time.sleep(0.02)
    to, subject, text = sent[0]
    assert to == "owner@glow.ae" and "Reset" in subject
    tok = link_token(text.split("\n\n")[2])
    assert c.post("/auth/reset", json={"token": tok, "password": "brand-new-password"}).status_code == 200


def test_admin_delete_needs_confirmation(app):
    c = app.test_client()
    c.post("/auth/signup", json=ACCT)
    assert c.post("/admin/pro/delete/glow-clinic", json={}, headers=ADMIN).status_code == 400
    assert c.post("/admin/pro/delete/glow-clinic", json={"confirm": "glow-clinic"}, headers=ADMIN).json["deleted"]
    assert c.get("/admin/pro/accounts", headers=ADMIN).json["accounts"] == []
    assert c.post("/admin/pro/delete/pro", json={"confirm": "pro"}, headers=ADMIN).status_code == 404   # settings accounts stay


def test_admins_from_setting(app, monkeypatch):
    monkeypatch.setenv("PROADS_ADMINS", "other")
    c = app.test_client()
    assert c.get("/admin/other/accounts", headers={"X-API-Key": "other-key-0123456789abc"}).status_code == 200
    assert c.get("/admin/pro/accounts", headers=ADMIN).status_code == 403


def test_setup_checklist_shows_state_without_secrets(app, monkeypatch):
    monkeypatch.setenv("PROADS_META_DATASET_ID", "123")
    monkeypatch.setenv("PROADS_META_TOKEN", "very-secret-token")
    monkeypatch.setenv("PROADS_META_TEST_CODE", "TEST1")
    c = app.test_client()
    r = c.get("/admin/pro/setup", headers=ADMIN)
    items = {x["label"]: x["done"] for x in r.json["items"]}
    assert items["Tracking for your own ads"] and not items["Ad tracking out of test mode"]
    assert not items["Password reset emails"] and items["Website address set"]
    assert "very-secret-token" not in r.get_data(as_text=True)
    assert c.get("/admin/other/setup", headers={"X-API-Key": "other-key-0123456789abc"}).status_code == 403


def test_admin_adds_a_company_and_moves_own_data_into_it(app):
    c = app.test_client()
    c.post("/import/pro", data="id,date,amount,currency\n1,2026-10-01,100,AED\n2,2026-10-02,50,AED\n", headers=ADMIN)
    c.post("/tracking/pro", json={"post_name": "Laser reel", "platform": "instagram", "url": "https://888.ae"}, headers=ADMIN)
    assert c.post("/admin/pro/companies", json={"business": "888 Clinic"}, headers=ADMIN).status_code == 400
    r = c.post("/admin/pro/companies", json={"business": "888 Clinic", "email": "owner@888.ae", "move_data": True},
               headers=ADMIN).json
    assert r["client_id"] == "888-clinic" and r["moved"] == {"sales": 2, "posts": 0, "codes": 1}
    assert c.post("/admin/pro/companies", json={"business": "Other", "email": "owner@888.ae"}, headers=ADMIN).status_code == 409
    st = app.extensions["paysignal"]
    assert st.store.db.execute("SELECT COUNT(*) FROM payments WHERE client_id='pro'").fetchone()[0] == 0
    tok = c.post("/auth/reset", json={"token": link_token(r["link"]), "password": "clinic-password-1"}).json["token"]
    d = c.get("/dashboard/888-clinic?period=365", headers={"X-API-Key": tok}).json
    assert d["has_data"] and d["metrics"][0]["value"] == 150
    acc = c.get("/admin/pro/accounts", headers=ADMIN).json["accounts"]
    assert acc[0]["name"] == "888 Clinic" and acc[0]["orders"] == 2
