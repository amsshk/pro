"""Organic engine demo: 4 months, 60 posts, with patterns hidden in the data.

Hidden truth the engine has to discover on its own:
  - trend/meme posts get the most likes but almost nobody pays  (watchers)
  - testimonial posts get few likes but bring loyal paying customers
  - discount offers bring more orders, but many are refunded and those buyers rarely return
  - evening posts sell better
  - some buyers never click the link: they buy a day or two later on their own (halo)
  - many customers come back and buy again without any link (returning)

Run:  python demo_organic.py
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from paysignal import Engine, LineItem, Payment
from paysignal.organic import OrganicEngine, Post

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


def main():
    posts, pays = build()
    engine = Engine(CLIENT)
    engine.ingest(pays)
    stats = engine.train(NOW)
    organic = OrganicEngine(engine, posts, now=NOW)
    print("=" * 96)
    print(f"PAYSIGNAL ORGANIC ENGINE - {len(posts)} posts, {len(pays)} payments, "
          f"value model: {stats['model']} on {stats['trained_on']} orders")
    print("=" * 96)
    print(organic.report())


if __name__ == "__main__":
    main()
