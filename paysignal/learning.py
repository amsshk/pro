"""Anonymous lessons kept across businesses, so the engine starts smarter for every new client.

From each real business's audit we keep only ratios and patterns, never names, emails,
phones, order lists, amounts or post IDs:
  refund rate, repeat-customer rate and their share of profit, the effect of discount codes
  (as a % vs full price), the hours and days people buy, and which post formats/hooks/offers
  brought more money per view (lift).

Benchmarks are only shown once at least MIN_BUSINESSES businesses contributed (and each pattern
only once that many businesses share it); only medians are shared, so no
single business can be recognised from them.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from typing import Optional

MIN_BUSINESSES = 5


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


def _coarse(x: Optional[float], step: float) -> Optional[float]:
    """Round to a coarse step so one business joining or leaving rarely moves a benchmark (no differencing)."""
    return None if x is None else round(round(x / step) * step, 3)


class LearningStore:
    SCHEMA = """CREATE TABLE IF NOT EXISTS learnings (
        source_hash TEXT PRIMARY KEY, data TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')))"""
    OPTOUT = "CREATE TABLE IF NOT EXISTS learning_optout (source_hash TEXT PRIMARY KEY)"

    def __init__(self, db, salt: str = "proads"):
        self.db, self.salt = db, salt
        self.db.execute(self.SCHEMA)
        self.db.execute(self.OPTOUT)
        self.db.commit()

    def _key(self, client_id: str) -> str:
        return hashlib.sha256(f"{self.salt}:{client_id}".encode()).hexdigest()[:24]

    def sharing(self, client_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM learning_optout WHERE source_hash=?", (self._key(client_id),)).fetchone() is None

    def set_sharing(self, client_id: str, share: bool) -> None:
        """Opting out also removes everything this business already contributed."""
        if share:
            self.db.execute("DELETE FROM learning_optout WHERE source_hash=?", (self._key(client_id),))
            self.db.commit()
        else:
            self.db.execute("INSERT OR IGNORE INTO learning_optout (source_hash) VALUES (?)", (self._key(client_id),))
            self.db.commit()
            self.forget(client_id)
            self.forget(client_id + ":archived")

    def keep(self, client_id: str, audit: dict) -> bool:
        """Save (or refresh) one business's anonymous lesson."""
        base = client_id.split(":archived")[0]
        if not self.sharing(base):
            return False
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
        """Only what at least MIN_BUSINESSES businesses share, coarsely rounded. Rare values are left out."""
        rows = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM learnings").fetchall()]
        if len(rows) < MIN_BUSINESSES:
            return None
        enough = lambda xs: len(xs) >= MIN_BUSINESSES
        med = lambda xs, step=0.05: _coarse(statistics.median(xs), step) if enough(xs) else None
        vals = lambda k: [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        hours = defaultdict(int)
        days = defaultdict(int)
        for r in rows:
            if r.get("peak_sales_hours"):
                hours[r["peak_sales_hours"]] += 1
            for d in set(r.get("best_sales_days") or []):
                days[d] += 1
        lifts = defaultdict(list)
        for r in rows:
            for attr, table in (r.get("post_lift") or {}).items():
                for k, lift in table.items():
                    lifts[(attr, k)].append(lift)
        patterns = defaultdict(dict)
        for (attr, k), xs in lifts.items():
            if enough(xs):                                        # only patterns seen in several businesses
                patterns[attr][k] = {"median_lift": med(xs, 0.1)}
        disc_rows = [r["discount_effect"] for r in rows if r.get("discount_effect")]
        disc = [x for xs in disc_rows for x in xs]
        common = lambda d: [k for k, n in sorted(d.items(), key=lambda kv: -kv[1]) if n >= MIN_BUSINESSES]
        return {
            "businesses": "5+" if len(rows) < 10 else "10+" if len(rows) < 50 else "50+",
            "median_refund_rate": med(vals("refund_rate")),
            "median_repeat_rate": med(vals("repeat_rate")),
            "median_repeat_profit_share": med(vals("repeat_profit_share")),
            "discount_codes_losing_profit": _coarse(sum(1 for x in disc if x < 0) / len(disc), 0.1) if enough(disc_rows) else None,
            "common_peak_hours": common(hours)[:2],
            "common_best_days": common(days)[:3],
            "post_patterns": dict(patterns),
        }
