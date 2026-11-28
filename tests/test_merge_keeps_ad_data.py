from datetime import datetime, timezone

from paysignal import proads_results
from paysignal.models import Payment
from paysignal.server import create_app

T = datetime(2026, 11, 28, 8, 38, tzinfo=timezone.utc)


def mk(src, ext, **kw):
    return Payment(client_id="shop", source=src, external_id=ext, kind="purchase", amount=965.0, currency="AED", occurred_at=T,
                   email="a@b.com", **kw)


def stored(st, src, ext):
    return next(p for p in st.store.purchases("shop") if p.source == src and p.external_id == ext)


def make(tmp_path):
    a = create_app({"shop": {"api_key": "k" * 24, "name": "S", "currency": "AED"}}, str(tmp_path / "t.db"), start_worker=False)
    return a.extensions["paysignal"]


def test_stripe_first_then_shopify_keeps_the_tiktok_click(tmp_path):
    st = make(tmp_path)
    st.ingest("shop", [mk("stripe", "ch_1")])
    st.ingest("shop", [mk("shopify", "5001", ttclid="E.C.abc", utm_source="tiktok", utm_medium="paid")])
    assert len(st.store.purchases("shop")) == 1
    p = stored(st, "stripe", "ch_1")
    assert p.ttclid == "E.C.abc" and proads_results.from_ads(p) == "tiktok"


def test_shopify_first_then_stripe_is_unchanged(tmp_path):
    st = make(tmp_path)
    st.ingest("shop", [mk("shopify", "5001", ttclid="E.C.abc")])
    st.ingest("shop", [mk("stripe", "ch_1")])
    assert len(st.store.purchases("shop")) == 1 and stored(st, "shopify", "5001").ttclid == "E.C.abc"


def test_sale_merged_before_the_fix_heals_on_the_next_import(tmp_path):
    st = make(tmp_path)
    st.ingest("shop", [mk("stripe", "ch_1")])
    st.ingest("shop", [mk("shopify", "5001")])                      # merged, no ad data on either (as it was before)
    assert stored(st, "stripe", "ch_1").ttclid is None
    st.ingest("shop", [mk("shopify", "5001", ttclid="E.C.abc")])    # Shopify import runs again, now with the journey
    assert stored(st, "stripe", "ch_1").ttclid == "E.C.abc"


def test_google_click_is_never_credited_to_tiktok_or_meta(tmp_path):
    g = mk("shopify", "5002", gclid="Cj0KCQ", utm_source="tiktok", utm_medium="paid")
    assert proads_results.from_ads(g) is None
    both = mk("shopify", "5003", gclid="Cj0KCQ", ttclid="E.C.abc")
    assert proads_results.from_ads(both) == "tiktok"
