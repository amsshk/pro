"""Partner (affiliate) program: people who bring paying clients to ProAds earn a share of what those clients pay.

- Each partner has a code and a link (https://<site>/?ref=CODE). The landing page remembers the code for 90 days, so a
  visitor who signs up days later is still credited (first partner wins).
- Commission: a percentage of each subscription or add-on payment the client makes in their first N months, after
  refunds and chargebacks. ProAds' ad-spend fees don't count.
- Each partner has a private dashboard link (secret token): clicks, sign-ups, paying clients, earned, paid, owed.
  It shows no client names or emails.
"""
from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

MONTH = 30 * 86400
COUNTED = ("first", "renewal", "addon")


def clean_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9_-]", "", str(code or "").upper())[:24]


class PartnerStore:
    def __init__(self, db):
        self.db = db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS partners (code TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, percent REAL NOT NULL,
            months INTEGER NOT NULL, token TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS partner_clicks (code TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (code, day));
        CREATE TABLE IF NOT EXISTS partner_referrals (client_id TEXT PRIMARY KEY, code TEXT NOT NULL, at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS partner_payouts (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, amount REAL NOT NULL,
            currency TEXT NOT NULL, note TEXT, at INTEGER NOT NULL);
        """)
        self.db.commit()

    COLS = ("code", "name", "email", "percent", "months", "token", "active", "created_at")

    def get(self, code: str) -> Optional[dict]:
        r = self.db.execute("SELECT " + ",".join(self.COLS) + " FROM partners WHERE code=?", (clean_code(code),)).fetchone()
        return dict(zip(self.COLS, r)) if r else None

    def by_token(self, token: str) -> Optional[dict]:
        r = self.db.execute("SELECT " + ",".join(self.COLS) + " FROM partners WHERE token=?", (str(token or ""),)).fetchone()
        return dict(zip(self.COLS, r)) if r else None

    def list(self) -> list[dict]:
        return [dict(zip(self.COLS, r)) for r in self.db.execute("SELECT " + ",".join(self.COLS) + " FROM partners ORDER BY created_at DESC")]

    def save(self, code: str, name: str, email: str = "", percent: float = 20, months: int = 12, active: bool = True) -> dict:
        code = clean_code(code)
        if len(code) < 3:
            raise ValueError("Use a code of at least 3 letters or numbers, like CALEB or AHMED10.")
        if not (name or "").strip():
            raise ValueError("Add the partner's name.")
        if not 1 <= float(percent) <= 60:
            raise ValueError("Commission must be between 1% and 60%.")
        if not 1 <= int(months) <= 60:
            raise ValueError("Months must be between 1 and 60.")
        if self.get(code):
            self.db.execute("UPDATE partners SET name=?, email=?, percent=?, months=?, active=? WHERE code=?",
                            (name.strip()[:80], (email or "").strip()[:120], float(percent), int(months), int(active), code))
        else:
            self.db.execute("INSERT INTO partners VALUES (?,?,?,?,?,?,?,?)", (code, name.strip()[:80], (email or "").strip()[:120],
                            float(percent), int(months), secrets.token_urlsafe(18), int(active), int(time.time())))
        self.db.commit()
        return self.get(code)

    def click(self, code: str) -> bool:
        p = self.get(code)
        if not p or not p["active"]:
            return False
        day = datetime.now(timezone.utc).date().isoformat()
        self.db.execute("INSERT INTO partner_clicks VALUES (?,?,1) ON CONFLICT(code, day) DO UPDATE SET n=n+1", (p["code"], day))
        self.db.commit()
        return True

    def refer(self, client_id: str, code: str) -> bool:
        """Credit a new account to a partner (once; the first partner keeps it)."""
        p = self.get(code) if code else None
        if not p or not p["active"]:
            return False
        cur = self.db.execute("INSERT OR IGNORE INTO partner_referrals VALUES (?,?,?)", (client_id, p["code"], int(time.time())))
        self.db.commit()
        return cur.rowcount == 1

    def partner_of(self, client_id: str) -> Optional[str]:
        r = self.db.execute("SELECT code FROM partner_referrals WHERE client_id=?", (client_id,)).fetchone()
        return r[0] if r else None

    def pay(self, code: str, amount: float, currency: str = "AED", note: str = "") -> None:
        if amount <= 0:
            raise ValueError("Enter the amount you paid.")
        self.db.execute("INSERT INTO partner_payouts (code, amount, currency, note, at) VALUES (?,?,?,?,?)",
                        (clean_code(code), round(float(amount), 2), currency.upper()[:3], (note or "")[:200], int(time.time())))
        self.db.commit()

    def report(self, code: str, revenue_rows: list[dict], active_client=lambda cid: False) -> dict:
        """What one partner brought and earned. revenue_rows: ProAds' own paid invoices (proads_revenue)."""
        p = self.get(code)
        clients = [r[0] for r in self.db.execute("SELECT client_id FROM partner_referrals WHERE code=?", (p["code"],))]
        mine = set(clients)
        first_paid: dict[str, int] = {}
        for r in revenue_rows:
            if r["client_id"] in mine and r["kind"] in COUNTED:
                first_paid[r["client_id"]] = min(first_paid.get(r["client_id"], r["paid_at"]), r["paid_at"])
        earned, revenue, months_rows = 0.0, 0.0, {}
        currency = "AED"
        for r in revenue_rows:
            cid = r["client_id"]
            if cid not in mine or r["kind"] not in COUNTED or r["paid_at"] > first_paid[cid] + p["months"] * MONTH:
                continue
            kept = max(r["amount"] - (r.get("refunded") or 0) - (r.get("chargeback") or 0), 0)
            revenue += kept
            earned += kept * p["percent"] / 100
            currency = r["currency"] or currency
            m = datetime.fromtimestamp(r["paid_at"], timezone.utc).strftime("%Y-%m")
            months_rows[m] = round(months_rows.get(m, 0) + kept * p["percent"] / 100, 2)
        paid = sum(r[0] for r in self.db.execute("SELECT amount FROM partner_payouts WHERE code=?", (p["code"],)))
        clicks = self.db.execute("SELECT COALESCE(SUM(n),0) FROM partner_clicks WHERE code=?", (p["code"],)).fetchone()[0]
        payouts = [{"amount": r[0], "currency": r[1], "note": r[2], "at": r[3]} for r in
                   self.db.execute("SELECT amount, currency, note, at FROM partner_payouts WHERE code=? ORDER BY at DESC", (p["code"],))]
        return {"code": p["code"], "name": p["name"], "percent": p["percent"], "months": p["months"], "active": bool(p["active"]),
                "clicks": int(clicks), "signups": len(clients), "paying": sum(1 for c in clients if active_client(c)),
                "paid_ever": len(first_paid), "revenue": round(revenue, 2), "earned": round(earned, 2), "paid": round(paid, 2),
                "owed": round(max(earned - paid, 0), 2), "currency": currency,
                "by_month": [{"month": k, "earned": v} for k, v in sorted(months_rows.items())], "payouts": payouts}
