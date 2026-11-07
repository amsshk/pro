"""Revenue forecast from the business's own sales: next 30 days of real revenue (sales minus refunds).

Honest by design:
  - only shown with enough history (MIN_DAYS of data and MIN_ORDERS orders); otherwise it says what's missing;
  - a range, not one number: the range comes from testing the same method on the business's own past
    ("backtest"), so its width reflects how predictable THIS business really is;
  - the accuracy from that test is shown next to the forecast.
Method: weekday pattern x recent level x a damped trend (simple, stable, explainable).
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

MIN_DAYS = 56
MIN_ORDERS = 20
HORIZON = 30
DAMP = 0.97                 # the trend fades day by day: no runaway straight lines


def daily_net(rows: list[tuple[date, float]]) -> dict[date, float]:
    out: dict[date, float] = defaultdict(float)
    for d, v in rows:
        out[d] += v
    return out


def _model(series: list[float], start_wd: int) -> dict:
    """Fit on a full daily series (oldest first). start_wd = weekday of series[0]."""
    n = len(series)
    recent = series[-56:]
    rec_wd = [(start_wd + n - len(recent) + i) % 7 for i in range(len(recent))]
    overall = sum(recent) / len(recent) if recent else 0.0
    factors = []
    for w in range(7):
        vals = [v for v, wd in zip(recent, rec_wd) if wd == w]
        raw = (sum(vals) / len(vals) / overall) if vals and overall > 0 else 1.0
        factors.append(1 + 0.7 * (raw - 1))                      # shrink: a few weeks are noisy
    m = sum(factors) / 7
    factors = [f / m for f in factors]
    des = [v / factors[(start_wd + i) % 7] if factors[(start_wd + i) % 7] > 0 else v for i, v in enumerate(series)]
    level = sum(des[-28:]) / min(28, len(des))
    # trend: slope of weekly averages over the last up to 12 weeks
    weeks = [sum(des[i:i + 7]) / 7 for i in range(max(0, len(des) - 84), len(des) - 6, 7)]
    slope = 0.0
    if len(weeks) >= 4:
        xs = list(range(len(weeks)))
        mx, my = statistics.fmean(xs), statistics.fmean(weeks)
        den = sum((x - mx) ** 2 for x in xs)
        slope = (sum((x - mx) * (y - my) for x, y in zip(xs, weeks)) / den / 7) if den else 0.0
        slope = max(-level / 60, min(level / 60, slope))          # at most +/-50% over a month
    resid = [v - level for v in des[-56:]]
    sd = statistics.pstdev(resid) if len(resid) > 1 else 0.0
    return {"factors": factors, "level": level, "slope": slope, "sd": sd}


def _project(model: dict, first_wd: int, horizon: int) -> list[float]:
    out, cum, damp = [], 0.0, 1.0
    for h in range(1, horizon + 1):
        damp *= DAMP
        cum += damp
        base = model["level"] + model["slope"] * cum
        out.append(max(0.0, base * model["factors"][(first_wd + h - 1) % 7]))
    return out


def build_forecast(rows: list[tuple[date, float]], orders: int, end: date, currency: str,
                   horizon: int = HORIZON) -> dict:
    """rows: (day, net revenue of a sale) for every sale. end: last day of data."""
    daily = daily_net(rows)
    if not daily:
        return {"ready": False, "needs": "Upload your sales history to see a forecast."}
    first = min(daily)
    span = (end - first).days + 1
    if span < MIN_DAYS or orders < MIN_ORDERS:
        need = []
        if span < MIN_DAYS:
            need.append(f"{MIN_DAYS - span} more days of sales history")
        if orders < MIN_ORDERS:
            need.append(f"{MIN_ORDERS - orders} more orders")
        return {"ready": False, "needs": "A forecast needs at least 8 weeks of history and 20 orders: add " +
                " and ".join(need) + ".", "days": span, "orders": orders}
    start = max(first, end - timedelta(days=364))
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    series = [daily.get(d, 0.0) for d in days]

    # backtest: forecast past months with only the data before them, compare with what really happened
    errors = []
    for k in range(1, 7):
        cut = len(series) - horizon * k
        if cut < MIN_DAYS:
            break
        m = _model(series[:cut], days[0].weekday())
        predicted = sum(_project(m, days[cut].weekday(), horizon))
        actual = sum(series[cut:cut + horizon])
        if actual > 0:
            errors.append(abs(predicted - actual) / actual)
    model = _model(series, days[0].weekday())
    fc = _project(model, (end + timedelta(days=1)).weekday(), horizon)
    total = sum(fc)
    # range: from the backtest when there is one, else a cautious default; never narrower than 10%
    spread = max(0.10, sorted(errors)[int(0.8 * (len(errors) - 1))] if errors else 0.35)
    if len(errors) < 2:
        spread = max(spread, 0.30)
    points = []
    for h, v in enumerate(fc, start=1):
        width = 1.28 * model["sd"] * model["factors"][(end + timedelta(days=h)).weekday()] * math.sqrt(1 + h / 30)
        points.append({"date": (end + timedelta(days=h)).isoformat(), "expected": round(v, 2),
                       "low": round(max(0.0, v - width), 2), "high": round(v + width, 2)})
    last30 = sum(series[-30:])
    wd_names = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]
    best = max(range(7), key=lambda i: model["factors"][i])
    trend_month = model["slope"] * 30 / model["level"] if model["level"] > 0 else 0.0
    return {
        "ready": True, "currency": currency, "from": (end + timedelta(days=1)).isoformat(), "horizon": horizon,
        "history": [{"date": d.isoformat(), "actual": round(v, 2)} for d, v in zip(days[-60:], series[-60:])],
        "forecast": points,
        "total": {"expected": round(total, 2), "low": round(total * (1 - spread), 2), "high": round(total * (1 + spread), 2)},
        "last_30_days": round(last30, 2),
        "change_vs_last_30": round(total / last30 - 1, 3) if last30 > 0 else None,
        "accuracy": {"tests": len(errors), "average_error": round(statistics.fmean(errors), 3) if errors else None},
        "drivers": {"trend_per_month": round(trend_month, 3), "best_day": wd_names[best],
                    "best_day_lift": round(model["factors"][best] - 1, 2)},
        "note": "Based only on your own sales after refunds. The forecast assumes growth slows down rather than continuing in a straight line. Ads, offers and seasons you change will move it.",
    }
