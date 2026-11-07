"""Ties it together: ingest payments -> score -> attribute -> send to Meta -> report."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import attribution, meta_capi
from .models import Payment
from .scoring import ValueScorer
from .store import PaymentStore


class Engine:
    def __init__(self, client_id: str, store: PaymentStore | None = None,
                 promo_map: dict[str, tuple[str, str]] | None = None, **scorer_kw):
        self.client_id = client_id
        self.store = store or PaymentStore()
        self.promo_map = promo_map or {}
        self.scorer = ValueScorer(self.store, client_id, **scorer_kw)

    def ingest(self, payments: list[Payment]) -> int:
        return self.store.add_many([p for p in payments if p.client_id == self.client_id])

    def train(self, now: datetime | None = None) -> dict:
        return self.scorer.fit(now or datetime.now(timezone.utc))

    def scored_purchases(self):
        for p in self.store.purchases(self.client_id):
            yield p, self.store.refunded_amount(self.client_id, p.external_id), self.scorer.score(p)

    def content_report(self) -> list[dict]:
        rows = [(p, ref, s.real_value) for p, ref, s in self.scored_purchases()]
        return attribution.content_report(rows, self.promo_map)

    def meta_events(self, only_unsent: bool = True) -> list[dict]:
        source = self.store.unsent(self.client_id) if only_unsent else self.store.purchases(self.client_id)
        return [meta_capi.build_purchase_event(p, self.scorer.score(p)) for p in source]

    def push_to_meta(self, dataset_id: str, access_token: str, dry_run: bool = True,
                     test_event_code: str | None = None, value_mode: str = "amount") -> list[dict]:
        """Send new purchases (with their real value) and refunds. Returns one result per batch, plus a
        "report" entry with what was skipped and why."""
        pending = self.store.unsent(self.client_id)
        refunds = self.store.unsent_refunds(self.client_id)
        # Meta refuses events older than 7 days (and the whole batch with them): skip those for good
        cutoff = datetime.now(timezone.utc) - timedelta(days=6, hours=20)
        too_old = [p for p in pending + refunds if p.occurred_at < cutoff]
        pending = [p for p in pending if p.occurred_at >= cutoff]
        refunds = [p for p in refunds if p.occurred_at >= cutoff]
        if not dry_run:
            for p in too_old:
                self.store.mark_sent(p.client_id, p.source, p.external_id)
        events = [meta_capi.build_purchase_event(p, self.scorer.score(p), value_mode=value_mode,
                                                 profit=self.scorer.margin(p) if p.cost_of_goods() is not None else None)
                  for p in pending]
        events += [meta_capi.build_refund_event(r, self.store.purchase(self.client_id, r.refund_of or "")) for r in refunds]
        sendable = [e for e in events if meta_capi.has_match_key(e)]
        results = meta_capi.send(sendable, dataset_id, access_token,
                                 test_event_code=test_event_code, dry_run=dry_run)
        if not dry_run and all(r.get("status") == 200 for r in results):
            for p in pending + refunds:              # no-identifier events can never be matched: don't retry them
                self.store.mark_sent(p.client_id, p.source, p.external_id)
        results.append({"report": True, "purchases": len(pending), "refunds": len(refunds),
                        "no_identifier": len(events) - len(sendable),
                        "too_old": len(too_old) if not dry_run else 0,
                        "keys": [sorted(k for k in meta_capi.MATCH_KEYS if k in e["user_data"])
                                 for e in sendable if e["event_name"] == "Purchase"]})
        return results
