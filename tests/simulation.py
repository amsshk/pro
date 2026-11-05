"""Test data: simulated history with known hidden patterns, used to check the engine finds them.

4 months, 60 posts.

Hidden truth the engine has to discover on its own:
  - trend/meme posts get the most likes but almost nobody pays  (watchers)
  - testimonial posts get few likes but bring loyal paying customers
  - discount offers bring more orders, but many are refunded and those buyers rarely return
  - evening posts sell better
  - some buyers never click the link: they buy a day or two later on their own (halo)
  - many customers come back and buy again without any link (returning)

"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from paysignal import Engine, LineItem, Payment
from paysignal.organic import Post

CLIENT = "organic-demo"
NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
START = NOW - timedelta(days=120)

FORMATS = {"reel": (9000, 0.010), "story": (2500, 0.030), "carousel": (4000, 0.015)}   # views, click rate
HOOKS = {   # engagement rate, buy rate after click, chance a buyer returns
    "trend_meme":    (0.12, 0.010, 0.05),
    "before_after":  (0.07, 0.060, 0.20),
    "tutorial":      (0.05, 0.070, 0.30),
    "testimonial":   (0.03, 0.110, 0.45),
    "behind_scenes": (0.04, 0.030, 0.25),
}
OFFERS = {"none": (1.0, 0.04, 0.0), "discount": (1.6, 0.35, 0.30), "free_gift": (1.2, 0.08, 0.0)}  # buy x, refund, discount
HOURS = {"morning": (9, 0.8), "afternoon": (14, 1.0), "evening": (20, 1.35)}


def build(seed: int = 11):
    rng = random.Random(seed)
    posts, pays, n = [], [], 0

    def purchase(t, email, post_id, offer, linked, offline=False):
        nonlocal n
        n += 1
        price = round(rng.uniform(150, 450), 2)
        disc = round(price * OFFERS[offer][2], 2) if linked else 0.0
        amt = round(price - disc, 2)
        p = Payment(client_id=CLIENT, source="csv" if offline else "shopify", external_id=f"o{n}",
                    kind="purchase", amount=amt, currency="AED", occurred_at=t, email=email,
                    discount=disc, items=[LineItem("S", "item", 1, amt, round(price * 0.45, 2))],
                    channel="physical_store" if offline else "website")
        if linked:
            p.utm_source, p.utm_medium, p.utm_content = "instagram", "organic", post_id
        pays.append(p)
        if rng.random() < (OFFERS[offer][1] if linked else 0.05):
            pays.append(Payment(client_id=CLIENT, source=p.source, external_id=f"r{n}", kind="refund",
                                amount=amt, currency="AED", refund_of=p.external_id, email=email,
                                occurred_at=t + timedelta(days=rng.uniform(3, 18))))

    for i in range(60):
        fmt, hook, offer = rng.choice(list(FORMATS)), rng.choice(list(HOOKS)), \
            rng.choices(list(OFFERS), weights=[6, 3, 2])[0]
        slot = rng.choice(list(HOURS))
        day = START + timedelta(days=i * 1.8 + rng.uniform(0, 1))
        posted = day.replace(hour=HOURS[slot][0], minute=0)
        base_views, ctr = FORMATS[fmt]
        eng, buy, ret = HOOKS[hook]
        views = int(base_views * rng.lognormvariate(0, 0.4) * (1 + 3 * eng))   # catchy posts get reach
        clicks = int(views * ctr * rng.uniform(0.8, 1.2))
        pid = f"post{i + 1:02d}"
        posts.append(Post(pid, "instagram", posted, fmt, hook, offer, views=views,
                          likes=int(views * eng * 0.8), comments=int(views * eng * 0.08),
                          shares=int(views * eng * 0.07), saves=int(views * eng * 0.05), link_clicks=clicks))

        buyers = sum(rng.random() < buy * OFFERS[offer][0] * HOURS[slot][1] for _ in range(clicks))
        for b in range(buyers):
            email = f"{pid}.b{b}@example.com"
            t = posted + timedelta(hours=rng.uniform(0.2, 30))
            purchase(t, email, pid, offer, linked=True)
            # discount-hunters rarely come back at full price
            loyal = ret * (0.4 if offer == "discount" else 1.0)
            while rng.random() < loyal:                     # comes back later, no link
                t += timedelta(days=rng.uniform(10, 40))
                if t > NOW:
                    break
                purchase(t, email, pid, offer, linked=False)
        # halo: people who saw it and bought later without clicking (not boosted by the discount,
        # since they didn't use it)
        for h in range(int(buyers / OFFERS[offer][0] * 0.6)):
            purchase(posted + timedelta(hours=rng.uniform(8, 60)), f"{pid}.h{h}@example.com",
                     pid, offer, linked=False, offline=rng.random() < 0.4)

    # normal background sales that no post caused
    d = START
    while d < NOW:
        for k in range(sum(rng.random() < 0.5 for _ in range(4))):
            purchase(d + timedelta(hours=rng.uniform(0, 24)), f"walkin.{d:%m%d}.{k}@example.com",
                     None, "none", linked=False, offline=True)
        d += timedelta(days=1)
    return posts, pays


# ---- realistic export files (Shopify orders, Meta Business Suite posts) ----
import csv
import io

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
    posts, pays = build(seed)
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
