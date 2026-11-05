"""Real value of each purchase: what it is actually worth, not what was charged.

    real_value = margin x (1 - return_risk) + expected_future_margin

- return_risk: gradient-boosting model trained on this client's own past orders
  (only orders older than the return window, so labels are final).
- expected_future_margin: how often this kind of customer comes back, times
  the margin of a typical order.
Falls back to simple averages when a client doesn't have enough history yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .identity import customer_key
from .models import Payment
from .store import PaymentStore

try:  # LightGBM is the production choice; sklearn's version is a close stand-in.
    from lightgbm import LGBMClassifier as _GBM
    _GBM_KW = dict(n_estimators=150, learning_rate=0.05, max_depth=3, min_child_samples=20, verbose=-1)
except ImportError:
    from sklearn.ensemble import HistGradientBoostingClassifier as _GBM
    _GBM_KW = dict(max_iter=150, learning_rate=0.05, max_depth=3, min_samples_leaf=20)

SHRINK = 0.2  # blend 20% of the store's base rate into every prediction (avoids 0% certainty)

FEATURES = ["log_amount", "discount_ratio", "n_items", "prior_orders",
            "prior_refund_rate", "has_click_id", "is_offline"]
MIN_TRAIN = 80          # labelled orders needed before the model is trusted
REFUND_SHARE = 0.5      # an order counts as "returned" if >=50% was refunded


@dataclass
class Score:
    margin: float
    return_risk: float
    future_value: float
    real_value: float
    model_used: str


class ValueScorer:
    def __init__(self, store: PaymentStore, client_id: str,
                 default_margin: float = 0.4, return_window_days: int = 30,
                 future_horizon_orders: float = 1.0):
        self.store, self.client_id = store, client_id
        self.default_margin = default_margin
        self.return_window = timedelta(days=return_window_days)
        self.horizon = future_horizon_orders
        self.model = None
        self.base_return_rate = 0.0
        self.repeat_rate = {"new": 0.0, "returning": 0.0}
        self.avg_margin = 0.0

    # ----- features -----
    def margin(self, p: Payment) -> float:
        cogs = p.cost_of_goods()
        return max(p.amount - cogs, 0.0) if cogs is not None else p.amount * self.default_margin

    def _history(self, p: Payment) -> list[Payment]:
        ck = customer_key(p.email, p.phone, p.customer_id)
        return self.store.customer_history(self.client_id, ck, p.occurred_at) if ck else []

    def features(self, p: Payment) -> list[float]:
        hist = self._history(p)
        hist_gross = sum(h.amount for h in hist)
        hist_ref = sum(self.store.refunded_amount(self.client_id, h.external_id) for h in hist)
        return [
            float(np.log1p(p.amount)),
            p.discount / (p.amount + p.discount) if (p.amount + p.discount) else 0.0,
            float(sum(i.quantity for i in p.items) or 1),
            float(len(hist)),
            hist_ref / hist_gross if hist_gross else 0.0,
            1.0 if (p.fbclid or p.ttclid) else 0.0,
            1.0 if p.channel == "physical_store" else 0.0,
        ]

    # ----- training -----
    def fit(self, now: datetime) -> dict:
        purchases = self.store.purchases(self.client_id)
        matured = [p for p in purchases if now - p.occurred_at >= self.return_window]
        if not matured:
            return {"trained_on": 0, "model": "none"}

        X = np.array([self.features(p) for p in matured])
        y = np.array([1 if self.store.refunded_amount(self.client_id, p.external_id) >= REFUND_SHARE * p.amount
                      else 0 for p in matured])
        self.base_return_rate = float(y.mean())
        self.avg_margin = float(np.mean([self.margin(p) for p in matured]))

        # repeat behaviour: did a customer buy again after this order?
        by_cust: dict[str, list[Payment]] = {}
        for p in purchases:
            ck = customer_key(p.email, p.phone, p.customer_id)
            if ck:
                by_cust.setdefault(ck, []).append(p)
        came_back = {"new": [], "returning": []}
        for orders in by_cust.values():
            orders.sort(key=lambda o: o.occurred_at)
            for i, o in enumerate(orders):
                if now - o.occurred_at < self.return_window * 3:
                    continue  # too recent to know
                came_back["new" if i == 0 else "returning"].append(1 if i + 1 < len(orders) else 0)
        self.repeat_rate = {k: float(np.mean(v)) if v else 0.0 for k, v in came_back.items()}

        if len(matured) >= MIN_TRAIN and 0 < y.sum() < len(y):
            self.model = _GBM(**_GBM_KW).fit(X, y)
            used = type(self.model).__name__
        else:
            self.model, used = None, "base_rate"
        return {"trained_on": len(matured), "return_rate": self.base_return_rate,
                "repeat_rate": self.repeat_rate, "model": used}

    # ----- scoring -----
    def score(self, p: Payment) -> Score:
        x = self.features(p)
        if self.model is not None:
            raw = float(self.model.predict_proba([x])[0, 1])
            risk = (1 - SHRINK) * raw + SHRINK * self.base_return_rate
        else:
            risk = self.base_return_rate
        segment = "returning" if x[3] > 0 else "new"
        future = self.repeat_rate[segment] * self.avg_margin * self.horizon
        m = self.margin(p)
        return Score(round(m, 2), round(risk, 3), round(future, 2),
                     round(m * (1 - risk) + future, 2),
                     "model" if self.model is not None else "base_rate")
