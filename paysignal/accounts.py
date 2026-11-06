"""Self-serve accounts: sign-up, password login, sessions and a lock against guessing.

Businesses defined in the server settings (PAYSIGNAL_CLIENTS) keep working with
their business ID + access key. Accounts created through the website live in the
database and sign in with email + password; the browser then holds a session
token, never the password.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Optional

SESSION_DAYS = 30
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
    except (ValueError, AttributeError):
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(got.hex(), digest)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:30]
    return s or "business"


def validate_signup(body: dict) -> tuple[dict, list[str]]:
    clean = {
        "name": (body.get("business") or "").strip()[:80],
        "email": (body.get("email") or "").strip().lower()[:120],
        "password": body.get("password") or "",
        "phone": (body.get("phone") or "").strip()[:30],
        "website": (body.get("website") or "").strip()[:200],
        "currency": (body.get("currency") or "AED").strip().upper()[:3],
    }
    errors = []
    if len(clean["name"]) < 2:
        errors.append("Enter your business name")
    if not EMAIL_RE.match(clean["email"]):
        errors.append("Enter a valid email")
    if len(clean["password"]) < 10:
        errors.append("Use a password of at least 10 characters")
    if clean["password"].lower() in (clean["email"], clean["name"].lower()):
        errors.append("Choose a password different from your email and business name")
    if not body.get("terms"):
        errors.append("Please accept the privacy policy")
    if not re.match(r"^[A-Z]{3}$", clean["currency"]):
        errors.append("Unknown currency")
    return clean, errors


class Limiter:
    """Locks a key (e.g. ip+account) after too many failures in a window."""
    def __init__(self, max_failures: int = MAX_FAILURES, window: int = LOCK_SECONDS):
        self.max, self.window = max_failures, window
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        xs = [t for t in self.fails.get(key, []) if now - t < self.window]
        if xs:
            self.fails[key] = xs
        else:
            self.fails.pop(key, None)
        return xs

    def locked_for(self, key: str) -> int:
        """Seconds left on the lock, 0 if free."""
        now = time.time()
        with self.lock:
            xs = self._recent(key, now)
            return int(self.window - (now - xs[0])) + 1 if len(xs) >= self.max else 0

    def fail(self, key: str) -> None:
        with self.lock:
            self.fails.setdefault(key, []).append(time.time())
            if len(self.fails) > 50_000:            # memory guard
                now = time.time()
                for k in list(self.fails)[:10_000]:
                    self._recent(k, now)

    def clear(self, key: str) -> None:
        with self.lock:
            self.fails.pop(key, None)


class AccountStore:
    """Accounts and sessions in the same database as payments."""
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS accounts (
        client_id TEXT PRIMARY KEY,
        email     TEXT NOT NULL UNIQUE,
        name      TEXT NOT NULL,
        pw_hash   TEXT NOT NULL,
        phone     TEXT,
        website   TEXT,
        currency  TEXT NOT NULL DEFAULT 'AED',
        created_at TEXT NOT NULL,
        ads_ctx   TEXT,
        activated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        client_id  TEXT NOT NULL,
        expires    REAL NOT NULL
    );
    """

    def __init__(self, db):
        self.db = db
        self.db.executescript(self.SCHEMA)

    def create(self, clean: dict, reserved: set[str], ads_ctx: str = "") -> str:
        base = slugify(clean["name"])
        cid = base
        while cid in reserved or self.get(cid):
            cid = f"{base}-{secrets.token_hex(2)}"
        self.db.execute("INSERT INTO accounts (client_id, email, name, pw_hash, phone, website, currency,"
                        " created_at, ads_ctx) VALUES (?,?,?,?,?,?,?,datetime('now'),?)",
                        (cid, clean["email"], clean["name"], hash_password(clean["password"]),
                         clean["phone"] or None, clean["website"] or None, clean["currency"], ads_ctx or None))
        self.db.commit()
        return cid

    def get(self, client_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT client_id, email, name, pw_hash, phone, website, currency, created_at,"
                              " ads_ctx, activated_at FROM accounts WHERE client_id=?", (client_id,)).fetchone()
        return self._row(row)

    def by_email(self, email: str) -> Optional[dict]:
        row = self.db.execute("SELECT client_id, email, name, pw_hash, phone, website, currency, created_at,"
                              " ads_ctx, activated_at FROM accounts WHERE email=?", (email.strip().lower(),)).fetchone()
        return self._row(row)

    @staticmethod
    def _row(row) -> Optional[dict]:
        keys = ("client_id", "email", "name", "pw_hash", "phone", "website", "currency", "created_at",
                "ads_ctx", "activated_at")
        return dict(zip(keys, row)) if row else None

    def mark_activated(self, client_id: str) -> bool:
        """True only the first time, so the activation event is sent once."""
        cur = self.db.execute("UPDATE accounts SET activated_at=datetime('now') WHERE client_id=?"
                              " AND activated_at IS NULL", (client_id,))
        self.db.commit()
        return cur.rowcount == 1

    def new_session(self, client_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        self.db.execute("INSERT INTO sessions VALUES (?,?,?)",
                        (token_hash(token), client_id, now + SESSION_DAYS * 86400))
        self.db.commit()
        return token

    def session_client(self, token: str) -> Optional[str]:
        if not token:
            return None
        row = self.db.execute("SELECT client_id, expires FROM sessions WHERE token_hash=?",
                              (token_hash(token),)).fetchone()
        return row[0] if row and row[1] > time.time() else None

    def end_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
        self.db.commit()
