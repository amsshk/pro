from datetime import datetime, timedelta, timezone

import simulation as sim
from paysignal import Engine, Payment
from paysignal.organic import OrganicEngine, Post, load_posts_csv

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def _organic(seed=11):
    posts, pays = sim.build(seed)
    e = Engine(sim.CLIENT)
    e.ingest(pays)
    e.train(sim.NOW)
    return OrganicEngine(e, posts, now=sim.NOW)


def _buy(i, email, days_ago, post=None, amount=100.0):
    return Payment(client_id="c", source="csv", external_id=str(i), kind="purchase", amount=amount,
                   currency="AED", occurred_at=NOW - timedelta(days=days_ago), email=email,
                   utm_source="instagram" if post else None, utm_content=post)


def test_returning_customer_credited_to_first_post():
    e = Engine("c")
    e.ingest([_buy(1, "a@x.com", 60, post="p1"), _buy(2, "a@x.com", 40)])
    e.train(NOW)
    o = OrganicEngine(e, [Post("p1", "instagram", NOW - timedelta(days=61), views=1000)], now=NOW)
    hows = [(c.post_id, c.how) for c in o.credits()]
    assert hows == [("p1", "direct"), ("p1", "returning")]


def test_unknown_buyer_stays_unattributed():
    e = Engine("c")
    e.ingest([_buy(1, "z@x.com", 30)])
    e.train(NOW)
    o = OrganicEngine(e, [], now=NOW)
    assert o.credits()[0].how == "unattributed"


def test_refunded_past_order_is_worth_nothing():
    e = Engine("c")
    e.ingest([_buy(1, "a@x.com", 60, post="p1"),
              Payment(client_id="c", source="csv", external_id="r1", kind="refund", amount=100,
                      currency="AED", occurred_at=NOW - timedelta(days=55), refund_of="1")])
    e.train(NOW)
    o = OrganicEngine(e, [Post("p1", "instagram", NOW - timedelta(days=61), views=1000)], now=NOW)
    assert o.credits()[0].real_value == 0


def test_posts_csv():
    posts, err = load_posts_csv("post_id,platform,posted_at,views,format,hook\n"
                                "p1,Instagram,2026-10-01T20:00:00,5000,Reel,Testimonial\n"
                                "p2,tiktok,bad-date,100,,\n")
    assert len(posts) == 1 and len(err) == 1
    assert (posts[0].platform, posts[0].format, posts[0].hook) == ("instagram", "reel", "testimonial")


def test_finds_watcher_posts():
    o = _organic()
    watchers = [r.post.hook for r in o.scoreboard() if "WATCHERS ONLY" in r.tags]
    assert watchers and watchers.count("trend_meme") >= len(watchers) / 2


def test_learns_hidden_patterns():
    pat = _organic().patterns()
    hook = pat["hook"]
    assert hook["testimonial"]["rate"] > hook["trend_meme"]["rate"]
    assert hook["trend_meme"]["lift"] < -0.5
    assert pat["format"]["story"]["rate"] > pat["format"]["reel"]["rate"]
    assert pat["offer"]["discount"]["rate"] < pat["offer"]["none"]["rate"]


def test_recommends_payers_not_discounts():
    for seed in (11, 12, 14):
        rec = _organic(seed).recommend(1)[0]
        assert rec["hook"] == "testimonial" and rec["offer"] != "discount"


def test_halo_is_positive_and_bounded():
    o = _organic()
    credits = o.credits()
    halo = sum(o.halo(credits).values())
    unlinked = sum(c.real_value for c in credits if c.how == "unattributed")
    assert 0 < halo < unlinked


def test_boost_candidates_avoid_refund_heavy_posts():
    for r in _organic().boost_candidates():
        assert r.refunds / r.gross <= 0.25 and r.orders >= 3
