"""First-week audit demo: a business connects on day one with 4 months of history.

1. Writes realistic export files (Shopify orders export + Meta Business Suite posts export).
   The old posts had NO tracking links, like most businesses.
2. Imports them, labels the posts, runs the audit.

Run:  python demo_audit.py            (add MODEL_API_KEY for the Muse-written summary)
"""
from __future__ import annotations

import csv
import io
import random

import demo_organic as sim
from paysignal import Engine
from paysignal.audit import audit_summary, audit_text, build_audit
from paysignal.importers import import_auto
from paysignal.labeler import label_posts, make_labeler

CAPTIONS = {
    "testimonial": ["⭐⭐⭐⭐⭐ What our clients say after their first session",
                    "تجربة عميلتنا معنا ❤️ شكراً على ثقتك", "Our customer shares her review after 2 months"],
    "tutorial": ["How to prepare your skin before a session, 3 steps", "نصائح مهمة قبل الجلسة 👇",
                 "Tips to keep your results longer, save this"],
    "before_after": ["Before & after: 3 sessions ✨", "قبل وبعد 🤍 النتيجة تتكلم", "Her transformation in 6 weeks"],
    "trend_meme": ["POV: you finally booked it 😂", "Me when someone asks my skincare secret 🤣",
                   "ترند الأسبوع مع فريقنا 😂"],
    "behind_scenes": ["Behind the scenes of today's shoot 📸", "Meet the team: Dr. Lina", "كواليس يوم في العيادة"],
}
OFFER_TEXT = {"none": "", "discount": " | 30% off this week with code FLASH30",
              "free_gift": " | free gift with every booking 🎁"}
MEDIA = {"reel": "IG reel", "story": "IG story", "carousel": "IG carousel"}
PRODUCTS = [("Hydrafacial", "HF-01"), ("Laser Session", "LS-02"), ("Glow Serum 50ml", "GS-50"),
            ("Peel Treatment", "PT-03")]


def write_exports(seed: int = 11) -> tuple[str, str]:
    rng = random.Random(seed)
    posts, pays = sim.build(seed)
    refunds = {p.refund_of: p for p in pays if p.kind == "refund"}

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Email", "Financial Status", "Paid at", "Currency", "Subtotal", "Total",
                "Discount Code", "Discount Amount", "Refunded Amount", "Lineitem quantity", "Lineitem name",
                "Lineitem price", "Lineitem sku", "Phone", "Id"])
    for n, p in enumerate((p for p in pays if p.kind == "purchase"), start=1001):
        refunded = refunds.get(p.external_id)
        if refunded and rng.random() < 0.45:
            name, sku = PRODUCTS[2]                      # the serum gets returned a lot
        else:
            name, sku = rng.choice([PRODUCTS[0], PRODUCTS[1], PRODUCTS[3]] + [PRODUCTS[2]] * (1 if refunded else 0))
        code, disc = "", p.discount
        if p.discount > 0:
            code = "FLASH30"
        elif rng.random() < 0.12:
            code, disc = "WELCOME10", round(p.amount / 0.9 * 0.1, 2)
        status = "refunded" if refunded else "paid"
        w.writerow([f"#{n}", p.email, status, p.occurred_at.strftime("%Y-%m-%d %H:%M:%S +0400"), "AED",
                    f"{p.amount + disc:.2f}", f"{p.amount:.2f}", code, f"{disc:.2f}",
                    f"{refunded.amount:.2f}" if refunded else "0.00", 1, name, f"{p.amount + disc:.2f}", sku,
                    "", str(5000000 + n)])

    out2 = io.StringIO()
    w2 = csv.writer(out2)
    w2.writerow(["Post ID", "Account username", "Description", "Duration (sec)", "Publish time", "Post type",
                 "Views", "Reach", "Reactions", "Comments", "Shares", "Saves", "Link clicks"])
    for p in posts:
        caption = rng.choice(CAPTIONS[p.hook]) + OFFER_TEXT[p.offer]
        w2.writerow([p.post_id, "demo.clinic", caption, 30 if p.format == "reel" else 0,
                     p.posted_at.strftime("%m/%d/%Y %H:%M"), MEDIA[p.format], p.views, int(p.views * 0.8),
                     p.likes, p.comments, p.shares, p.saves, p.link_clicks])
    return out.getvalue(), out2.getvalue()


def main():
    orders_csv, posts_csv = write_exports()
    with open("sample_shopify_orders_export.csv", "w", encoding="utf-8") as f:
        f.write(orders_csv)
    with open("sample_meta_posts_export.csv", "w", encoding="utf-8") as f:
        f.write(posts_csv)

    engine = Engine("audit-demo")
    orders = import_auto(orders_csv, "audit-demo", costs={"HF-01": 120, "LS-02": 150, "GS-50": 60, "PT-03": 110})
    posts = import_auto(posts_csv, "audit-demo")
    engine.ingest(orders["payments"])
    labels = label_posts(posts["posts"], make_labeler())
    print(f"Imported {orders['kind']} export: {len(orders['payments'])} records {orders['errors'] or ''}")
    print(f"Imported {posts['kind']} export: {len(posts['posts'])} posts, labelled by {labels['source']}, "
          f"{len(labels['low_confidence'])} to check by hand\n")

    audit = build_audit(engine, posts["posts"], now=sim.NOW)
    print(audit_text(audit))

    from paysignal.ai import MetaAI
    ai = MetaAI()
    if ai.available:
        print("\nSUMMARY (Muse):\n" + audit_summary(ai, audit)["summary"])
    else:
        print("\n(Set MODEL_API_KEY to add a Muse-written summary in English or Arabic.)")


if __name__ == "__main__":
    main()
