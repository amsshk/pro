"""Payment store (SQLite for the prototype; same schema moves to Postgres).

- Deduplicates: the same payment arriving twice (webhook retry + CSV) is stored once.
- Links refunds to their original purchase.
- Keeps every client's data separate via client_id.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime

from .identity import customer_key
from .models import LineItem, Payment

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    client_id   TEXT NOT NULL,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    amount      REAL NOT NULL,
    currency    TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    customer_key TEXT,
    refund_of   TEXT,
    data        TEXT NOT NULL,
    sent_to_meta INTEGER DEFAULT 0,
    PRIMARY KEY (client_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_cust ON payments(client_id, customer_key);
CREATE INDEX IF NOT EXISTS idx_refund ON payments(client_id, refund_of);
CREATE TABLE IF NOT EXISTS posts (
    client_id TEXT NOT NULL,
    post_id   TEXT NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (client_id, post_id)
);
CREATE TABLE IF NOT EXISTS promo_codes (
    client_id TEXT NOT NULL,
    code      TEXT NOT NULL,
    platform  TEXT NOT NULL,
    post_id   TEXT NOT NULL,
    link      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (client_id, code)
);
"""


class PaymentStore:
    def __init__(self, path: str = ":memory:"):
        # The server calls the store from several threads; the server serialises
        # access with one lock, so sharing the connection across threads is safe.
        self.db = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")   # survives crashes, faster writes
        self.db.executescript(SCHEMA)

    def add(self, p: Payment) -> bool:
        """Returns True if new, False if it was a duplicate."""
        d = asdict(p)
        d["occurred_at"] = p.occurred_at.isoformat()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO payments (client_id, source, external_id, kind, amount, currency,"
            " occurred_at, customer_key, refund_of, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (p.client_id, p.source, p.external_id, p.kind, p.amount, p.currency, d["occurred_at"],
             customer_key(p.email, p.phone, p.customer_id), p.refund_of, json.dumps(d)),
        )
        self.db.commit()
        return cur.rowcount == 1

    def add_many(self, payments: list[Payment]) -> int:
        return sum(self.add(p) for p in payments)

    @staticmethod
    def _load(row) -> Payment:
        d = json.loads(row[0])
        d["occurred_at"] = datetime.fromisoformat(d["occurred_at"])
        d["items"] = [LineItem(**i) for i in d["items"]]
        return Payment(**d)

    def purchases(self, client_id: str) -> list[Payment]:
        rows = self.db.execute("SELECT data FROM payments WHERE client_id=? AND kind='purchase'"
                               " ORDER BY occurred_at", (client_id,))
        return [self._load(r) for r in rows]

    def refunded_amount(self, client_id: str, purchase_id: str) -> float:
        (total,) = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=?"
                                   " AND kind='refund' AND refund_of=?", (client_id, purchase_id)).fetchone()
        return total

    def customer_history(self, client_id: str, ckey: str, before: datetime) -> list[Payment]:
        rows = self.db.execute("SELECT data FROM payments WHERE client_id=? AND customer_key=?"
                               " AND kind='purchase' AND occurred_at < ? ORDER BY occurred_at",
                               (client_id, ckey, before.isoformat()))
        return [self._load(r) for r in rows]

    def mark_sent(self, client_id: str, source: str, external_id: str) -> None:
        self.db.execute("UPDATE payments SET sent_to_meta=1 WHERE client_id=? AND source=? AND external_id=?",
                        (client_id, source, external_id))
        self.db.commit()

    def unsent(self, client_id: str) -> list[Payment]:
        rows = self.db.execute("SELECT data FROM payments WHERE client_id=? AND kind='purchase'"
                               " AND sent_to_meta=0 ORDER BY occurred_at", (client_id,))
        return [self._load(r) for r in rows]

    # ----- posts (kept across restarts) -----
    def save_posts(self, client_id: str, posts: list) -> None:
        rows = []
        for p in posts:
            d = asdict(p)
            d["posted_at"] = p.posted_at.isoformat()
            rows.append((client_id, p.post_id, json.dumps(d, ensure_ascii=False)))
        self.db.executemany("INSERT OR REPLACE INTO posts (client_id, post_id, data) VALUES (?,?,?)", rows)
        self.db.commit()

    def load_posts(self, client_id: str) -> list:
        from .organic import Post
        out = []
        for (raw,) in self.db.execute("SELECT data FROM posts WHERE client_id=?", (client_id,)):
            d = json.loads(raw)
            d["posted_at"] = datetime.fromisoformat(d["posted_at"])
            out.append(Post(**d))
        return sorted(out, key=lambda p: p.posted_at)

    # ----- tracking codes -----
    def add_promo(self, client_id: str, code: str, platform: str, post_id: str, link: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO promo_codes (client_id, code, platform, post_id, link, created_at)"
                        " VALUES (?,?,?,?,?,datetime('now'))", (client_id, code.upper(), platform, post_id, link))
        self.db.commit()

    def promos(self, client_id: str) -> list[dict]:
        rows = self.db.execute("SELECT code, platform, post_id, link, created_at FROM promo_codes"
                               " WHERE client_id=? ORDER BY created_at DESC", (client_id,))
        return [dict(zip(("code", "platform", "post_id", "link", "created_at"), r)) for r in rows]

    def last_payment_by_source(self, client_id: str) -> dict[str, str]:
        rows = self.db.execute("SELECT source, MAX(occurred_at), COUNT(*) FROM payments WHERE client_id=?"
                               " AND kind='purchase' GROUP BY source", (client_id,))
        return {src: {"last": last, "count": n} for src, last, n in rows}

    def totals(self, client_id: str) -> dict:
        gross, = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND kind='purchase'",
                                 (client_id,)).fetchone()
        refunds, = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND kind='refund'",
                                   (client_id,)).fetchone()
        return {"gross": gross, "refunds": refunds, "net": gross - refunds}
