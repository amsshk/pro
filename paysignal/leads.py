"""Leads from Meta Business Suite (Leads Center export), for businesses that sell in Messenger chats.

The export has no money in it: who wrote, when, from a paid ad or not, the stage (e.g. Purchased) and labels
(including the ad ID the chat came from). ProAds keeps no names: each lead is stored under a hash, with the
date, source, stage, ad IDs and team labels, so the business sees which ads and which staff bring buyers.
An optional average sale price turns buyer counts into an estimated value. Works with the export in English or Thai.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

# column -> header names Meta uses (English, Thai)
COLUMNS = {
    "created": ("created", "created time", "created at", "date created", "สร้างเมื่อ"),
    "name": ("name", "full name", "ชื่อ"),
    "email": ("email", "อีเมล"),
    "source": ("source", "แหล่งที่มา"),
    "form": ("form", "แบบฟอร์ม"),
    "channel": ("channel", "ช่องทาง"),
    "stage": ("stage", "ระยะ"),
    "owner": ("owner", "เจ้าของ"),
    "labels": ("labels", "ป้ายกำกับ"),
    "phone": ("phone", "phone number", "โทรศัพท์"),
    "phone2": ("secondary phone number", "หมายเลขโทรศัพท์รอง"),
    "whatsapp": ("whatsapp number", "หมายเลข whatsapp"),
}
SOURCE = {"เสียค่าใช้จ่าย": "Paid ads", "paid": "Paid ads", "ออร์แกนิก": "Organic", "organic": "Organic",
          "โดยตรง": "Direct", "direct": "Direct"}
STAGE = {"ซื้อแล้ว": "Purchased", "สั่งซื้อแล้ว": "Purchased", "purchased": "Purchased", "won": "Purchased",
         "สร้างคอนเวอร์ชั่นแล้ว": "Converted", "converted": "Converted", "conversion created": "Converted",
         "นัดหมายแล้ว": "Booked", "booked": "Booked", "appointment booked": "Booked",
         "มาใหม่": "New", "ใหม่": "New", "new": "New", "intake": "New", "การรับข้อมูล": "New",
         "มีคุณสมบัติ": "Qualified", "qualified": "Qualified", "ผ่านการคัดกรอง": "Qualified",
         "ติดต่อแล้ว": "Contacted", "contacted": "Contacted", "ทักไม่คุย": "No reply", "no reply": "No reply",
         "lost": "Lost", "ไม่สำเร็จ": "Lost", "spam": "Spam", "สแปม": "Spam"}
# Converted is Meta's own last stage in Leads Center; Purchased is the business's own stage or label
BOUGHT = ("Purchased", "Converted")
# how far along a lead is: a newer export never moves a lead backwards
RANK = {"Spam": 0, "Lost": 1, "No reply": 2, "New": 3, "Contacted": 4, "Qualified": 5, "Booked": 6, "Converted": 7, "Purchased": 8}
# labels Meta or the business use for status rather than for a team member
STATUS_LABELS = {"messenger_ads": None, "การตอบสนองต่อโฆษณา": None, "ad response": None, "ad_response": None,
                 "ซื้อแล้ว": "Purchased", "purchased": "Purchased", "สั่งซื้อแล้ว": "Purchased",
                 "นัดหมายแล้ว": "Booked", "booked": "Booked", "intake": None, "การรับข้อมูล": None,
                 "รอนัดปรึกษา": "Waiting to book", "สมัครงาน": "Job application", "job application": "Job application",
                 "ลูกค้าเก่า": "Returning customer", "returning customer": "Returning customer",
                 "รอจอง": "Waiting to book", "waiting to book": "Waiting to book", "สร้างคอนเวอร์ชั่นแล้ว": "Conversion sent", "conversion created": "Conversion sent",
                 "conversion sent": "Conversion sent"}


def _cols(header: list[str]) -> dict[str, str]:
    low = {h.strip().lower(): h for h in header}
    out = {}
    for key, names in COLUMNS.items():
        for n in names:
            if n in low:
                out[key] = low[n]
                break
    return out


def is_meta_leads(header: list[str]) -> bool:
    c = _cols(header)
    return "created" in c and "stage" in c and ("source" in c or "channel" in c) and ("labels" in c or "owner" in c)


def _date(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    for f in ("%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
              "%Y-%m-%d %H:%M", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.upper() if "%p" in f else s, f).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def parse(text: str) -> tuple[list[dict], list[str]]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return [], ["the file has no leads"]
    c = _cols(list(rows[0].keys()))
    out, errors, bad = [], [], 0
    for r in rows:
        get = lambda k: (r.get(c[k]) or "").strip() if k in c else ""
        when = _date(get("created"))
        if not when:
            bad += 1
            continue
        ads, team, flags = [], [], []
        for t in (x.strip() for x in get("labels").split(",")):
            if not t:
                continue
            if t.lower().startswith("ad_id."):
                ads.append(t[6:])
            elif t.lower() in STATUS_LABELS or t in STATUS_LABELS:
                v = STATUS_LABELS.get(t.lower(), STATUS_LABELS.get(t))
                if v:
                    flags.append(v)
            else:
                team.append(t)
        stage_raw = get("stage")
        stage = STAGE.get(stage_raw.lower(), STAGE.get(stage_raw, stage_raw or "Unknown"))
        if stage not in BOUGHT and "Purchased" in flags:
            stage = "Purchased"
        phone = _digits(get("phone") or get("whatsapp") or get("phone2"))
        email = get("email").lower()
        ident = email or phone or get("name")
        key = hashlib.sha256(f"{when.isoformat()}|{ident}".encode()).hexdigest()[:32]
        src = get("source")
        owner = get("owner")
        out.append({"key": key, "created_at": int(when.timestamp()), "source": SOURCE.get(src.lower(), SOURCE.get(src, src or "Unknown")),
                    "channel": get("channel") or "Unknown", "stage": stage, "ad_ids": ads[:20], "team": team[:5],
                    "owner": "" if owner.lower() in ("", "unassigned") else owner, "flags": sorted(set(flags)),
                    "email_hash": hashlib.sha256(email.encode()).hexdigest() if email else None,
                    "phone_hash": hashlib.sha256(phone.encode()).hexdigest() if phone else None})
    if bad:
        errors.append(f"{bad} row(s) skipped: no readable date")
    return out, errors


class LeadStore:
    def __init__(self, db):
        self.db = db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS leads (client_id TEXT NOT NULL, key TEXT NOT NULL, created_at INTEGER NOT NULL,
            source TEXT, channel TEXT, stage TEXT, ad_ids TEXT, team TEXT, owner TEXT, flags TEXT, email_hash TEXT,
            phone_hash TEXT, PRIMARY KEY (client_id, key));
        CREATE TABLE IF NOT EXISTS lead_settings (client_id TEXT PRIMARY KEY, avg_price REAL);
        """)
        self.db.commit()

    def add(self, client_id: str, leads: list[dict]) -> dict:
        new = 0
        for l in leads:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_id, l["key"], l["created_at"], l["source"], l["channel"], l["stage"], ",".join(l["ad_ids"]),
                 "|".join(l["team"]), l["owner"], ",".join(l["flags"]), l["email_hash"], l["phone_hash"]))
            if cur.rowcount == 1:
                new += 1
            else:                                        # seen before (another export): merge, never move it backwards
                old = self.db.execute("SELECT stage, ad_ids, team, owner, flags FROM leads WHERE client_id=? AND key=?",
                                      (client_id, l["key"])).fetchone()
                stage = l["stage"] if RANK.get(l["stage"], 3) >= RANK.get(old[0], 3) else old[0]
                join = lambda a, b, sep: sep.join(dict.fromkeys([x for x in (a or "").split(sep) if x] + list(b)))
                self.db.execute("UPDATE leads SET stage=?, ad_ids=?, team=?, owner=?, flags=? WHERE client_id=? AND key=?",
                                (stage, join(old[1], l["ad_ids"], ","), join(old[2], l["team"], "|"), l["owner"] or old[3],
                                 join(old[4], l["flags"], ","), client_id, l["key"]))
        self.db.commit()
        return {"leads": len(leads), "new": new, "duplicates": len(leads) - new}

    def count(self, client_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM leads WHERE client_id=?", (client_id,)).fetchone()[0]

    def clear(self, client_id: str) -> None:
        self.db.execute("DELETE FROM leads WHERE client_id=?", (client_id,))
        self.db.commit()

    def avg_price(self, client_id: str) -> Optional[float]:
        r = self.db.execute("SELECT avg_price FROM lead_settings WHERE client_id=?", (client_id,)).fetchone()
        return r[0] if r else None

    def set_avg_price(self, client_id: str, price: Optional[float]) -> None:
        if price is not None and not 0 < float(price) < 10_000_000:
            raise ValueError("Enter the average price of one sale, like 1500.")
        self.db.execute("INSERT INTO lead_settings VALUES (?,?) ON CONFLICT(client_id) DO UPDATE SET avg_price=excluded.avg_price",
                        (client_id, None if price is None else round(float(price), 2)))
        self.db.commit()

    def summary(self, client_id: str) -> dict:
        rows = self.db.execute("SELECT created_at, source, channel, stage, ad_ids, team, owner, flags FROM leads "
                               "WHERE client_id=? ORDER BY created_at", (client_id,)).fetchall()
        price = self.avg_price(client_id)
        if not rows:
            return {"leads": 0, "avg_price": price}
        months: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        ads: Counter = Counter()
        ads_buyers: Counter = Counter()
        team: Counter = Counter()
        stages: Counter = Counter()
        sources: Counter = Counter()
        sent = 0
        bought_total = 0
        rows = [r for r in rows if r[3] != "Spam" and "Job application" not in (r[7] or "")]
        if not rows:
            return {"leads": 0, "avg_price": price}
        for at, source, channel, stage, ad_ids, tm, owner, flags in rows:
            m = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m")
            bought = stage in BOUGHT
            months[m][0] += 1
            months[m][1] += bought
            bought_total += bought
            stages[stage] += 1
            sources[source] += 1
            sent += stage == "Converted" or "Conversion sent" in (flags or "")
            for a in {x for x in (ad_ids or "").split(",") if x}:
                ads[a] += 1
                ads_buyers[a] += bought
            for t in {x for x in (tm or "").split("|") if x} | ({owner} if owner else set()):
                team[t] += bought
        first = datetime.fromtimestamp(rows[0][0], timezone.utc).strftime("%Y-%m")
        last = datetime.fromtimestamp(rows[-1][0], timezone.utc).strftime("%Y-%m")
        # every month from the first to the last lead, so gaps show as zero
        y, mo = int(first[:4]), int(first[5:])
        series = []
        while f"{y:04d}-{mo:02d}" <= last:
            k = f"{y:04d}-{mo:02d}"
            series.append({"month": k, "leads": months[k][0] if k in months else 0, "buyers": months[k][1] if k in months else 0})
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return {"leads": len(rows), "buyers": bought_total, "sent_to_meta": sent, "avg_price": price,
                "value": round(bought_total * price, 2) if price else None,
                "first": rows[0][0], "last": rows[-1][0],
                "sources": [{"source": k, "leads": v} for k, v in sources.most_common()],
                "stages": [{"stage": k, "leads": v} for k, v in stages.most_common()],
                "months": series,
                "rate": round(bought_total / len(rows), 4),
                "funnel": [{"stage": k, "leads": stages[k]} for k in sorted(stages, key=lambda k: -RANK.get(k, 3))],
                "top_ads": [{"ad_id": a, "leads": n, "buyers": ads_buyers[a]} for a, n in
                            sorted(ads.items(), key=lambda x: (-ads_buyers[x[0]], -x[1]))[:10]],
                "no_buyer_ads": [{"ad_id": a, "leads": n} for a, n in ads.most_common() if not ads_buyers[a]][:5],
                "team": [{"label": k, "buyers": v} for k, v in team.most_common(8) if v]}
