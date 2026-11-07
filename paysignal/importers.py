"""Import a business's HISTORY on day one, so results come in the first week
instead of after 6 weeks of new data.

  Shopify   Orders > Export (CSV)
  Stripe    Payments > Export (CSV)
  Posts     Meta Business Suite > Insights > Content > Export, or Instagram/TikTok exports

Exports differ between versions and languages, so columns are matched by a list
of known names, and unmatched required columns are reported clearly.
`import_auto` looks at the header and picks the right importer.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import LineItem, Payment
from .organic import Post


def _rows(text: str) -> tuple[list[str], list[dict]]:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in reader.fieldnames or []]
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader]
    return header, rows


def _pick(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n.lower())
        if v:
            return v
    return ""


def _has(header: list[str], *names: str) -> bool:
    low = {h.lower() for h in header}
    return any(n.lower() in low for n in names)


def _money(s: str) -> float:
    s = re.sub(r"[^\d.,\-]", "", s or "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) == 2 else s.replace(",", "")
    return float(s)


_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                 "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                 "%Y/%m/%d %H:%M", "%b %d, %Y %I:%M %p", "%d %b %Y %H:%M")


def _date(s: str, default_tz=timezone.utc) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        for f in _DATE_FORMATS:
            try:
                dt = datetime.strptime(s, f)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=default_tz)


# ---------------- Shopify ----------------

SHOPIFY_KEYS = ("Name", "Financial Status", "Lineitem name")


def import_shopify_orders(text: str, client_id: str, costs: dict[str, float] | None = None,
                          tz=timezone.utc) -> tuple[list[Payment], list[str]]:
    """Shopify's order export has one row per line item; order fields are on the first row."""
    costs = costs or {}
    header, rows = _rows(text)
    if not _has(header, "Name") or not _has(header, "Total"):
        return [], ["not a Shopify order export (needs 'Name' and 'Total' columns)"]
    orders: dict[str, dict] = {}
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        o = orders.setdefault(name, {"first": r, "items": []})
        if r.get("lineitem name"):
            sku = _pick(r, "Lineitem sku") or r["lineitem name"]
            o["items"].append(LineItem(sku, r["lineitem name"], int(_money(_pick(r, "Lineitem quantity")) or 1),
                                       _money(_pick(r, "Lineitem price")), costs.get(sku)))

    payments, errors, skipped = [], [], 0
    for name, o in orders.items():
        r = o["first"]
        status = _pick(r, "Financial Status").lower()
        if status in ("pending", "voided", "authorized", "expired"):
            skipped += 1
            _note_unpaid(_date(_pick(r, "Created at"), tz), _money(_pick(r, "Total")), status, "shopify", name)
            continue
        when = _date(_pick(r, "Paid at", "Created at"), tz)
        if when is None:
            errors.append(f"order {name}: no date")
            continue
        total = _money(_pick(r, "Total"))
        currency = _pick(r, "Currency") or "AED"
        oid = _pick(r, "Id") or name
        payments.append(Payment(
            client_id=client_id, source="shopify", external_id=oid, kind="purchase", amount=total,
            currency=currency, occurred_at=when, email=_pick(r, "Email") or None,
            phone=_pick(r, "Phone", "Billing Phone", "Shipping Phone") or None,
            discount=_money(_pick(r, "Discount Amount")), promo_code=_pick(r, "Discount Code") or None,
            items=o["items"]))
        refunded = _money(_pick(r, "Refunded Amount"))
        if status in ("refunded", "partially_refunded", "partially refunded") and not refunded:
            refunded = total if status == "refunded" else 0.0
        if refunded > 0:
            refund_at = _date(_pick(r, "Cancelled at"), tz) or when + timedelta(days=7)
            payments.append(Payment(
                client_id=client_id, source="shopify", external_id=f"refund:{oid}", kind="refund",
                amount=min(refunded, total), currency=currency, occurred_at=refund_at, refund_of=oid,
                email=_pick(r, "Email") or None))
    if skipped:
        errors.append(f"skipped {skipped} unpaid orders (pending/voided)")
    return payments, errors


# ---------------- unpaid orders (kept for the money-leak detector, never imported as sales) ----------------
import contextvars
_UNPAID: contextvars.ContextVar = contextvars.ContextVar("proads_unpaid", default=None)


def _note_unpaid(when, amount: float, status: str, source: str, ref: str = "") -> None:
    box = _UNPAID.get()
    if box is not None and when is not None and amount and amount > 0:
        box.append({"date": when.date().isoformat(), "amount": round(amount, 2), "status": (status or "unpaid")[:30],
                    "source": source, "ref": str(ref)[:60]})


# ---------------- Stripe ----------------

def import_stripe_payments(text: str, client_id: str, tz=timezone.utc) -> tuple[list[Payment], list[str]]:
    """Stripe dashboard export: amounts are already in normal units (e.g. 249.00)."""
    header, rows = _rows(text)
    if not _has(header, "id") or not _has(header, "Amount"):
        return [], ["not a Stripe payments export (needs 'id' and 'Amount' columns)"]
    payments, errors, skipped = [], [], 0
    for r in rows:
        status = _pick(r, "Status").lower()
        captured = _pick(r, "Captured").lower()
        if status in ("failed", "canceled", "requires_payment_method") or captured == "false":
            skipped += 1
            _note_unpaid(_date(_pick(r, "Created date (UTC)", "Created (UTC)", "Created"), timezone.utc),
                         _money(_pick(r, "Amount")), status or "uncaptured", "stripe", r.get("id", ""))
            continue
        when = _date(_pick(r, "Created date (UTC)", "Created (UTC)", "Created"), timezone.utc)
        if when is None:
            errors.append(f"payment {r.get('id')}: no date")
            continue
        amount, currency = _money(_pick(r, "Amount")), (_pick(r, "Currency") or "aed").upper()
        pid = r["id"]
        payments.append(Payment(
            client_id=client_id, source="stripe", external_id=pid, kind="purchase", amount=amount,
            currency=currency, occurred_at=when,
            email=_pick(r, "Customer Email", "Receipt Email", "Card Name Email") or None,
            phone=_pick(r, "Customer Phone") or None, customer_id=_pick(r, "Customer ID") or None,
            utm_content=_pick(r, "utm_content (metadata)", "utm_content") or None,
            promo_code=_pick(r, "promo_code (metadata)", "promo_code") or None))
        refunded = _money(_pick(r, "Amount Refunded"))
        if refunded > 0:
            payments.append(Payment(
                client_id=client_id, source="stripe", external_id=f"{pid}:refund:{refunded}", kind="refund",
                amount=min(refunded, amount), currency=currency, occurred_at=when + timedelta(days=7),
                refund_of=pid))
    if skipped:
        errors.append(f"skipped {skipped} failed or uncaptured payments")
    return payments, errors


# ---------------- Posts ----------------

POST_COLUMNS = {
    "post_id": ("post id", "post_id", "id", "media id", "video id"),
    "posted_at": ("publish time", "posted_at", "published", "publish date", "date", "created time", "timestamp",
                  "post time", "posted"),
    "caption": ("description", "caption", "title", "message", "post caption", "text"),
    "media_type": ("post type", "media_type", "media type", "media product type", "type", "format"),
    "views": ("views", "plays", "video views", "reach", "impressions", "accounts reached"),
    "likes": ("reactions", "likes", "reactions, comments and shares"),
    "comments": ("comments",),
    "shares": ("shares",),
    "saves": ("saves", "saved"),
    "link_clicks": ("link clicks", "website clicks", "clicks", "profile link clicks"),
    "platform": ("platform",),
}


def import_posts(text: str, platform: str = "instagram", tz=timezone.utc) -> tuple[list[Post], list[str]]:
    header, rows = _rows(text)
    low = {h.lower() for h in header}
    found = {k: next((n for n in names if n in low), None) for k, names in POST_COLUMNS.items()}
    missing = [k for k in ("post_id", "posted_at", "views") if not found[k]]
    if missing:
        return [], [f"could not find columns for: {', '.join(missing)} (header was: {', '.join(header)})"]
    posts, errors = [], []
    for n, r in enumerate(rows, start=2):
        get = lambda k: r.get(found[k], "") if found[k] else ""
        when = _date(get("posted_at"), tz)
        if when is None or not get("post_id"):
            errors.append(f"row {n}: missing id or unreadable date {get('posted_at')!r}")
            continue
        num = lambda k: int(_money(get(k)) or 0)
        mt = get("media_type")
        posts.append(Post(
            get("post_id"), (get("platform") or platform).lower(), when,
            views=num("views"), likes=num("likes"), comments=num("comments"), shares=num("shares"),
            saves=num("saves"), link_clicks=num("link_clicks"), caption=get("caption"), media_type=mt,
            format="unknown", hook="unknown", offer="unknown"))
    return posts, errors


# ---------------- auto ----------------

SIMPLE_CSV_COLUMNS = {"id", "date", "amount", "currency", "kind", "cost", "email", "phone", "customer_id", "fbclid",
                      "ttclid", "discount", "refund_of", "channel", "promo_code", "utm_source", "utm_medium",
                      "utm_campaign", "utm_content", ""}


def detect(text: str) -> str:
    header, _ = _rows(text.split("\n", 1)[0] + "\n")
    if _has(header, "Financial Status") or (_has(header, "Name") and _has(header, "Lineitem name")):
        return "shopify"
    if _has(header, "Amount Refunded") or _has(header, "Created date (UTC)"):
        return "stripe"
    low = {h.lower() for h in header}
    if any(n in low for n in POST_COLUMNS["views"]) and any(n in low for n in POST_COLUMNS["post_id"]):
        return "posts"
    # ProAds' own simple format, only when every column is one it understands; anything else (a status,
    # product or refund column) goes to the general importer, which skips cancelled/failed orders
    if {"id", "date", "amount", "currency"} <= low and low <= SIMPLE_CSV_COLUMNS:
        return "csv"
    if looks_like_sales(header):
        return "sales"
    return "unknown"


# ---------------- any other platform's sales export ----------------

SALES_COLUMNS = {
    "id": ("order id", "order number", "order no", "order #", "order", "id", "transaction id", "reference",
           "reference number", "invoice", "invoice number", "receipt", "receipt number", "payment id", "charge id", "#",
           "رقم الطلب"),
    "date": ("date", "order date", "created at", "created", "created date", "paid at", "payment date", "transaction date",
             "date created", "purchase date", "sale date", "time", "datetime", "تاريخ الطلب", "التاريخ"),
    "amount": ("total", "amount", "grand total", "order total", "total amount", "total price", "paid amount", "amount paid",
               "net amount", "gross amount", "gross sales", "sales", "price", "sum", "value", "الإجمالي", "المبلغ"),
    "currency": ("currency", "currency code", "order currency", "العملة"),
    "email": ("email", "customer email", "e-mail", "billing email", "buyer email", "البريد الإلكتروني"),
    "phone": ("phone", "mobile", "customer phone", "phone number", "billing phone", "whatsapp", "رقم الجوال", "الجوال"),
    "status": ("status", "order status", "payment status", "financial status", "state", "حالة الطلب"),
    "product": ("product", "product name", "item", "item name", "items", "lineitem name", "service", "المنتج"),
    "promo": ("promo code", "discount code", "coupon", "coupon code", "voucher", "code", "promo_code", "كود الخصم"),
    "refunded": ("refunded", "refunded amount", "refund amount", "amount refunded"),
}
_BAD = ("cancel", "fail", "void", "declin", "pending", "unpaid", "abandon", "expired", "ملغ")


def _col(header: list[str], key: str) -> Optional[str]:
    low = {h.strip().lower(): h for h in header}
    for name in SALES_COLUMNS[key]:
        if name in low:
            return low[name]
    for name in SALES_COLUMNS[key]:                 # "Order Total (AED)" style headers
        for h_low, h in low.items():
            if len(name) > 3 and name in h_low:
                return h
    return None


def looks_like_sales(header: list[str]) -> bool:
    return bool(_col(header, "date") and _col(header, "amount"))


def import_generic_sales(text: str, client_id: str, default_currency: str = "AED",
                         tz=timezone.utc) -> tuple[list[Payment], list[str]]:
    """Orders or payments exported from any platform (Salla, Zid, WooCommerce, Tap, a POS…).
    Needs a date and an amount column; everything else is optional."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    header = [h for h in (reader.fieldnames or []) if h]
    cols = {k: _col(header, k) for k in SALES_COLUMNS}
    if not (cols["date"] and cols["amount"]):
        return [], ["this file needs a date column and an amount (or total) column"]
    payments, errors, skipped = [], [], 0
    for n, raw in enumerate(reader, start=2):
        get = lambda k: (raw.get(cols[k]) or "").strip() if cols[k] else ""
        status = get("status").lower()
        if status and any(b in status for b in _BAD):
            skipped += 1
            try:
                _note_unpaid(_date(get("date"), tz), _money(get("amount")), status, "upload", get("id"))
            except ValueError:
                pass
            continue
        when = _date(get("date"), tz)
        if when is None:
            errors.append(f"row {n}: date not understood ({get('date')[:20]})")
            continue
        try:
            amount = _money(get("amount"))
        except ValueError:
            errors.append(f"row {n}: amount not understood")
            continue
        if amount <= 0:
            continue
        currency = (get("currency") or default_currency).upper()[:3]
        oid = get("id") or hashlib.sha1(f"{when.isoformat()}|{amount}|{get('email')}|{get('phone')}|{n}".encode()).hexdigest()[:16]
        product = get("product")
        payments.append(Payment(
            client_id=client_id, source="upload", external_id=oid, kind="purchase", amount=amount, currency=currency,
            occurred_at=when, email=get("email") or None, phone=get("phone") or None,
            promo_code=get("promo") or None,
            items=[LineItem(product, product, 1, amount, None)] if product else []))
        refunded = _money(get("refunded")) if cols["refunded"] else (amount if "refund" in status else 0.0)
        if refunded > 0:
            payments.append(Payment(
                client_id=client_id, source="upload", external_id=f"refund:{oid}", kind="refund",
                amount=min(refunded, amount), currency=currency, occurred_at=when + timedelta(days=7), refund_of=oid))
    if skipped:
        errors.append(f"skipped {skipped} cancelled, failed or unpaid orders")
    return payments, errors


def xlsx_to_csv(data: bytes) -> str:
    """First sheet of an Excel file as CSV text."""
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    out = io.StringIO()
    w = csv.writer(out)
    for row in ws.iter_rows(values_only=True):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        w.writerow(["" if v is None else (v.isoformat(sep=" ") if hasattr(v, "isoformat") else v) for v in row])
    return out.getvalue()


def import_auto(text: str, client_id: str, costs: dict | None = None, platform: str = "instagram",
                default_currency: str = "AED") -> dict:
    """Returns {"kind", "payments", "posts", "errors", "unpaid"}."""
    box: list = []
    token = _UNPAID.set(box)
    try:
        out = _import_auto(text, client_id, costs, platform, default_currency)
    finally:
        _UNPAID.reset(token)
    out["unpaid"] = box
    return out


def _import_auto(text: str, client_id: str, costs: dict | None, platform: str, default_currency: str) -> dict:
    from .connectors import parse_csv
    kind = detect(text)
    if kind == "shopify":
        payments, errors = import_shopify_orders(text, client_id, costs)
        return {"kind": kind, "payments": payments, "posts": [], "errors": errors}
    if kind == "stripe":
        payments, errors = import_stripe_payments(text, client_id)
        return {"kind": kind, "payments": payments, "posts": [], "errors": errors}
    if kind == "posts":
        posts, errors = import_posts(text, platform)
        return {"kind": kind, "payments": [], "posts": posts, "errors": errors}
    if kind == "csv":
        payments, errors = parse_csv(text, client_id)
        return {"kind": kind, "payments": payments, "posts": [], "errors": errors}
    if kind == "sales":
        payments, errors = import_generic_sales(text, client_id, default_currency)
        return {"kind": kind, "payments": payments, "posts": [], "errors": errors}
    return {"kind": "unknown", "payments": [], "posts": [],
            "errors": ["file not recognised: it needs at least a date column and an amount (or total) column"]}
