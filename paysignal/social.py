"""Sign in with Google, Apple or Facebook.

Each provider switches on only when its keys are set in the server settings:
  Google    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  Apple     APPLE_CLIENT_ID (the Services ID), APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_PRIVATE_KEY (the .p8 file's text)
  Facebook  FACEBOOK_LOGIN_APP_ID, FACEBOOK_LOGIN_APP_SECRET (falls back to META_APP_ID / META_APP_SECRET)

Security:
  * state: random, single use, 10 minutes, stored server-side and bound to this browser by a cookie
  * nonce (Google, Apple) and PKCE (Google) so a stolen code or replayed token is useless
  * identity tokens come straight from the provider's token endpoint over TLS (OpenID Connect 3.1.3.7); issuer,
    audience, expiry and nonce are still checked
  * an existing ProAds account is linked by email ONLY when the provider says the email is verified
  * the browser never gets a session token in a URL: it gets a one-time code (2 minutes) to exchange
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

GRAPH = "https://graph.facebook.com/v25.0"
STATE_TTL = 600
CODE_TTL = 120
PENDING_TTL = 1800


class SocialError(Exception):
    pass


def _env(*names) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def configured() -> list[str]:
    out = []
    if _env("GOOGLE_CLIENT_ID") and _env("GOOGLE_CLIENT_SECRET"):
        out.append("google")
    if all(_env(k) for k in ("APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY")):
        out.append("apple")
    # explicit switch: the ProAds Meta app's keys alone don't turn this on (that app may only be set up for ads)
    if _env("FACEBOOK_LOGIN_APP_ID") and _env("FACEBOOK_LOGIN_APP_SECRET", "META_APP_SECRET"):
        out.append("facebook")
    return out


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt_payload(token: str) -> dict:
    """The claims of an ID token received directly from the provider over TLS."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception as e:
        raise SocialError("The sign-in provider sent an unreadable answer.") from e


def apple_client_secret(now: Optional[int] = None) -> str:
    import jwt                                        # PyJWT with cryptography (ES256)
    now = int(now or time.time())
    key = _env("APPLE_PRIVATE_KEY").replace("\\n", "\n")
    return jwt.encode({"iss": _env("APPLE_TEAM_ID"), "iat": now, "exp": now + 3000, "aud": "https://appleid.apple.com",
                       "sub": _env("APPLE_CLIENT_ID")}, key, algorithm="ES256", headers={"kid": _env("APPLE_KEY_ID")})


def auth_url(provider: str, redirect_uri: str, state: str, nonce: str, verifier: str) -> str:
    if provider == "google":
        challenge = b64url(hashlib.sha256(verifier.encode()).digest())
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": _env("GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri, "response_type": "code",
            "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account",
            "code_challenge": challenge, "code_challenge_method": "S256"})
    if provider == "apple":
        return "https://appleid.apple.com/auth/authorize?" + urlencode({
            "client_id": _env("APPLE_CLIENT_ID"), "redirect_uri": redirect_uri, "response_type": "code",
            "scope": "name email", "response_mode": "form_post", "state": state, "nonce": nonce})
    if provider == "facebook":
        return "https://www.facebook.com/v25.0/dialog/oauth?" + urlencode({
            "client_id": _env("FACEBOOK_LOGIN_APP_ID", "META_APP_ID"), "redirect_uri": redirect_uri, "state": state,
            "response_type": "code", "scope": "email,public_profile"})
    raise SocialError("Unknown sign-in provider.")


def identity(provider: str, http, code: str, redirect_uri: str, nonce: str, verifier: str,
             apple_user: Optional[str] = None, now: Optional[int] = None) -> dict:
    """Exchange the code; return {provider, subject, email, email_verified, name}."""
    now = int(now or time.time())
    if provider in ("google", "apple"):
        if provider == "google":
            r = http.post("https://oauth2.googleapis.com/token", data={
                "code": code, "client_id": _env("GOOGLE_CLIENT_ID"), "client_secret": _env("GOOGLE_CLIENT_SECRET"),
                "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier}, timeout=15)
            issuers, aud = ("https://accounts.google.com", "accounts.google.com"), _env("GOOGLE_CLIENT_ID")
        else:
            r = http.post("https://appleid.apple.com/auth/token", data={
                "code": code, "client_id": _env("APPLE_CLIENT_ID"), "client_secret": apple_client_secret(now),
                "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=15)
            issuers, aud = ("https://appleid.apple.com",), _env("APPLE_CLIENT_ID")
        body = r.json() if r is not None else {}
        if getattr(r, "status_code", 500) != 200 or not body.get("id_token"):
            raise SocialError("The sign-in didn't complete. Please try again.")
        c = jwt_payload(body["id_token"])
        auds = c.get("aud") if isinstance(c.get("aud"), list) else [c.get("aud")]
        if c.get("iss") not in issuers or aud not in auds or int(c.get("exp") or 0) < now - 60 or c.get("nonce") != nonce:
            raise SocialError("The sign-in answer didn't match this request. Please try again.")
        verified = c.get("email_verified") in (True, "true")
        name = c.get("name") or ""
        if provider == "apple" and apple_user:            # Apple sends the name only once, outside the token
            try:
                n = json.loads(apple_user).get("name") or {}
                name = " ".join(x for x in (n.get("firstName"), n.get("lastName")) if x)
            except ValueError:
                pass
        return {"provider": provider, "subject": str(c.get("sub") or ""), "email": (c.get("email") or "").lower(),
                "email_verified": verified, "name": name[:80]}
    if provider == "facebook":
        app_id, secret = _env("FACEBOOK_LOGIN_APP_ID", "META_APP_ID"), _env("FACEBOOK_LOGIN_APP_SECRET", "META_APP_SECRET")
        r = http.get(f"{GRAPH}/oauth/access_token", params={"client_id": app_id, "client_secret": secret,
                                                            "redirect_uri": redirect_uri, "code": code}, timeout=15)
        tok = (r.json() or {}).get("access_token") if getattr(r, "status_code", 500) == 200 else None
        if not tok:
            raise SocialError("Facebook didn't complete the sign-in. Please try again.")
        import hmac
        proof = hmac.new(secret.encode(), tok.encode(), hashlib.sha256).hexdigest()
        me = http.get(f"{GRAPH}/me", params={"fields": "id,name,email", "access_token": tok, "appsecret_proof": proof},
                      timeout=15).json() or {}
        if not me.get("id"):
            raise SocialError("Facebook didn't share your profile. Please try again.")
        # Facebook only returns an email address the person has confirmed with Facebook
        return {"provider": "facebook", "subject": str(me["id"]), "email": (me.get("email") or "").lower(),
                "email_verified": bool(me.get("email")), "name": (me.get("name") or "")[:80]}
    raise SocialError("Unknown sign-in provider.")


class SocialStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS social_identities (provider TEXT NOT NULL, subject TEXT NOT NULL, client_id TEXT NOT NULL,
        email TEXT, created_at INTEGER NOT NULL, PRIMARY KEY (provider, subject));
    CREATE TABLE IF NOT EXISTS social_states (state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, nonce TEXT NOT NULL,
        verifier TEXT NOT NULL, bind_hash TEXT NOT NULL, intent TEXT NOT NULL, extra TEXT, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS social_codes (code_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
        expires INTEGER NOT NULL);
    """

    def __init__(self, db):
        self.db = db
        self.db.executescript(self.SCHEMA)
        self.db.commit()

    @staticmethod
    def _h(x: str) -> str:
        return hashlib.sha256(x.encode()).hexdigest()

    def new_state(self, provider: str, intent: str, extra: dict, now: Optional[int] = None) -> dict:
        now = int(now or time.time())
        self.db.execute("DELETE FROM social_states WHERE created < ?", (now - STATE_TTL,))
        st, nonce, verifier, bind = (secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48),
                                     secrets.token_urlsafe(24))
        self.db.execute("INSERT INTO social_states VALUES (?,?,?,?,?,?,?,?)",
                        (self._h(st), provider, nonce, verifier, self._h(bind), intent, json.dumps(extra), now))
        self.db.commit()
        return {"state": st, "nonce": nonce, "verifier": verifier, "bind": bind}

    def take_state(self, state: str, provider: str, bind: Optional[str], now: Optional[int] = None) -> Optional[dict]:
        """Single use: the row is deleted whatever happens next."""
        now = int(now or time.time())
        r = self.db.execute("SELECT provider, nonce, verifier, bind_hash, intent, extra, created FROM social_states"
                            " WHERE state_hash=?", (self._h(state or ""),)).fetchone()
        self.db.execute("DELETE FROM social_states WHERE state_hash=?", (self._h(state or ""),))
        self.db.commit()
        if not r or r[0] != provider or now - r[6] > STATE_TTL:
            return None
        if not bind or not secrets.compare_digest(self._h(bind), r[3]):
            return None
        return {"nonce": r[1], "verifier": r[2], "intent": r[4], "extra": json.loads(r[5] or "{}")}

    def new_code(self, kind: str, payload: dict, ttl: int, now: Optional[int] = None) -> str:
        now = int(now or time.time())
        self.db.execute("DELETE FROM social_codes WHERE expires < ?", (now,))
        code = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO social_codes VALUES (?,?,?,?)", (self._h(code), kind, json.dumps(payload), now + ttl))
        self.db.commit()
        return code

    def take_code(self, code: str, kind: str, now: Optional[int] = None) -> Optional[dict]:
        now = int(now or time.time())
        r = self.db.execute("SELECT kind, payload, expires FROM social_codes WHERE code_hash=?", (self._h(code or ""),)).fetchone()
        self.db.execute("DELETE FROM social_codes WHERE code_hash=?", (self._h(code or ""),))
        self.db.commit()
        if not r or r[0] != kind or r[2] < now:
            return None
        return json.loads(r[1])

    def peek_code(self, code: str, kind: str, now: Optional[int] = None) -> Optional[dict]:
        now = int(now or time.time())
        r = self.db.execute("SELECT kind, payload, expires FROM social_codes WHERE code_hash=?", (self._h(code or ""),)).fetchone()
        return json.loads(r[1]) if r and r[0] == kind and r[2] >= now else None

    def client_for(self, provider: str, subject: str) -> Optional[str]:
        r = self.db.execute("SELECT client_id FROM social_identities WHERE provider=? AND subject=?", (provider, subject)).fetchone()
        return r[0] if r else None

    def link(self, provider: str, subject: str, client_id: str, email: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO social_identities VALUES (?,?,?,?,?)",
                        (provider, subject, client_id, email, int(time.time())))
        self.db.commit()

    def providers_of(self, client_id: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT provider FROM social_identities WHERE client_id=?", (client_id,))]

    def forget(self, client_id: str) -> None:
        self.db.execute("DELETE FROM social_identities WHERE client_id=?", (client_id,))
        self.db.commit()
