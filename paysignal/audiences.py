"""Buyer lists for Meta and TikTok customer-list audiences, built from verified payments.

Only paid orders count, and money refunded is taken off, so a customer whose orders were all refunded is left
out. Contact details are normalised and SHA-256 hashed here: the files never hold a readable email or phone.
Uses: exclude people who already bought (so ads reach new buyers), retarget them with a new product, and,
once the list is big enough, seed a lookalike (Meta can weight it by each buyer's value).
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Optional

from . import tiktok
from .identity import hash_email, hash_phone, normalize_email, normalize_phone

# The platforms' own floors (they can change): Meta needs about 100 matched people for a customer list;
# TikTok and Meta lookalikes work best from about 1,000.
META_MIN, LOOKALIKE_MIN = 100, 1000


def buyers(store, client_id: str, default_cc: str = "971") -> list[dict]:
    """One row per real customer: hashed email/phone and the money they kept with the business."""
    people: dict[str, dict] = {}
    alias: dict[str, str] = {}
    for p in store.purchases(client_id):
        kept = p.amount - store.refunded_amount(client_id, p.external_id)
        email, phone = normalize_email(p.email), normalize_phone(p.phone, default_cc)
        if not email and not phone:
            continue
        key = (alias.get(f"e:{email}") if email else None) or (alias.get(f"p:{phone}") if phone else None) \
            or (f"e:{email}" if email else f"p:{phone}")
        row = people.setdefault(key, {"email": None, "phone": None, "orders": 0, "value": 0.0})
        if email:
            alias[f"e:{email}"] = key
            row["email"] = row["email"] or email
        if phone:
            alias[f"p:{phone}"] = key
            row["phone"] = row["phone"] or phone
        if kept > 0.005:
            row["orders"] += 1
            row["value"] += kept
    out = []
    for r in people.values():
        if r["orders"] == 0:                                   # every order refunded: not a buyer
            continue
        out.append({"email": hash_email(r["email"]), "phone": hash_phone(r["phone"], default_cc),
                    "phone_e164": tiktok.hash_phone_e164(("+" + r["phone"]) if r["phone"] else None, default_cc),
                    "orders": r["orders"], "value": round(r["value"], 2)})
    return sorted(out, key=lambda r: -r["value"])


def summary(rows: list[dict]) -> dict:
    n = len(rows)
    return {"buyers": n, "with_email": sum(1 for r in rows if r["email"]), "with_phone": sum(1 for r in rows if r["phone"]),
            "repeat": sum(1 for r in rows if r["orders"] > 1), "meta_min": META_MIN, "lookalike_min": LOOKALIKE_MIN,
            "meta_ok": n >= META_MIN, "lookalike_ok": n >= LOOKALIKE_MIN}


def meta_csv(rows: list[dict]) -> bytes:
    """Meta customer list with customer value: email, phone (hashed), value."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "phone", "value"])
    for r in rows:
        w.writerow([r["email"] or "", r["phone"] or "", f"{r['value']:.2f}"])
    return buf.getvalue().encode()


def tiktok_txt(rows: list[dict], kind: str) -> bytes:
    """TikTok customer file: one hashed ID per line, one ID type per file (pick it, with SHA256, when uploading)."""
    key = "email" if kind == "email" else "phone_e164"
    return ("\n".join(r[key] for r in rows if r[key]) + "\n").encode()
