import random
from datetime import date, timedelta

from paysignal.forecast import build_forecast
from paysignal.server import create_app

END = date(2026, 11, 6)


def sales(days, base=1000.0, growth=0.0, weekend=1.0, noise=0.1, seed=1):
    rnd = random.Random(seed)
    rows = []
    for i in range(days):
        d = END - timedelta(days=days - 1 - i)
        v = base * (1 + growth * i / 30) * (weekend if d.weekday() >= 4 else 1) * (1 + rnd.uniform(-noise, noise))
        rows.append((d, v))
    return rows


def test_needs_enough_history():
    r = build_forecast(sales(30), 40, END, "AED")
    assert not r["ready"] and "26 more days" in r["needs"]
    assert not build_forecast(sales(90), 5, END, "AED")["ready"]


def test_steady_business_forecast_is_accurate_and_ranged():
    r = build_forecast(sales(200), 400, END, "AED")
    t = r["total"]
    assert r["ready"] and 27000 < t["expected"] < 33000 and t["low"] < t["expected"] < t["high"]
    assert r["accuracy"]["tests"] >= 4 and r["accuracy"]["average_error"] < 0.1
    assert len(r["forecast"]) == 30 and r["forecast"][0]["date"] == "2026-11-07" and len(r["history"]) == 60


def test_learns_weekdays_and_damped_growth():
    r = build_forecast(sales(200, weekend=1.8, noise=0.02), 400, END, "AED")
    assert r["drivers"]["best_day"] in ("Fridays", "Saturdays", "Sundays") and r["drivers"]["best_day_lift"] > 0.2
    g = build_forecast(sales(200, growth=0.1, noise=0.02), 400, END, "AED")
    assert g["drivers"]["trend_per_month"] > 0.03 and g["change_vs_last_30"] > 0
    assert g["total"]["expected"] < g["last_30_days"] * 1.5          # damped: no runaway line


def test_endpoint(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Glow", "email": "o@glow.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    assert c.get("/forecast/glow", headers=h).json["ready"] is False
    today = date.today()
    rows = "\n".join(f"{i},{(today - timedelta(days=i % 120)).isoformat()},{200 + (i % 7) * 10},AED" for i in range(300))
    c.post("/import/glow", data="id,date,amount,currency\n" + rows + "\n", headers=h)
    r = c.get("/forecast/glow", headers=h).json
    assert r["ready"] and r["total"]["expected"] > 0 and r["currency"] == "AED"
