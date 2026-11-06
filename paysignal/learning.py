"""Anonymous lessons kept across businesses, so the engine starts smarter for every new client.

From each real business's audit we keep only ratios and patterns, never names, emails,
phones, order lists, amounts or post IDs:
  refund rate, repeat-customer rate and their share of profit, the effect of discount codes
  (as a % vs full price), the hours and days people buy, and which post formats/hooks/offers
  brought more money per view (lift).

Benchmarks are only shown once at least MIN_BUSINESSES businesses contributed, so no
single business can be recognised from them.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from typing import Optional

MIN_BUSINESSES = 3


def lesson_from_audit(audit: dict) -> Optional[dict]:
    """The anonymous part of an audit. None when there is too little data to learn from."""
    if not audit or "error" in audit or audit.get("orders", 0) < 20:
        return None
    lesson = {"orders_bucket": "20-99" if audit["orders"] < 100 else "100-999" if audit["orders"] < 1000 else "1000+",
              "refund_rate": audit.get("refund_rate"),
              "peak_sales_hours": audit.get("peak_sales_hours"), "best_sales_days": audit.get("best_sales_days")}
    c = audit.get("customers") or {}
    if c:
        lesson["repeat_rate"] = c.get("repeat_rate")
        lesson["repeat_profit_share"] = c.get("profit_from_repeat_customers")
    codes = [d["vs_full_price"] for d in audit.get("discount_codes") or [] if d.get("orders", 0) >= 10]
    if codes:
        lesson["discount_effect"] = codes                        # e.g. -0.48 = 48% less profit per order
    posts = audit.get("posts") or {}
    pats = {}
    for attr, table in (posts.get("patterns") or {}).items():
        pats[attr] = {k: v["lift"] for k, v in table.items() if v.get("posts", 0) >= 3}
    if any(pats.values()):
        lesson["post_lift"] = pats
    return lesson


class LearningStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS learnings (
        source_hash TEXT PRIMARY KEY, data TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')))"""

    def __init__(self, db, salt: str = "proads"):
        self.db, self.salt = db, salt
        self.db.execute(self.SCHEMA)
        self.db.commit()

    def _key(self, client_id: str) -> str:
        return hashlib.sha256(f"{self.salt}:{client_id}".encode()).hexdigest()[:24]

    def keep(self, client_id: str, audit: dict) -> bool:
        """Save (or refresh) one business's anonymous lesson."""
        lesson = lesson_from_audit(audit)
        if not lesson:
            return False
        self.db.execute("INSERT INTO learnings (source_hash, data) VALUES (?,?) ON CONFLICT(source_hash) DO UPDATE"
                        " SET data=excluded.data, updated_at=datetime('now')", (self._key(client_id), json.dumps(lesson)))
        self.db.commit()
        return True

    def forget(self, client_id: str) -> None:
        self.db.execute("DELETE FROM learnings WHERE source_hash=?", (self._key(client_id),))
        self.db.commit()

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]

    def benchmarks(self) -> Optional[dict]:
        rows = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM learnings").fetchall()]
        if len(rows) < MIN_BUSINESSES:
            return None
        med = lambda xs: round(statistics.median(xs), 3) if xs else None
        vals = lambda k: [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        hours = defaultdict(int)
        days = defaultdict(int)
        for r in rows:
            if r.get("peak_sales_hours"):
                hours[r["peak_sales_hours"]] += 1
            for d in r.get("best_sales_days") or []:
                days[d] += 1
        lifts = defaultdict(list)
        for r in rows:
            for attr, table in (r.get("post_lift") or {}).items():
                for k, lift in table.items():
                    lifts[(attr, k)].append(lift)
        patterns = defaultdict(dict)
        for (attr, k), xs in lifts.items():
            if len(xs) >= MIN_BUSINESSES:                         # only patterns seen in several businesses
                patterns[attr][k] = {"median_lift": med(xs), "businesses": len(xs)}
        disc = [x for r in rows for x in r.get("discount_effect") or []]
        return {
            "businesses": len(rows),
            "median_refund_rate": med(vals("refund_rate")),
            "median_repeat_rate": med(vals("repeat_rate")),
            "median_repeat_profit_share": med(vals("repeat_profit_share")),
            "discount_codes_losing_profit": round(sum(1 for x in disc if x < 0) / len(disc), 2) if disc else None,
            "common_peak_hours": [h for h, _ in sorted(hours.items(), key=lambda kv: -kv[1])[:2]],
            "common_best_days": [d for d, _ in sorted(days.items(), key=lambda kv: -kv[1])[:3]],
            "post_patterns": dict(patterns),
        }
