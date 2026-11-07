"""Customer identity: normalize and hash before anything leaves the server.

Meta and TikTok match people using SHA-256 hashes of normalized email/phone.
Raw contact details are never sent.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hash_field(value: Optional[str], kind: str = "text") -> Optional[str]:
    """Meta's normalising rules before hashing: lowercase; names keep letters only; city/zip lose spaces
    and punctuation; country is the 2-letter code."""
    if not value:
        return None
    v = str(value).strip().lower()
    if kind == "name":
        v = re.sub(r"[^\w]", "", v)
    elif kind in ("city", "zip"):
        v = re.sub(r"[\s\W_]", "", v)
    elif kind == "country":
        v = re.sub(r"[^a-z]", "", v)[:2]
        if len(v) != 2:
            return None
    return _sha256(v) if v else None


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    e = email.strip().lower()
    return e if "@" in e else None


def normalize_phone(phone: Optional[str], default_country_code: str = "971") -> Optional[str]:
    """Digits only, with country code. Local numbers like 050... get the default code."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("00"):
        digits = digits[2:]
    elif not phone.strip().startswith("+") and digits.startswith("0"):
        digits = default_country_code + digits.lstrip("0")
    return digits if len(digits) >= 8 else None


def hash_email(email: Optional[str]) -> Optional[str]:
    e = normalize_email(email)
    return _sha256(e) if e else None


def hash_phone(phone: Optional[str], default_country_code: str = "971") -> Optional[str]:
    p = normalize_phone(phone, default_country_code)
    return _sha256(p) if p else None


def fbc_from_fbclid(fbclid: Optional[str], clicked_at: datetime) -> Optional[str]:
    """Meta's click-id format: fb.1.<click time in ms>.<fbclid>."""
    if not fbclid:
        return None
    return f"fb.1.{int(clicked_at.timestamp() * 1000)}.{fbclid}"


def customer_key(email: Optional[str], phone: Optional[str], customer_id: Optional[str]) -> Optional[str]:
    """One stable key per customer across sources, used to link history."""
    return hash_email(email) or hash_phone(phone) or (f"id:{customer_id}" if customer_id else None)
