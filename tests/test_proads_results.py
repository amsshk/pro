import json
import time
from datetime import datetime, timedelta, timezone

from paysignal import proads_results
from paysignal.models import Payment
from paysignal.server import create_app


def P(eid, amount, **kw):
    return Payment(client_id="c", source="shopify", external_id=eid, kind="purchase", amount=amount, currency="AED",
                   occurred_at=datetime(2026, 11, 20, tzinfo=timezone.utc), **kw)


def test_only_ad_tracked_paid_orders_count():
    ps = [P("1", 485, ttclid="x"), P("2", 280), P("3", 300, utm_source="instagram", utm_medium="paid"), P("4", 100, ttclid="y")]
    out = proads_results.build(ps, lambda p: 100 if p.external_id == "4" else 0, datetime(2026, 11, 1, tzinfo=timezone.utc),
                               [{"platform": "TikTok", "id": "1", "name": "A", "status": "ENABLE"}], {"spend": 500, "clicks": 900, "known": True},
                               {"meta": 0, "tiktok": 5}, 107)
    assert out["orders"] == 2 and out["orders_by"] == {"tiktok": 1, "meta": 1} and out["money"] == 785
    assert out["back_per_1"] == 1.57 and out["cost_per_order"] == 250 and out["live"] == 1 and out["sent_tiktok"] == 5


def test_unknown_spend_is_not_guessed():
    out = proads_results.build([], lambda p: 0, None, [{"platform": "Meta", "id": "1", "name": "A", "status": "PAUSED"}],
                               {"spend": 0, "clicks": 0, "known": False}, {}, None)
    assert out["spend"] is None and out["clicks"] is None and out["back_per_1"] is None


def test_endpoint(tmp_path):
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "r.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Glow Clinic", "email": "o@glow.ae", "password": "first-password-1", "terms": True,
                                       "phone": "0501234567"}).json["token"]
    db = app.extensions["paysignal"].store.db
    db.execute("INSERT INTO tiktok_delivered (client_id, event_id, pixel_code, sent_at, status, reason) VALUES ('glow-clinic','e1','P',?,'accepted',NULL)",
               (int(time.time()),))
    db.commit()
    r = c.get("/proads-results/glow-clinic", headers={"X-API-Key": tok}).json
    assert r["campaigns"] == 0 and r["sent_tiktok"] == 1 and r["tiktok_sent_total"] == 1 and r["meta_sent_total"] == 0 and r["since"]


def test_health_score():
    good = proads_results.health_score(50, 50, 1.0, None)
    assert good["score"] == 100 and good["label"] == "Good"
    weak = proads_results.health_score(5, 6, 0.4, 3)
    assert weak["score"] < 50 and weak["parts"]["focus"] == 5 and "purchases" in weak["tip"].lower()
