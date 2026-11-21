


def test_purchase_event_says_what_was_bought():
    from datetime import datetime, timezone
    from paysignal.meta_capi import build_purchase_event
    from paysignal.models import LineItem, Payment

    class S:
        expected_value = 485
        profit = None
    p = Payment(client_id="c", source="shopify", external_id="1", kind="purchase", amount=485, currency="AED",
                occurred_at=datetime(2026, 11, 20, tzinfo=timezone.utc), email="a@b.com",
                items=[LineItem(sku="N6-50", name="N°6 Extrait", quantity=1, unit_price=485)])
    try:
        ev = build_purchase_event(p, S())
    except AttributeError:
        from paysignal.engine import Engine
        ev = None
    assert ev and ev["custom_data"]["content_ids"] == ["N6-50"] and ev["custom_data"]["num_items"] == 1


def test_profit_goes_as_net_revenue_and_local_phone_gets_country_code():
    from datetime import datetime, timezone
    from paysignal.identity import normalize_phone
    from paysignal.meta_capi import build_purchase_event
    from paysignal.models import Payment

    class S:
        expected_value = 485
    p = Payment(client_id="c", source="shopify", external_id="2", kind="purchase", amount=485, currency="AED",
                occurred_at=datetime(2026, 11, 20, tzinfo=timezone.utc), email="a@b.com")
    ev = build_purchase_event(p, S(), profit=210.0)
    assert ev["custom_data"]["value"] == 485 and ev["custom_data"]["net_revenue"] == 210.0
    assert "net_revenue" not in build_purchase_event(p, S())["custom_data"]
    assert normalize_phone("501234567") == "971501234567" and normalize_phone("0501234567") == "971501234567"
    assert normalize_phone("+966501234567") == "966501234567" and normalize_phone("971501234567") == "971501234567"
