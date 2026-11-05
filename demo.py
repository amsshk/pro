"""Organic-first demo: 6 months of simulated sales from four organic posts.

Shows the point of the engine: the post with the most sales is not the post
that makes the most money once refunds and repeat buying are counted.

Run:  python demo.py
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from paysignal import Engine, LineItem, Payment
from paysignal.attribution import make_promo_code, tag_link
from paysignal.connectors import parse_csv, parse_shopify_webhook, parse_stripe_event

random.seed(7)
CLIENT = "demo-store"
NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
START = NOW - timedelta(days=180)

# Each organic post: how many buyers it brings, how often they return the item,
# how often they come back to buy again, and how they pay.
POSTS = {
    "ig_reel_40off":       dict(platform="instagram", buyers=220, refund=0.38, repeat=0.06, discount=0.40, via="utm"),
    "ig_story_reviews":    dict(platform="instagram", buyers=110, refund=0.06, repeat=0.42, discount=0.00, via="utm"),
    "tiktok_howto":        dict(platform="tiktok",    buyers=140, refund=0.14, repeat=0.22, discount=0.10, via="utm"),
    "fb_post_whatsapp":    dict(platform="facebook",  buyers=80,  refund=0.05, repeat=0.35, discount=0.00, via="code"),
}
PROMO = {pid: make_promo_code(pid) for pid in POSTS}
PROMO_MAP = {code: (POSTS[pid]["platform"], pid) for pid, code in PROMO.items()}


def simulate() -> list[Payment]:
    payments, n = [], 0
    for pid, cfg in POSTS.items():
        for c in range(cfg["buyers"]):
            email = f"{pid}.{c}@example.com"
            t = START + timedelta(days=random.uniform(0, 170))
            orders = 1 + (sum(random.random() < cfg["repeat"] for _ in range(3)))
            for o in range(orders):
                n += 1
                list_price = round(random.uniform(120, 420), 2)
                disc = round(list_price * cfg["discount"] if o == 0 else 0, 2)
                amount = round(list_price - disc, 2)
                offline = cfg["via"] == "code"
                p = Payment(
                    client_id=CLIENT, source="csv" if offline else "shopify", external_id=f"o{n}",
                    kind="purchase", amount=amount, currency="AED", occurred_at=t,
                    email=email, phone=f"05{random.randint(10000000, 99999999)}",
                    discount=disc, channel="physical_store" if offline else "website",
                    items=[LineItem("SKU", "item", 1, amount, round(list_price * 0.45, 2))],
                    utm_source=None if offline else cfg["platform"],
                    utm_medium=None if offline else "organic",
                    utm_content=None if (offline or o > 0) else pid,
                    promo_code=PROMO[pid] if (offline and o == 0) else None,
                )
                # repeat orders inherit the first post as their source
                if o > 0 and not offline:
                    p.utm_content = pid
                if o > 0 and offline:
                    p.promo_code = PROMO[pid]
                payments.append(p)
                risk = cfg["refund"] * (1.4 if disc else 0.7)
                if random.random() < risk:
                    payments.append(Payment(
                        client_id=CLIENT, source=p.source, external_id=f"r{n}", kind="refund",
                        amount=amount, currency="AED", occurred_at=t + timedelta(days=random.uniform(2, 20)),
                        refund_of=p.external_id, email=email))
                t += timedelta(days=random.uniform(15, 60))
                if t > NOW:
                    break
    return payments


def connector_check() -> list[Payment]:
    """One real-format payload per connector, to prove parsing end to end."""
    stripe_evt = {"type": "charge.succeeded", "data": {"object": {
        "id": "ch_demo1", "amount": 24900, "currency": "aed", "created": int(NOW.timestamp()) - 3600,
        "billing_details": {"email": "Sara@Example.com", "phone": "+971 50 123 4567"},
        "metadata": {"utm_source": "instagram", "utm_medium": "organic", "utm_content": "ig_story_reviews"}}}}
    shopify_order = {"id": 9001, "total_price": "189.00", "currency": "AED", "email": "omar@example.com",
                     "processed_at": (NOW - timedelta(hours=2)).isoformat(),
                     "landing_site": "/products/x?utm_source=tiktok&utm_medium=organic&utm_content=tiktok_howto&fbclid=IwAR0demo",
                     "line_items": [{"sku": "SKU", "title": "item", "quantity": 1, "price": "189.00"}]}
    csv_text = ("id,date,amount,currency,phone,promo_code,cost\n"
                f"cash-1,{(NOW - timedelta(hours=1)).date()},300,AED,050 765 4321,{PROMO['fb_post_whatsapp']},120\n")
    csv_payments, errors = parse_csv(csv_text, CLIENT)
    assert not errors, errors
    return (parse_stripe_event(stripe_evt, CLIENT)
            + parse_shopify_webhook("orders/paid", shopify_order, CLIENT, costs={"SKU": 80})
            + csv_payments)


def main() -> None:
    engine = Engine(CLIENT, promo_map=PROMO_MAP)
    history = simulate()
    added = engine.ingest(history)
    dupes = len(history) - added + (len(history) - engine.ingest(history))  # re-ingest: all dupes
    live = connector_check()
    engine.ingest(live)

    stats = engine.train(NOW)
    t = engine.store.totals(CLIENT)
    print("=" * 78)
    print("PAYSIGNAL - organic test run")
    print("=" * 78)
    print(f"Payments detected: {added + len(live)}   (duplicates ignored on re-import: {dupes})")
    print(f"Gross AED {t['gross']:,.0f}   refunds AED {t['refunds']:,.0f}   net AED {t['net']:,.0f}")
    print(f"Model: {stats['model']}, trained on {stats['trained_on']} matured orders, "
          f"return rate {stats['return_rate']:.1%}, repeat rate new {stats['repeat_rate']['new']:.1%}")

    print("\nWHICH ORGANIC POSTS ACTUALLY MAKE MONEY")
    print(f"{'post':<20}{'platform':<11}{'via':<10}{'orders':>7}{'gross':>11}{'refund%':>9}{'real value':>12}")
    for r in engine.content_report():
        print(f"{r['content']:<20}{r['platform']:<11}{r['kind']:<10}{r['orders']:>7}"
              f"{r['gross']:>11,.0f}{r['refund_rate']:>9.0%}{r['real_value']:>12,.0f}")

    print("\nTAGGED LINKS / CODES TO USE ON THE POSTS")
    for pid, cfg in POSTS.items():
        how = PROMO[pid] if cfg["via"] == "code" else tag_link("https://shop.example.com", cfg["platform"], pid)
        print(f"  {pid:<20}{how}")

    print("\nNEWEST PAYMENTS AND THE VALUE META WILL BE TOLD")
    for p in live:
        s = engine.scorer.score(p)
        print(f"  {p.source:<8} charged {p.amount:>7.2f} {p.currency}  ->  real value {s.real_value:>7.2f} "
              f"(margin {s.margin}, return risk {s.return_risk:.0%}, future {s.future_value})")

    result = engine.push_to_meta(dataset_id="YOUR_DATASET_ID", access_token="YOUR_TOKEN", dry_run=True)
    sample = next(e for e in result[0]["payload"]["data"] if e["event_id"].endswith("ch_demo1"))
    with open("sample_meta_event.json", "w") as f:
        json.dump(sample, f, indent=2)
    print(f"\nMeta Conversions API (dry run): {result[0]['count']} events ready, nothing sent.")
    print("Sample event saved to sample_meta_event.json")


if __name__ == "__main__":
    main()
