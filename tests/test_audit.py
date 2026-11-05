import json

import pytest


import simulation as sim
from paysignal import Engine
from paysignal.ai import MetaAI
from paysignal.audit import audit_summary, audit_text, build_audit
from paysignal.importers import detect, import_auto, import_posts, import_shopify_orders, import_stripe_payments
from paysignal.labeler import RuleLabeler, label_posts

SHOPIFY = """Name,Email,Financial Status,Paid at,Currency,Total,Discount Code,Discount Amount,Refunded Amount,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku,Id
#1001,a@x.com,paid,2026-10-01 20:15:00 +0400,AED,300.00,,0.00,0.00,1,Hydrafacial,300.00,HF-01,111
#1002,b@x.com,refunded,2026-10-02 10:00:00 +0400,AED,"1,200.00",FLASH30,360.00,"1,200.00",2,Serum,400.00,GS-50,112
#1002,,,,,,,,,1,Mask,400.00,MK-1,
#1003,c@x.com,pending,2026-10-03 10:00:00 +0400,AED,50.00,,0,0,1,Thing,50.00,T-1,113
"""

STRIPE = """id,Created date (UTC),Amount,Amount Refunded,Currency,Captured,Customer Email,Status
ch_1,2026-10-01 10:00:00,249.00,0.00,aed,true,a@x.com,Paid
ch_2,2026-10-02 11:00:00,100.00,40.00,aed,true,b@x.com,Paid
ch_3,2026-10-03 12:00:00,80.00,0.00,aed,false,c@x.com,Failed
"""

META_POSTS = """Post ID,Account username,Description,Publish time,Post type,Views,Reactions,Comments,Shares,Saves,Link clicks
1789,clinic,"Before & after ✨",10/01/2026 20:00,IG reel,"12,400",820,64,51,90,140
1790,clinic,"تجربة عميلتنا ❤️",10/02/2026 13:30,IG story,3100,95,8,4,12,88
"""


def test_detects_each_export():
    assert detect(SHOPIFY) == "shopify"
    assert detect(STRIPE) == "stripe"
    assert detect(META_POSTS) == "posts"
    assert detect("id,date,amount,currency\n1,2026-01-01,5,AED\n") == "csv"
    assert detect("hello,world\n1,2\n") == "unknown"


def test_shopify_export_groups_line_items_and_refunds():
    pays, errors = import_shopify_orders(SHOPIFY, "c")
    purchases = [p for p in pays if p.kind == "purchase"]
    refunds = [p for p in pays if p.kind == "refund"]
    assert len(purchases) == 2 and len(refunds) == 1 and "unpaid" in errors[0]
    big = next(p for p in purchases if p.external_id == "112")
    assert big.amount == 1200.0 and big.promo_code == "FLASH30" and len(big.items) == 2
    assert refunds[0].refund_of == "112" and refunds[0].amount == 1200.0
    assert big.occurred_at.utcoffset().total_seconds() == 4 * 3600


def test_stripe_export_skips_failed_and_links_refunds():
    pays, errors = import_stripe_payments(STRIPE, "c")
    assert [p.external_id for p in pays if p.kind == "purchase"] == ["ch_1", "ch_2"]
    (r,) = [p for p in pays if p.kind == "refund"]
    assert r.refund_of == "ch_2" and r.amount == 40.0 and errors


def test_meta_posts_export_and_labels():
    posts, errors = import_posts(META_POSTS)
    assert not errors and posts[0].views == 12400 and posts[0].posted_at.month == 10
    label_posts(posts)
    assert (posts[0].format, posts[0].hook) == ("reel", "before_after")
    assert (posts[1].format, posts[1].hook) == ("story", "testimonial")


def test_media_type_variants():
    r = RuleLabeler()
    for mt, fmt in [("IG reel", "reel"), ("Photos", "image"), ("FB video", "video"), ("IG carousel", "carousel"),
                    ("CAROUSEL_ALBUM", "carousel"), ("Stories", "story")]:
        assert r.label("", mt).format == fmt, mt


def test_posts_export_missing_columns_explained():
    posts, errors = import_posts("Title,Likes\nhi,3\n")
    assert not posts and "post_id" in errors[0] and "Title" in errors[0]


def test_reimport_is_idempotent():
    e = Engine("c")
    pays, _ = import_shopify_orders(SHOPIFY, "c")
    assert e.ingest(pays) == 3 and e.ingest(pays) == 0


@pytest.fixture(scope="module")
def demo_audit_result():
    orders_csv, posts_csv = sim.write_exports()
    e = Engine("d")
    e.ingest(import_auto(orders_csv, "d")["payments"])
    posts = import_auto(posts_csv, "d")["posts"]
    label_posts(posts)
    return build_audit(e, posts, now=sim.NOW)


def test_audit_finds_the_money_leaks(demo_audit_result):
    a = demo_audit_result
    flash = next(c for c in a["discount_codes"] if c["code"] == "FLASH30")
    welcome = next(c for c in a["discount_codes"] if c["code"] == "WELCOME10")
    assert flash["vs_full_price"] < -0.4 < welcome["vs_full_price"]
    assert a["refund_heavy_products"][0]["product"] == "Glow Serum 50ml"
    assert any("FLASH30" in x for x in a["actions"]) and any("Glow Serum" in x for x in a["actions"])


def test_audit_labels_post_findings_as_estimates(demo_audit_result):
    assert "ESTIMATED" in demo_audit_result["posts"]["method"]
    assert "ESTIMATED" in audit_text(demo_audit_result)


def test_audit_has_no_personal_data(demo_audit_result):
    assert "@" not in json.dumps(demo_audit_result, default=str)


def test_audit_summary_uses_best_model(demo_audit_result):
    class S:
        def __init__(self): self.calls = []
        def post(self, url, timeout, json, headers):
            self.calls.append(json)
            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"choices": [{"message": {"content": "ملخص"}}], "usage": {}}
            return R()
    s = S()
    out = audit_summary(MetaAI(api_key="k", session=s), demo_audit_result, language="Arabic")
    assert out["summary"] == "ملخص" and s.calls[0]["model"] == "muse-wonder-1.0"
    assert "Arabic" in s.calls[0]["messages"][0]["content"]


def test_empty_audit():
    assert "error" in build_audit(Engine("empty"))


def test_server_import_and_audit(tmp_path):
    from paysignal.server import create_app
    c = create_app({"c": {"api_key": "K"}}, str(tmp_path / "db"), start_worker=False).test_client()
    H = {"X-API-Key": "K"}
    assert c.get("/audit/c", headers=H).status_code == 400
    r = c.post("/import/c", data=SHOPIFY, headers=H).json
    assert r["kind"] == "shopify" and r["new"] == 3
    r = c.post("/import/c", data=META_POSTS, headers=H).json
    assert r["kind"] == "posts" and r["posts"] == 2
    a = c.get("/audit/c?summary=1", headers=H).json
    assert a["audit"]["orders"] == 2 and "summary_error" in a
    assert c.post("/import/c", data="x,y\n1,2\n", headers=H).status_code == 400
