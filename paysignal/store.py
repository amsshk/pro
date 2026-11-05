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
"""


class PaymentStore:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
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

    def totals(self, client_id: str) -> dict:
        gross, = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND kind='purchase'",
                                 (client_id,)).fetchone()
        refunds, = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE client_id=? AND kind='refund'",
                                   (client_id,)).fetchone()
        return {"gross": gross, "refunds": refunds, "net": gross - refunds}
