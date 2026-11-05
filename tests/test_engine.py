import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

from paysignal import Engine, Payment
from paysignal.attribution import resolve, tag_link
from paysignal.connectors import parse_csv, parse_shopify_webhook, parse_stripe_event
from paysignal.connectors.shopify import verify_hmac
from paysignal.connectors.stripe import verify_signature
from paysignal.identity import hash_email, normalize_phone
from paysignal.meta_capi import build_purchase_event, has_match_key

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def pay(i, amount=100.0, **kw):
    return Payment(client_id="c1", source="csv", external_id=str(i), kind="purchase",
                   amount=amount, currency="AED", occurred_at=NOW - timedelta(days=60), **kw)


def test_email_hash_is_normalized():
    assert hash_email("  Sara@Example.COM ") == hashlib.sha256(b"sara@example.com").hexdigest()


def test_uae_phone_normalization():
    assert normalize_phone("050 123 4567") == "971501234567"
    assert normalize_phone("+971 50 123 4567") == "971501234567"
    assert normalize_phone("00971501234567") == "971501234567"


def test_duplicates_are_ignored():
    e = Engine("c1")
    assert e.ingest([pay(1), pay(1)]) == 1


def test_other_clients_data_is_rejected():
    e = Engine("c1")
    other = pay(1); other.client_id = "c2"
    assert e.ingest([other]) == 0


def test_refund_links_to_purchase():
    e = Engine("c1")
    e.ingest([pay(1), Payment(client_id="c1", source="csv", external_id="r1", kind="refund", amount=40,
                              currency="AED", occurred_at=NOW, refund_of="1")])
    assert e.store.refunded_amount("c1", "1") == 40
    assert e.store.totals("c1")["net"] == 60


def test_stripe_amounts_and_refunds():
    evt = {"type": "charge.refunded", "data": {"object": {"id": "ch_1", "amount": 5000, "amount_refunded": 2500,
                                                           "currency": "aed", "created": 1}}}
    (r,) = parse_stripe_event(evt, "c1")
    assert r.kind == "refund" and r.amount == 25.0 and r.refund_of == "ch_1"


def test_stripe_signature():
    body, secret, ts = b'{"a":1}', "whsec_x", str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert verify_signature(body, f"t={ts},v1={sig}", secret)
    assert not verify_signature(body, f"t={ts},v1=bad", secret)


def test_shopify_hmac_and_tracking():
    body = {"id": 7, "total_price": "50.00", "currency": "AED", "email": "a@b.com",
            "landing_site": "/?utm_source=tiktok&utm_medium=organic&utm_content=vid9&fbclid=XYZ",
            "discount_codes": [{"code": "GLOW-1A2B"}], "line_items": []}
    raw = json.dumps(body).encode()
    good = base64.b64encode(hmac.new(b"s", raw, hashlib.sha256).digest()).decode()
    assert verify_hmac(raw, good, "s") and not verify_hmac(raw, "bad", "s")
    (p,) = parse_shopify_webhook("orders/paid", body, "c1")
    assert (p.utm_content, p.fbclid, p.promo_code) == ("vid9", "XYZ", "GLOW-1A2B")


def test_csv_reports_bad_rows():
    ok, errors = parse_csv("id,date,amount,currency\n1,2026-01-01,10,aed\n2,notadate,5,aed\n", "c1")
    assert len(ok) == 1 and len(errors) == 1


def test_csv_missing_columns():
    ok, errors = parse_csv("id,amount\n1,10\n", "c1")
    assert not ok and "missing columns" in errors[0]


def test_attribution_priority():
    assert resolve(pay(1, utm_source="instagram", utm_content="reel1")).content == "reel1"
    t = resolve(pay(2, promo_code="glow-1a2b"), {"GLOW-1A2B": ("facebook", "post5")})
    assert (t.platform, t.content, t.method) == ("facebook", "post5", "promo_code")
    assert resolve(pay(3)).kind == "unattributed"


def test_tag_link_keeps_existing_query():
    url = tag_link("https://shop.com/p?id=4", "instagram", "story1")
    assert "id=4" in url and "utm_content=story1" in url


def test_meta_event_never_contains_raw_contact():
    e = Engine("c1"); e.ingest([pay(1, email="sara@example.com", phone="0501234567")]); e.train(NOW)
    ev = build_purchase_event(pay(1, email="sara@example.com", phone="0501234567"), e.scorer.score(pay(1)))
    text = json.dumps(ev)
    assert "sara@example.com" not in text and "0501234567" not in text
    assert ev["event_id"] == "c1:csv:1" and has_match_key(ev)


def test_returned_orders_score_lower():
    e = Engine("c1")
    rows = []
    for i in range(120):
        discounted = i % 2 == 0
        rows.append(pay(i, amount=60 if discounted else 100, discount=40 if discounted else 0,
                        email=f"u{i}@x.com"))
        if discounted and i % 4 == 0:
            rows.append(Payment(client_id="c1", source="csv", external_id=f"r{i}", kind="refund", amount=60,
                                currency="AED", occurred_at=NOW - timedelta(days=50), refund_of=str(i)))
    e.ingest(rows)
    assert e.train(NOW)["model"] != "base_rate"
    risky = e.scorer.score(pay(999, amount=60, discount=40, email="new@x.com"))
    safe = e.scorer.score(pay(998, amount=100, email="new2@x.com"))
    assert risky.return_risk > safe.return_risk
    assert 0 < safe.return_risk  # never claims 0% certainty
