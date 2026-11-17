"""ProAds Impact: honest percentages."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as P

from paysignal import impact
from paysignal.server import create_app

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


def pay(day, amount=100, source="shopify", **kw):
    base = dict(source=source, channel="website", event_source_url=None, fbclid=None, utm_source=None, external_id=f"{day}-{amount}-{source}")
    base.update(kw)
    return P(occurred_at=NOW - timedelta(days=day), amount=amount, **base)


def test_before_after_shows_change_with_a_range():
    started = NOW - timedelta(days=14)
    ps = [pay(d) for d in range(15, 29)] + [pay(d) for d in range(1, 15) for _ in range(2)]   # 1 a day before, 2 after
    out = impact.before_after(ps, lambda p: 0, started, NOW, spend_before=1000, spend_after=1000)
    assert out["ready"] and out["days"] == 14
    assert out["orders"]["change"] == 1.0 and out["money"]["before"] == 100 and out["money"]["after"] == 200
    assert out["return"] == {"before": 1.4, "after": 2.8, "change": 1.0, "spend_before": 1000, "spend_after": 1000}


def test_too_early_and_too_few():
    assert impact.before_after([], lambda p: 0, NOW - timedelta(days=3), NOW)["needs_days"] == 4
    assert impact.before_after([pay(1)], lambda p: 0, NOW - timedelta(days=10), NOW)["too_few_before"]


def test_refunds_are_not_counted():
    started = NOW - timedelta(days=10)
    ps = [pay(d) for d in range(11, 21)] + [pay(d, external_id=f"r{d}") for d in range(0, 10)]
    out = impact.before_after(ps, lambda p: 100 if str(p.external_id).startswith("r") else 0, started, NOW)
    assert out["money"]["after"] == 0 and out["money"]["change"] == -1.0


def test_coverage_counts_sales_no_pixel_can_see():
    ps = [pay(1), pay(2, source="csv"), pay(3, source="stripe"), pay(4, source="stripe", fbclid="x"), pay(40)]
    c = impact.coverage(ps, lambda p: 0, NOW - timedelta(days=30), sent_meta=4, match={"em": 0.9, "ph": 0.5})
    assert c["orders"] == 4 and c["sent_meta_share"] == 1.0 and c["outside_checkout"] == 2 and c["outside_share"] == 0.5


class R:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def test_meta_quality_reads_the_purchase_event():
    class H:
        def get(self, url, params, timeout):
            assert url.endswith("/dataset_quality") and params["dataset_id"] == "777"
            return R(200, {"web": [{"event_name": "PageView"}, {"event_name": "Purchase", "acr": {"percentage": 19.5},
                                   "event_match_quality": {"composite_score": 7.8}, "event_coverage": {"percentage": 92}}]})
    q = impact.meta_quality(H(), "tok", "777", "v25.0")
    assert q == {"acr": 19.5, "match_quality": 7.8, "coverage": 92.0, "freshness": None}

    class E:
        def get(self, url, params, timeout):
            return R(400, {"error": {"message": "Missing permission"}})
    assert "Missing permission" in impact.meta_quality(E(), "t", "1", "v25.0")["error"]


def test_endpoint(tmp_path):
    key = "imp-key-0123456789abcdef"
    app = create_app({"shop": {"api_key": key, "name": "Shop", "currency": "AED"}}, str(tmp_path / "i.db"), start_worker=False)
    c = app.test_client()
    today = datetime.now(timezone.utc).date()
    rows = "\n".join(f"{i},{(today - timedelta(days=i % 40)).isoformat()},200,AED,b{i}@x.com" for i in range(80))
    c.post("/upload/csv/shop", data=("id,date,amount,currency,email\n" + rows + "\n").encode(), headers={"X-API-Key": key})
    r = c.get("/impact/shop", headers={"X-API-Key": key}).json
    assert r["coverage"]["orders"] > 0 and r["coverage"]["outside_share"] == 1.0 and r["change"]["not_started"]
    assert "Connect your Meta Pixel" in r["meta"]["error"]
    assert c.get("/impact/shop").status_code == 401
