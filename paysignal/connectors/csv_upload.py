"""CSV upload -> Payment. Covers cash, bank transfers, WhatsApp sales, any POS export.

Required columns: id, date, amount, currency
Optional: kind (purchase/refund), refund_of, email, phone, customer_id,
          fbclid, ttclid, discount, cost, channel,
          utm_source, utm_medium, utm_campaign, utm_content, promo_code
          (for WhatsApp/cash sales, put the post the customer mentioned in utm_content
           or the code they used in promo_code)
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from ..models import LineItem, Payment

REQUIRED = {"id", "date", "amount", "currency"}


def _date(s: str) -> datetime:
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_csv(text: str, client_id: str) -> tuple[list[Payment], list[str]]:
    """Returns (payments, errors). Bad rows are reported, not silently dropped."""
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.strip().lower() for c in reader.fieldnames or []}
    missing = REQUIRED - cols
    if missing:
        return [], [f"missing columns: {', '.join(sorted(missing))}"]

    payments, errors = [], []
    for n, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        try:
            amount = float(row["amount"])
            cost = row.get("cost")
            payments.append(Payment(
                client_id=client_id, source="csv", external_id=row["id"],
                kind=row.get("kind") or "purchase", amount=amount,
                currency=row["currency"].upper(), occurred_at=_date(row["date"]),
                email=row.get("email") or None, phone=row.get("phone") or None,
                customer_id=row.get("customer_id") or None,
                fbclid=row.get("fbclid") or None, ttclid=row.get("ttclid") or None,
                discount=float(row.get("discount") or 0), refund_of=row.get("refund_of") or None,
                channel=row.get("channel") or "physical_store",
                items=[LineItem("row", "row", 1, amount, float(cost))] if cost else [],
                utm_source=row.get("utm_source") or None, utm_medium=row.get("utm_medium") or None,
                utm_campaign=row.get("utm_campaign") or None, utm_content=row.get("utm_content") or None,
                promo_code=row.get("promo_code") or None,
            ))
        except (ValueError, KeyError) as e:
            errors.append(f"row {n}: {e}")
    return payments, errors
