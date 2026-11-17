"""Follow-ups (Premium): the cheapest purchases are from people who almost bought or already bought.

Three lists, each message sent by the shop from its OWN WhatsApp with one tap (a wa.me link with the text filled
in), so there is no messaging account to set up and no bulk sending:
- Recover checkouts: Shopify checkouts started but not paid (from the checkouts/create and update webhooks), 1 hour
  to 7 days old, with a phone number.
- Ask for a review: paid orders 5 to 30 days old, once per customer, linking to the shop's review page.
- Win back: customers whose last paid order was N days ago (default 60) and who haven't bought since, once per
  customer per cycle, with an optional code.

Recovered = a paid order from the same email or phone within 7 days (checkouts) or 14 days (win-back) after the
shop tapped Send. Sales that came back without a message are shown apart, so the number isn't inflated.
Contact details are stored encrypted; the checkout link too. Nothing is sent by ProAds itself.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import quote

from .identity import normalize_email, normalize_phone

HOUR, DAY = 3600, 86400
RECOVER_WINDOW = 7 * DAY
WINBACK_WINDOW = 14 * DAY
DEFAULTS = {
    "checkout": "Hi {name}, this is {shop}. You left {items} in your cart. Your order is saved here: {link} "
                "Any questions about it? Just reply here.",
    "review": "Hi {name}, thank you for your order from {shop}! If you're happy with it, a short review would mean a lot: {link}",
    "winback": "Hi {name}, it's {shop}. It's been a while since your last order. {code_line}Shop again here: {link}",
}
TYPES = tuple(DEFAULTS)


def _h(x: Optional[str]) -> Optional[str]:
    return hashlib.sha256(x.encode()).hexdigest() if x else None


def wa_link(phone: Optional[str], text: str, default_cc: str = "971") -> Optional[str]:
    p = normalize_phone(phone, default_cc)
    return f"https://wa.me/{p}?text={quote(text)}" if p and len(p) >= 8 else None


def fill(template: str, **v) -> str:
    v.setdefault("name", "")
    if v.get("code"):
        v["code_line"] = f"Here's {v['code']} for you as a thank-you. "
    else:
        v["code_line"] = ""
    out = template
    for k, val in v.items():
        out = out.replace("{" + k + "}", str(val or ""))
    return " ".join(out.replace("Hi ,", "Hi,").split())


class FollowupStore:
    def __init__(self, db, encrypt: Callable[[str], str], decrypt: Callable[[str], str]):
        self.db, self.enc, self.dec = db, encrypt, decrypt
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS checkouts (client_id TEXT NOT NULL, checkout_id TEXT NOT NULL, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, completed_at INTEGER, total REAL, currency TEXT, items TEXT, name_enc TEXT,
            phone_enc TEXT, email_hash TEXT, phone_hash TEXT, url_enc TEXT, PRIMARY KEY (client_id, checkout_id));
        CREATE TABLE IF NOT EXISTS followup_actions (client_id TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
            action TEXT NOT NULL, at INTEGER NOT NULL, email_hash TEXT, phone_hash TEXT, PRIMARY KEY (client_id, kind, ref));
        CREATE TABLE IF NOT EXISTS followup_settings (client_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)
        self.db.commit()

    # ---------- settings ----------
    def settings(self, client_id: str) -> dict:
        r = self.db.execute("SELECT data FROM followup_settings WHERE client_id=?", (client_id,)).fetchone()
        d = json.loads(r[0]) if r else {}
        return {"templates": {k: (d.get("templates") or {}).get(k) or DEFAULTS[k] for k in TYPES},
                "review_link": d.get("review_link") or "", "shop_link": d.get("shop_link") or "",
                "winback_days": int(d.get("winback_days") or 60), "winback_code": d.get("winback_code") or ""}

    def save_settings(self, client_id: str, body: dict) -> dict:
        cur = self.settings(client_id)
        t = body.get("templates") or {}
        for k in TYPES:
            if k in t:
                v = str(t[k] or "").strip()[:600]
                cur["templates"][k] = v or DEFAULTS[k]
        for k in ("review_link", "shop_link"):
            if k in body:
                v = str(body[k] or "").strip()[:300]
                if v and not v.startswith(("https://", "http://")):
                    raise ValueError("Links must start with https://")
                cur[k] = v
        if "winback_days" in body:
            d = int(body["winback_days"])
            if not 14 <= d <= 365:
                raise ValueError("Win-back days must be between 14 and 365.")
            cur["winback_days"] = d
        if "winback_code" in body:
            cur["winback_code"] = str(body["winback_code"] or "").strip()[:40]
        self.db.execute("INSERT INTO followup_settings VALUES (?,?) ON CONFLICT(client_id) DO UPDATE SET data=excluded.data",
                        (client_id, json.dumps(cur)))
        self.db.commit()
        return cur

    # ---------- checkouts from Shopify ----------
    def checkout(self, client_id: str, c: dict) -> bool:
        cid = str(c.get("token") or c.get("id") or "")
        if not cid:
            return False
        def ts(v):
            try:
                return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()) if v else None
            except ValueError:
                return None
        cust = c.get("customer") or {}
        addr = c.get("shipping_address") or c.get("billing_address") or {}
        phone = c.get("phone") or addr.get("phone") or cust.get("phone")
        email = normalize_email(c.get("email") or cust.get("email"))
        name = (cust.get("first_name") or addr.get("first_name") or "").strip()[:40]
        items = ", ".join(f"{(li.get('quantity') or 1)}× {li.get('title') or li.get('name') or 'item'}"
                          for li in (c.get("line_items") or [])[:4])[:200]
        url = c.get("abandoned_checkout_url") or ""
        try:
            total = float(c.get("total_price") or c.get("subtotal_price") or 0)
        except (TypeError, ValueError):
            total = 0.0
        created = ts(c.get("created_at")) or int(time.time())
        enc = lambda v: self.enc(v) if v else None
        self.db.execute(
            "INSERT INTO checkouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_id, checkout_id) DO UPDATE SET "
            "updated_at=excluded.updated_at, completed_at=COALESCE(excluded.completed_at, checkouts.completed_at), "
            "total=excluded.total, items=excluded.items, name_enc=COALESCE(excluded.name_enc, checkouts.name_enc), "
            "phone_enc=COALESCE(excluded.phone_enc, checkouts.phone_enc), email_hash=COALESCE(excluded.email_hash, checkouts.email_hash), "
            "phone_hash=COALESCE(excluded.phone_hash, checkouts.phone_hash), url_enc=COALESCE(excluded.url_enc, checkouts.url_enc)",
            (client_id, cid, created, ts(c.get("updated_at")) or created, ts(c.get("completed_at")), total,
             (c.get("currency") or c.get("presentment_currency") or "").upper()[:3], items, enc(name), enc(phone),
             _h(email), _h(normalize_phone(phone)), enc(url)))
        self.db.commit()
        return True

    def mark(self, client_id: str, kind: str, ref: str, action: str, email_hash=None, phone_hash=None) -> None:
        if kind not in TYPES or action not in ("sent", "dismissed"):
            raise ValueError("unknown follow-up")
        self.db.execute("INSERT INTO followup_actions VALUES (?,?,?,?,?,?,?) ON CONFLICT(client_id, kind, ref) DO UPDATE SET "
                        "action=excluded.action, at=excluded.at", (client_id, kind, ref, action, int(time.time()), email_hash, phone_hash))
        self.db.commit()

    def actions(self, client_id: str) -> dict:
        return {(k, r): {"action": a, "at": at, "email_hash": e, "phone_hash": p} for k, r, a, at, e, p in self.db.execute(
            "SELECT kind, ref, action, at, email_hash, phone_hash FROM followup_actions WHERE client_id=?", (client_id,))}

    def forget(self, client_id: str) -> None:
        for t in ("checkouts", "followup_actions", "followup_settings"):
            self.db.execute(f"DELETE FROM {t} WHERE client_id=?", (client_id,))
        self.db.commit()

    # ---------- the lists ----------
    def build(self, client_id: str, purchases: list, refunded: Callable, shop_name: str, default_cc: str = "971",
              now: Optional[float] = None, limit: int = 100) -> dict:
        now = now or time.time()
        st = self.settings(client_id)
        acts = self.actions(client_id)
        # paid orders by customer key (email or phone hash)
        buys: dict[str, list[tuple[int, float]]] = defaultdict(list)
        cust: dict[str, dict] = {}
        for p in purchases:
            kept = p.amount - refunded(p)
            if kept <= 0.005:
                continue
            at = int(p.occurred_at.timestamp())
            eh, ph = _h(normalize_email(p.email)), _h(normalize_phone(p.phone, default_cc))
            for k in filter(None, (eh, ph)):
                buys[k].append((at, kept))
            key = eh or ph
            if key:
                c = cust.setdefault(key, {"first": at, "last": 0, "orders": 0, "phone": None, "eh": eh, "ph": ph, "last_ref": None})
                c["orders"] += 1
                if at >= c["last"]:
                    c["last"], c["last_ref"] = at, p.external_id
                    c["phone"] = p.phone or c["phone"]
                    c["eh"], c["ph"] = eh or c["eh"], ph or c["ph"]
        def bought_after(eh, ph, t0, t1):
            got = [(a, v) for k in filter(None, (eh, ph)) for a, v in buys.get(k, []) if t0 < a <= t1]
            return (len({a for a, _ in got}), round(sum(v for _, v in {a: v for a, v in got}.items()), 2)) if got else (0, 0.0)

        out = {k: [] for k in TYPES}
        stats = {k: {"sent": 0, "recovered": 0, "money": 0.0} for k in TYPES}
        stats["checkout"]["came_back_alone"] = 0

        # checkouts
        for (cid, created, completed, total, cur, items, name_e, phone_e, eh, ph, url_e) in self.db.execute(
                "SELECT checkout_id, created_at, completed_at, total, currency, items, name_enc, phone_enc, email_hash, phone_hash, url_enc "
                "FROM checkouts WHERE client_id=? AND created_at>=? ORDER BY created_at DESC", (client_id, int(now - 30 * DAY))):
            a = acts.get(("checkout", cid))
            if a and a["action"] == "sent":
                stats["checkout"]["sent"] += 1
                n, money = bought_after(eh, ph, a["at"], a["at"] + RECOVER_WINDOW)
                if n:
                    stats["checkout"]["recovered"] += 1
                    stats["checkout"]["money"] += money
                continue
            paid_later = bought_after(eh, ph, created - HOUR, now + DAY)[0]
            if completed or paid_later:
                if not a:
                    stats["checkout"]["came_back_alone"] += int(bool(paid_later and not completed))
                continue
            if a or not (HOUR <= now - created <= 7 * DAY) or not phone_e:
                continue
            name = self.dec(name_e) if name_e else ""
            text = fill(st["templates"]["checkout"], name=name, shop=shop_name, items=items or "your items",
                        link=self.dec(url_e) if url_e else st["shop_link"])
            out["checkout"].append({"ref": cid, "name": name or "Customer", "at": created, "amount": total, "currency": cur,
                                    "items": items, "wa": wa_link(self.dec(phone_e), text, default_cc), "text": text})

        # reviews and win-back, from paid orders
        for key, c in cust.items():
            phone = c["phone"]
            if not phone:
                continue
            age = now - c["last"]
            ref = c["last_ref"]
            r = acts.get(("review", ref))
            if r and r["action"] == "sent":
                stats["review"]["sent"] += 1
            elif not r and 5 * DAY <= age <= 30 * DAY:
                text = fill(st["templates"]["review"], shop=shop_name, link=st["review_link"])
                out["review"].append({"ref": ref, "name": "Customer", "at": c["last"], "orders": c["orders"],
                                      "wa": wa_link(phone, text, default_cc), "text": text})
            w = acts.get(("winback", ref))
            if w and w["action"] == "sent":
                stats["winback"]["sent"] += 1
                n, money = bought_after(c["eh"], c["ph"], w["at"], w["at"] + WINBACK_WINDOW)
                if n:
                    stats["winback"]["recovered"] += 1
                    stats["winback"]["money"] += money
            elif not w and st["winback_days"] * DAY <= age <= (st["winback_days"] + 60) * DAY:
                text = fill(st["templates"]["winback"], shop=shop_name, link=st["shop_link"], code=st["winback_code"])
                out["winback"].append({"ref": ref, "name": "Customer", "at": c["last"], "orders": c["orders"],
                                       "wa": wa_link(phone, text, default_cc), "text": text})
        out["review"].sort(key=lambda x: -x["at"])
        out["winback"].sort(key=lambda x: -x["orders"])
        needs = {"checkout": None, "review": "review_link", "winback": "shop_link"}
        for k in TYPES:
            stats[k]["waiting"] = len([x for x in out[k] if x["wa"]])     # counted even before the links are set
            out[k] = [x for x in out[k] if x["wa"]][:limit] if not needs[k] or st[needs[k]] else []
            stats[k]["money"] = round(stats[k]["money"], 2)
        return {"lists": out, "stats": stats, "settings": st,
                "checkouts_seen": self.db.execute("SELECT COUNT(*) FROM checkouts WHERE client_id=?", (client_id,)).fetchone()[0]}
