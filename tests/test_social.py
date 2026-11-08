import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from paysignal import social
from test_billing import ACCT, app  # noqa: F401  (fixture)


def tok(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return enc({"alg": "RS256"}) + "." + enc(claims) + ".sig"


class R:
    def __init__(self, body, status=200):
        self.b, self.status_code = body, status

    def json(self):
        return self.b


class Provider:
    """Fake Google / Apple / Facebook token endpoints."""
    def __init__(self):
        self.claims = {}
        self.fb_me = {}
        self.seen = []

    def post(self, url, data=None, timeout=None):
        self.seen.append((url, dict(data or {})))
        return R({"id_token": tok(self.claims), "access_token": "x"})

    def get(self, url, params=None, timeout=None):
        if url.endswith("/oauth/access_token"):
            return R({"access_token": "fbtok"})
        return R(self.fb_me)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setenv("FACEBOOK_LOGIN_APP_ID", "fbapp")
    monkeypatch.setenv("FACEBOOK_LOGIN_APP_SECRET", "fbsecret")
    monkeypatch.setenv("PROADS_PUBLIC_URL", "https://pro.ezdevsoft.net")


def start(c, provider, **q):
    r = c.get(f"/auth/oauth/{provider}/start", query_string={"intent": "signup", **q})
    assert r.status_code == 302
    return parse_qs(urlparse(r.headers["Location"]).query)


def google_ok(p, nonce, **kw):
    p.claims = {"iss": "https://accounts.google.com", "aud": "gid.apps.googleusercontent.com", "sub": "g-123",
                "email": "Owner@Glow.ae", "email_verified": True, "name": "Sara Glow", "exp": int(time.time()) + 600,
                "nonce": nonce, **kw}


def test_only_configured_providers_show(app, env, monkeypatch):
    c = app.test_client()
    assert c.get("/public-config").json["sign_in"] == ["google", "facebook"]
    assert c.get("/auth/oauth/apple/start").status_code == 404


def test_google_new_customer_full_flow(app, env):
    state = app.extensions["paysignal"]
    p = Provider()
    state.social_http = p
    c = app.test_client()
    q = start(c, "google", utm=json.dumps({"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "launch-thb"}))
    assert q["redirect_uri"] == ["https://pro.ezdevsoft.net/auth/oauth/google/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email profile"]
    google_ok(p, q["nonce"][0])
    r = c.get("/auth/oauth/google/callback", query_string={"state": q["state"][0], "code": "abc"})
    assert r.status_code == 302 and r.headers["Location"].startswith("/?finish=")
    assert p.seen[0][1]["code_verifier"]                              # PKCE verifier sent
    code = r.headers["Location"].split("finish=")[1].split("#")[0]
    assert c.get("/auth/oauth/pending", query_string={"code": code}).json == {"email": "owner@glow.ae", "name": "Sara Glow", "provider": "google"}
    assert c.post("/auth/oauth/finish", json={"code": code, "business": "Glow Clinic"}).status_code == 400   # terms needed
    d = c.post("/auth/oauth/finish", json={"code": code, "business": "Glow Clinic", "terms": True, "currency": "AED"}).json
    assert d["client_id"] == "glow-clinic" and d["token"]
    assert c.post("/auth/oauth/finish", json={"code": code, "business": "X", "terms": True}).status_code == 404  # single use
    assert state.accounts.attribution("glow-clinic")["utm_campaign"] == "launch-thb"
    st = c.get("/status/glow-clinic", headers={"X-API-Key": d["token"]}).json
    assert st["has_password"] is False
    # next time: straight in, via a one-time code (never a session token in the URL)
    q = start(c, "google")
    google_ok(p, q["nonce"][0])
    r = c.get("/auth/oauth/google/callback", query_string={"state": q["state"][0], "code": "abc"})
    assert r.headers["Location"].startswith("/app#/home?login_code=")
    lc = r.headers["Location"].split("login_code=")[1]
    x = c.post("/auth/oauth/exchange", json={"code": lc}).json
    assert x["client_id"] == "glow-clinic" and c.get("/status/glow-clinic", headers={"X-API-Key": x["token"]}).status_code == 200
    assert c.post("/auth/oauth/exchange", json={"code": lc}).status_code == 401
    # deleting an account without a password: type the email instead
    assert c.post("/account/delete/glow-clinic", headers={"X-API-Key": x["token"]}, json={"confirm_email": "nope@x"}).status_code == 401
    assert c.post("/account/delete/glow-clinic", headers={"X-API-Key": x["token"]}, json={"confirm_email": "owner@glow.ae"}).json["deleted"]
    assert state.social.client_for("google", "g-123") is None


def test_forged_or_replayed_answers_are_refused(app, env):
    state = app.extensions["paysignal"]
    p = Provider()
    state.social_http = p
    c = app.test_client()
    q = start(c, "google")
    google_ok(p, "someone-elses-nonce")
    r = c.get("/auth/oauth/google/callback", query_string={"state": q["state"][0], "code": "abc"})
    assert "oauth_error" in r.headers["Location"]
    # the state was used up by that attempt
    google_ok(p, q["nonce"][0])
    assert "oauth_error" in c.get("/auth/oauth/google/callback", query_string={"state": q["state"][0], "code": "abc"}).headers["Location"]
    q = start(c, "google")
    google_ok(p, q["nonce"][0], aud="another-app")
    assert "oauth_error" in c.get("/auth/oauth/google/callback", query_string={"state": q["state"][0], "code": "abc"}).headers["Location"]
    # a link started in another browser (no binding cookie) is refused
    q = start(c, "google")
    google_ok(p, q["nonce"][0])
    other = app.test_client()
    assert "oauth_error" in other.get("/auth/oauth/google/callback", query_string={"state": q["state"][0], "code": "abc"}).headers["Location"]


def test_links_existing_account_only_with_verified_email(app, env):
    state = app.extensions["paysignal"]
    p = Provider()
    state.social_http = p
    c = app.test_client()
    c.post("/auth/signup", json={**ACCT, "email": "owner@glow.ae"})
    q = start(c, "google")
    google_ok(p, q["nonce"][0], email_verified=False)
    assert "oauth_error" in c.get("/auth/oauth/google/callback", query_string={"state": q["state"][0], "code": "a"}).headers["Location"]
    q = start(c, "google")
    google_ok(p, q["nonce"][0])
    r = c.get("/auth/oauth/google/callback", query_string={"state": q["state"][0], "code": "a"})
    assert "login_code=" in r.headers["Location"] and state.social.client_for("google", "g-123") == "glow-clinic"
    st = c.get("/status/glow-clinic", headers={"X-API-Key": c.post("/auth/oauth/exchange", json={"code": r.headers["Location"].split("login_code=")[1]}).json["token"]}).json
    assert st["has_password"] is True                              # their password still works and is still required to delete


def test_facebook_needs_an_email(app, env):
    state = app.extensions["paysignal"]
    p = Provider()
    state.social_http = p
    c = app.test_client()
    q = start(c, "facebook")
    assert q["scope"] == ["email,public_profile"]
    p.fb_me = {"id": "fb-1", "name": "Sara"}
    assert "verified%20email" in c.get("/auth/oauth/facebook/callback", query_string={"state": q["state"][0], "code": "a"}).headers["Location"]
    q = start(c, "facebook")
    p.fb_me = {"id": "fb-1", "name": "Sara", "email": "sara@glow.ae"}
    assert c.get("/auth/oauth/facebook/callback", query_string={"state": q["state"][0], "code": "a"}).headers["Location"].startswith("/?finish=")


def test_facebook_login_for_business_config(app, env, monkeypatch):
    monkeypatch.setenv("FACEBOOK_LOGIN_CONFIG_ID", "cfg-1")
    q = start(app.test_client(), "facebook")
    assert q["config_id"] == ["cfg-1"] and "scope" not in q


def test_apple_form_post_and_signed_client_secret(app, env, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import jwt
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    for k, v in {"APPLE_CLIENT_ID": "net.ezdevsoft.pro", "APPLE_TEAM_ID": "TEAM123456", "APPLE_KEY_ID": "KEY1234567",
                 "APPLE_PRIVATE_KEY": pem}.items():
        monkeypatch.setenv(k, v)
    secret = social.apple_client_secret()
    claims = jwt.decode(secret, key.public_key(), algorithms=["ES256"], audience="https://appleid.apple.com")
    assert claims["iss"] == "TEAM123456" and claims["sub"] == "net.ezdevsoft.pro"
    assert jwt.get_unverified_header(secret)["kid"] == "KEY1234567"
    state = app.extensions["paysignal"]
    p = Provider()
    state.social_http = p
    c = app.test_client()
    q = start(c, "apple")
    assert q["response_mode"] == ["form_post"]
    p.claims = {"iss": "https://appleid.apple.com", "aud": "net.ezdevsoft.pro", "sub": "a-1", "email": "x@privaterelay.appleid.com",
                "email_verified": "true", "exp": int(time.time()) + 600, "nonce": q["nonce"][0]}
    r = c.post("/auth/oauth/apple/callback", data={"state": q["state"][0], "code": "a",
                                                   "user": json.dumps({"name": {"firstName": "Sara", "lastName": "Ali"}})})
    code = r.headers["Location"].split("finish=")[1].split("#")[0]
    assert c.get("/auth/oauth/pending", query_string={"code": code}).json["name"] == "Sara Ali"
