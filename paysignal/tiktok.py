"""Send real, paid sales to TikTok through its Events API, so TikTok ads learn who actually buys.

Works like the Meta side: the same verified purchases, the same event id (so a TikTok Pixel on the site and
ProAds never count one sale twice), customer details SHA-256 hashed before they leave the server.

  Purchase              every new paid purchase, valued exactly like the Meta side: what the customer paid, or
                        (if the business chose it) the profit on that order, minus anything already refunded
  CompleteRegistration  ProAds' own sign-ups; StartTrial when that account becomes active (for ProAds' own ads)

TikTok has no refund event, so ProAds protects TikTok's learning another way:
  * a purchase already fully refunded is never sent (TikTok would learn to find more people like that buyer)
  * a partly refunded one is sent at its kept value
  * for a business that refunds a lot (10%+ of sales), each sale waits 48 hours first, so quick refunds and
    chargebacks are caught before TikTok learns from them (TikTok still accepts it: its window is 7 days)
Delivery: a refused batch is retried one event at a time, so one bad sale never blocks the rest; a sale TikTok
refuses on its own is recorded as rejected, a token problem stops sending until it's fixed.

A business connects with two things from TikTok Events Manager: the Pixel ID and an Events API access token
(Pixel > Settings > Generate access token). No TikTok app review is needed for this.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .identity import _sha256, hash_email, normalize_phone
from .models import Payment

log = logging.getLogger("proads.tiktok")

URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"
BATCH = 50
MAX_AGE = timedelta(days=6, hours=20)        # TikTok only takes recent web events: older ones are skipped
PIXEL_RE = re.compile(r"^[A-Z0-9]{10,40}$")


def hash_phone_e164(phone: Optional[str], default_country_code: str = "971") -> Optional[str]:
    """TikTok wants the phone in E.164 (with the +) before hashing."""
    digits = normalize_phone(phone, default_country_code)
    return _sha256("+" + digits) if digits else None


def user_for(email=None, phone=None, external_id=None, ttclid=None, ttp=None, ip=None, ua=None) -> dict:
    return {k: v for k, v in {
        "email": hash_email(email), "phone": hash_phone_e164(phone),
        "external_id": _sha256(str(external_id).strip().lower()) if external_id else None,
        "ttclid": (ttclid or "").strip()[:500] or None, "ttp": (ttp or "").strip()[:200] or None,
        "ip": ip or None, "user_agent": ua or None}.items() if v}


def has_match_key(event: dict) -> bool:
    return any(k in event["user"] for k in ("email", "phone", "external_id", "ttclid", "ttp"))


HIGH_REFUND_RATE = 0.10
HOLD = timedelta(hours=48)


def event_id(p: Payment) -> str:
    """The order number, as TikTok recommends: a TikTok Pixel on the shop's checkout that sends the same order
    number is then counted once, not twice."""
    return str(p.external_id)[:200]


def purchase_event(p: Payment, value: Optional[float] = None) -> dict:
    ev = {"event": "Purchase", "event_time": int(p.occurred_at.timestamp()), "event_id": event_id(p),
          "user": user_for(p.email, p.phone, p.customer_id, p.ttclid, p.ttp, p.client_ip_address, p.client_user_agent),
          "properties": {"value": round(p.amount if value is None else value, 2), "currency": p.currency,
                         "order_id": p.external_id, "content_type": "product"}}
    if p.items:
        ev["properties"]["contents"] = [{"content_id": i.sku or i.name, "content_name": i.name,
                                         "quantity": i.quantity, "price": i.unit_price} for i in p.items[:50]]
        ev["properties"]["num_items"] = sum(i.quantity for i in p.items)
    if p.event_source_url:
        ev["page"] = {"url": p.event_source_url}
    return ev


def send(events: list[dict], pixel_code: str, token: str, http, test_event_code: Optional[str] = None) -> list[dict]:
    """Post in batches. Returns one result per batch: {"ok", "count", "status", "message"}."""
    out = []
    for i in range(0, len(events), BATCH):
        body = {"event_source": "web", "event_source_id": pixel_code, "data": events[i:i + BATCH]}
        if test_event_code:
            body["test_event_code"] = test_event_code
        try:
            r = http.post(URL, json=body, timeout=30, headers={"Access-Token": token, "Content-Type": "application/json"})
            data = r.json() if getattr(r, "content", b"x") else {}
        except Exception as e:                      # network trouble: try again next round
            out.append({"ok": False, "count": len(body["data"]), "status": 0, "message": f"couldn't reach TikTok ({type(e).__name__})"})
            continue
        ok = r.status_code == 200 and data.get("code") == 0
        out.append({"ok": ok, "count": len(body["data"]), "status": r.status_code,
                    "message": data.get("message") or ("" if ok else f"HTTP {r.status_code}")})
    return out


def explain(message: str) -> str:
    m = (message or "").lower()
    if "access token" in m or "token" in m and ("invalid" in m or "expired" in m):
        return "TikTok didn't accept the access token. In TikTok Events Manager, open the Pixel > Settings > Generate access token, and paste the new one."
    if "pixel" in m or "event_source_id" in m:
        return "TikTok doesn't recognise this Pixel ID for that token. Check you copied the Pixel ID of the same Pixel the token belongs to."
    return message or "TikTok refused the events."


class TikTokStore:
    """A business can keep several TikTok Pixels (each with its own token) and pick which one gets its sales.
    Only one is active at a time, so a sale is never counted in two Pixels. What was sent is remembered per Pixel:
    switching to another Pixel sends it the recent sales it hasn't had yet."""
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tiktok_pixels (client_id TEXT NOT NULL, pixel_code TEXT NOT NULL, name TEXT,
        token_enc TEXT NOT NULL, test_code TEXT, added_at INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 0,
        last_sync INTEGER, status TEXT, message TEXT, sent INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (client_id, pixel_code));
    CREATE TABLE IF NOT EXISTS tiktok_delivered (client_id TEXT NOT NULL, pixel_code TEXT NOT NULL, event_id TEXT NOT NULL,
        sent_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'accepted', reason TEXT,
        PRIMARY KEY (client_id, pixel_code, event_id));
    """
    COLS = ("client_id", "pixel_code", "name", "token_enc", "test_code", "added_at", "active", "last_sync", "status", "message", "sent")

    def __init__(self, db):
        self.db = db
        self.db.executescript(self.SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """Earlier versions kept one Pixel per account (tiktok_links / tiktok_sent): carry them over once."""
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tiktok_links" in tables:
            for r in self.db.execute("SELECT client_id, pixel_code, token_enc, test_code, connected_at, last_sync, status,"
                                     " message, sent FROM tiktok_links").fetchall():
                self.db.execute("INSERT OR IGNORE INTO tiktok_pixels (client_id, pixel_code, name, token_enc, test_code, added_at,"
                                " active, last_sync, status, message, sent) VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                                (r[0], r[1], "TikTok Pixel", *r[2:]))
            if "tiktok_sent" in tables:
                cols = [c[1] for c in self.db.execute("PRAGMA table_info(tiktok_sent)")]
                st = "COALESCE(s.status, 'accepted')" if "status" in cols else "'accepted'"
                rs = "s.reason" if "reason" in cols else "NULL"
                self.db.execute(f"INSERT OR IGNORE INTO tiktok_delivered (client_id, pixel_code, event_id, sent_at, status, reason)"
                                f" SELECT s.client_id, l.pixel_code, s.event_id, s.sent_at, {st}, {rs} FROM tiktok_sent s"
                                f" JOIN tiktok_links l ON l.client_id = s.client_id")
                self.db.execute("DROP TABLE tiktok_sent")
            self.db.execute("DROP TABLE tiktok_links")

    def _row(self, r) -> Optional[dict]:
        return dict(zip(self.COLS, r)) if r else None

    def save(self, client_id: str, pixel_code: str, token_enc: str, test_code: Optional[str], name: Optional[str] = None) -> None:
        """Add a Pixel (or update its token) and make it the one that receives the sales."""
        self.db.execute("UPDATE tiktok_pixels SET active=0 WHERE client_id=?", (client_id,))
        self.db.execute("INSERT INTO tiktok_pixels (client_id, pixel_code, name, token_enc, test_code, added_at, active, status)"
                        " VALUES (?,?,?,?,?,?,1,'waiting') ON CONFLICT(client_id, pixel_code) DO UPDATE SET"
                        " name=COALESCE(excluded.name, tiktok_pixels.name), token_enc=excluded.token_enc, test_code=excluded.test_code,"
                        " active=1, status='waiting', message=NULL",
                        (client_id, pixel_code, (name or "").strip()[:80] or None, token_enc, test_code or None, int(time.time())))
        self.db.commit()

    def activate(self, client_id: str, pixel_code: str) -> bool:
        if not self.db.execute("SELECT 1 FROM tiktok_pixels WHERE client_id=? AND pixel_code=?", (client_id, pixel_code)).fetchone():
            return False
        self.db.execute("UPDATE tiktok_pixels SET active = (pixel_code = ?) WHERE client_id=?", (pixel_code, client_id))
        self.db.execute("UPDATE tiktok_pixels SET status='waiting', message=NULL WHERE client_id=? AND pixel_code=?", (client_id, pixel_code))
        self.db.commit()
        return True

    def get(self, client_id: str) -> Optional[dict]:
        """The Pixel that receives the sales now."""
        return self._row(self.db.execute(f"SELECT {', '.join(self.COLS)} FROM tiktok_pixels WHERE client_id=? AND active=1",
                                         (client_id,)).fetchone())

    def pixels(self, client_id: str) -> list[dict]:
        return [self._row(r) for r in self.db.execute(f"SELECT {', '.join(self.COLS)} FROM tiktok_pixels WHERE client_id=?"
                                                      " ORDER BY active DESC, added_at", (client_id,)).fetchall()]

    def all(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT client_id FROM tiktok_pixels WHERE active=1")]

    def remove(self, client_id: str, pixel_code: Optional[str] = None) -> None:
        """Remove one Pixel (or all). If the active one goes, nothing receives sales until another is chosen."""
        if pixel_code:
            self.db.execute("DELETE FROM tiktok_pixels WHERE client_id=? AND pixel_code=?", (client_id, pixel_code))
        else:
            self.db.execute("DELETE FROM tiktok_pixels WHERE client_id=?", (client_id,))
        self.db.commit()

    def was_sent(self, client_id: str, pixel_code: str, event_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM tiktok_delivered WHERE client_id=? AND pixel_code=? AND event_id=?",
                               (client_id, pixel_code, event_id)).fetchone() is not None

    def mark_sent(self, client_id: str, pixel_code: str, event_ids: list[str], status: str = "accepted",
                  reason: Optional[str] = None) -> None:
        now = int(time.time())
        self.db.executemany("INSERT OR IGNORE INTO tiktok_delivered (client_id, pixel_code, event_id, sent_at, status, reason)"
                            " VALUES (?,?,?,?,?,?)", [(client_id, pixel_code, e, now, status, reason) for e in event_ids])
        self.db.commit()

    def result(self, client_id: str, pixel_code: str, ok: bool, message: Optional[str], sent: int = 0) -> None:
        self.db.execute("UPDATE tiktok_pixels SET last_sync=?, status=?, message=?, sent=sent+? WHERE client_id=? AND pixel_code=?",
                        (int(time.time()), "ok" if ok else "error", message, sent, client_id, pixel_code))
        self.db.commit()

    def health(self, client_id: str, pixel_code: Optional[str], days: int = 7) -> dict:
        if not pixel_code:
            return {}
        since = int(time.time()) - days * 86400
        rows = dict(self.db.execute("SELECT status, COUNT(*) FROM tiktok_delivered WHERE client_id=? AND pixel_code=? AND sent_at>=?"
                                    " GROUP BY status", (client_id, pixel_code, since)).fetchall())
        last = self.db.execute("SELECT reason FROM tiktok_delivered WHERE client_id=? AND pixel_code=? AND status='rejected'"
                               " ORDER BY sent_at DESC LIMIT 1", (client_id, pixel_code)).fetchone()
        return {"accepted": rows.get("accepted", 0), "rejected": rows.get("rejected", 0), "refunded_skipped": rows.get("refunded", 0),
                "last_rejection": last[0] if last else None}

    @staticmethod
    def _pub(row: dict) -> dict:
        return {"pixel_code": row["pixel_code"], "name": row["name"] or "TikTok Pixel", "active": bool(row["active"]),
                "test_mode": bool(row["test_code"]), "status": row["status"], "message": row["message"],
                "last_sync": row["last_sync"], "sent": row["sent"]}

    def public(self, client_id: str) -> dict:
        """Never includes a token."""
        rows = self.pixels(client_id)
        act = next((r for r in rows if r["active"]), None)
        out = {"connected": bool(act), "pixels": [self._pub(r) for r in rows]}
        if act:
            out.update(self._pub(act))
            out.update(self.health(client_id, act["pixel_code"]))
        return out


def refund_rate(payments: list[Payment], refunded) -> float:
    """Share of sales money refunded over the last 180 days (the business's own history)."""
    since = datetime.now(timezone.utc) - timedelta(days=180)
    rec = [p for p in payments if p.kind == "purchase" and p.occurred_at >= since]
    total = sum(p.amount for p in rec)
    return (sum(min(refunded(p), p.amount) for p in rec) / total) if total else 0.0


def plan(payments: list[Payment], store: "TikTokStore", client_id: str, refunded, value_of,
         now: Optional[datetime] = None, pixel_code: str = "") -> dict:
    """What to send now: {"send": [(payment, value)], "refunded": [payment], "held": int, "hold_hours": int}."""
    now = now or datetime.now(timezone.utc)
    rate = refund_rate(payments, refunded)
    hold = HOLD if rate >= HIGH_REFUND_RATE else timedelta(0)
    out = {"send": [], "refunded": [], "held": 0, "hold_hours": int(hold.total_seconds() // 3600), "refund_rate": round(rate, 3)}
    for p in payments:
        if p.kind != "purchase" or p.occurred_at < now - MAX_AGE or store.was_sent(client_id, pixel_code, event_id(p)):
            continue
        if p.occurred_at > now - hold:
            out["held"] += 1
            continue
        back = refunded(p)
        if back >= p.amount - 0.005:
            out["refunded"].append(p)              # never teach TikTok to find more buyers like a refunded one
            continue
        v = value_of(p)
        if back:
            v = v * (1 - back / p.amount)           # kept value only
        out["send"].append((p, round(v, 2)))
    return out


def is_auth_problem(message: str) -> bool:
    m = (message or "").lower()
    return any(k in m for k in ("token", "permission", "unauthori", "pixel", "event_source_id", "advertiser"))


def deliver(events: list[dict], pixel_code: str, token: str, http, test_event_code: Optional[str] = None) -> dict:
    """Send; if a batch is refused for something other than the token, retry its events one by one so a single
    bad sale is set aside (rejected) and the rest still reach TikTok. Returns {"sent", "rejected", "stopped"}."""
    sent, rejected, stopped = [], [], None
    for i in range(0, len(events), BATCH):
        chunk = events[i:i + BATCH]
        r = send(chunk, pixel_code, token, http, test_event_code)[0]
        if r["ok"]:
            sent += [e["event_id"] for e in chunk]
            continue
        if r["status"] == 0 or r["status"] >= 500 or r["status"] == 429 or is_auth_problem(r["message"]):
            stopped = r["message"]                 # TikTok down, slow down, or token: try again next round
            break
        for e in chunk:
            one = send([e], pixel_code, token, http, test_event_code)[0]
            if one["ok"]:
                sent.append(e["event_id"])
            elif one["status"] == 0 or one["status"] >= 500 or one["status"] == 429 or is_auth_problem(one["message"]):
                stopped = one["message"]
                break
            else:
                rejected.append((e["event_id"], one["message"]))
        if stopped:
            break
    return {"sent": sent, "rejected": rejected, "stopped": stopped}


# ---------- ProAds' own TikTok tracking (sign-ups), like growth.py does for Meta ----------
def proads_settings() -> dict:
    return {"pixel": os.environ.get("PROADS_TIKTOK_PIXEL", "").strip(), "token": os.environ.get("PROADS_TIKTOK_TOKEN", "").strip(),
            "test_code": os.environ.get("PROADS_TIKTOK_TEST_CODE", "").strip()}


def from_growth(meta_event: dict, ctx: Optional[dict], account: dict) -> Optional[dict]:
    """ProAds' own Meta event -> the TikTok equivalent (only the ones TikTok has a standard event for)."""
    name = {"CompleteRegistration": "CompleteRegistration", "StartTrial": "StartTrial", "Purchase": "Purchase",
            "Subscribe": "Subscribe"}.get(meta_event["event_name"])
    if not name:
        return None
    ctx = ctx or {}
    ev = {"event": name, "event_time": meta_event["event_time"], "event_id": meta_event["event_id"],
          "user": user_for(account.get("email"), account.get("phone"), account.get("client_id"),
                           ctx.get("ttclid"), ctx.get("ttp"), ctx.get("ip"), ctx.get("ua"))}
    cd = meta_event.get("custom_data") or {}
    if "value" in cd:
        ev["properties"] = {"value": cd["value"], "currency": cd.get("currency", "USD")}
    if ctx.get("url"):
        ev["page"] = {"url": ctx["url"]}
    return ev if has_match_key(ev) else None
