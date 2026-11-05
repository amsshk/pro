"""PaySignal live server.

Receives payment notifications 24/7, checks they are genuine, stores them,
and regularly sends their real value to Meta.

Endpoints (one set per agency client, chosen by <client_id>):
  POST /webhooks/stripe/<client_id>    Stripe sends charge.succeeded / charge.refunded here
  POST /webhooks/shopify/<client_id>   Shopify sends orders/paid / refunds/create here
  POST /upload/csv/<client_id>         offline sales (cash, WhatsApp, POS)      [API key]
  POST /upload/posts/<client_id>       post stats for the organic engine        [API key]
  GET  /report/<client_id>             organic report + totals as JSON          [API key]
  POST /sync/meta/<client_id>          send unsent payments to Meta now         [API key]
  GET  /health

Run locally:   python -m paysignal.server
Production:    gunicorn "paysignal.server:create_app()" --workers 1 --threads 8
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, request

from .connectors import parse_csv, parse_shopify_webhook, parse_stripe_event
from .connectors.shopify import verify_hmac
from .connectors.stripe import verify_signature
from .engine import Engine
from .organic import OrganicEngine, load_posts_csv
from .store import PaymentStore

log = logging.getLogger("paysignal")
RETRAIN_SECONDS = 24 * 3600


def load_clients(path: str | None) -> dict:
    """Client settings live in a JSON file outside git (see clients.example.json),
    or in the PAYSIGNAL_CLIENTS environment variable on a hosted server."""
    raw = os.environ.get("PAYSIGNAL_CLIENTS")
    if raw:
        return json.loads(raw)
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


class App:
    def __init__(self, clients: dict, db_path: str):
        self.clients = clients
        self.store = PaymentStore(db_path)
        self.lock = threading.Lock()          # one writer at a time
        self.engines: dict[str, Engine] = {}
        self.posts: dict[str, list] = {}
        self.trained_at: dict[str, float] = {}

    def config(self, client_id: str) -> dict:
        cfg = self.clients.get(client_id)
        if cfg is None:
            abort(404, "unknown client")
        return cfg

    def engine(self, client_id: str) -> Engine:
        if client_id not in self.engines:
            cfg = self.config(client_id)
            promo = {k: tuple(v) for k, v in cfg.get("promo_map", {}).items()}
            self.engines[client_id] = Engine(client_id, store=self.store, promo_map=promo,
                                             default_margin=cfg.get("default_margin", 0.4))
        return self.engines[client_id]

    def ensure_trained(self, client_id: str, force: bool = False) -> None:
        if force or time.time() - self.trained_at.get(client_id, 0) > RETRAIN_SECONDS:
            self.engine(client_id).train(datetime.now(timezone.utc))
            self.trained_at[client_id] = time.time()

    def require_key(self, client_id: str) -> None:
        expected = self.config(client_id).get("api_key", "")
        given = request.headers.get("X-API-Key", "")
        if not expected or not hmac.compare_digest(expected, given):
            abort(401, "bad or missing X-API-Key")

    def ingest(self, client_id: str, payments) -> dict:
        with self.lock:
            new = self.engine(client_id).ingest(payments)
        return {"received": len(payments), "new": new, "duplicates": len(payments) - new}

    def sync_meta(self, client_id: str) -> dict:
        cfg = self.config(client_id)
        meta = cfg.get("meta", {})
        live = bool(meta.get("live")) and meta.get("dataset_id") and meta.get("access_token")
        with self.lock:
            self.ensure_trained(client_id)
            results = self.engine(client_id).push_to_meta(
                dataset_id=meta.get("dataset_id", ""), access_token=meta.get("access_token", ""),
                dry_run=not live, test_event_code=meta.get("test_event_code"))
        sent = sum(r["count"] if r.get("dry_run") else int(r.get("body", {}).get("events_received", 0) or 0)
                   for r in results)
        errors = [r["body"] for r in results if not r.get("dry_run") and r.get("status") != 200]
        return {"mode": "live" if live else "dry_run", "batches": len(results), "events": sent, "errors": errors}


def create_app(clients: dict | None = None, db_path: str | None = None, start_worker: bool = None) -> Flask:
    clients = clients if clients is not None else load_clients(os.environ.get("PAYSIGNAL_CLIENTS_FILE", "clients.json"))
    db_path = db_path or os.environ.get("PAYSIGNAL_DB", "paysignal.db")
    state = App(clients, db_path)
    flask = Flask(__name__)
    flask.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    flask.extensions["paysignal"] = state

    @flask.get("/health")
    def health():
        return {"ok": True, "clients": len(state.clients)}

    @flask.post("/webhooks/stripe/<client_id>")
    def stripe_hook(client_id):
        secret = state.config(client_id).get("stripe_webhook_secret")
        body = request.get_data()
        if not secret or not verify_signature(body, request.headers.get("Stripe-Signature", ""), secret):
            log.warning("rejected stripe webhook for %s: bad signature", client_id)
            abort(401, "bad signature")
        try:
            event = json.loads(body)
        except ValueError:
            abort(400, "invalid json")
        return state.ingest(client_id, parse_stripe_event(event, client_id))

    @flask.post("/webhooks/shopify/<client_id>")
    def shopify_hook(client_id):
        cfg = state.config(client_id)
        secret = cfg.get("shopify_webhook_secret")
        body = request.get_data()
        if not secret or not verify_hmac(body, request.headers.get("X-Shopify-Hmac-Sha256", ""), secret):
            log.warning("rejected shopify webhook for %s: bad hmac", client_id)
            abort(401, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            abort(400, "invalid json")
        topic = request.headers.get("X-Shopify-Topic", "")
        return state.ingest(client_id, parse_shopify_webhook(topic, payload, client_id, cfg.get("costs", {})))

    @flask.post("/upload/csv/<client_id>")
    def upload_csv(client_id):
        state.require_key(client_id)
        payments, errors = parse_csv(request.get_data(as_text=True), client_id)
        if errors and not payments:
            return jsonify({"errors": errors}), 400
        return {**state.ingest(client_id, payments), "errors": errors}

    @flask.post("/upload/posts/<client_id>")
    def upload_posts(client_id):
        state.require_key(client_id)
        posts, errors = load_posts_csv(request.get_data(as_text=True))
        if errors and not posts:
            return jsonify({"errors": errors}), 400
        with state.lock:
            state.posts[client_id] = posts
        return {"posts": len(posts), "errors": errors}

    @flask.get("/report/<client_id>")
    def report(client_id):
        state.require_key(client_id)
        with state.lock:
            state.ensure_trained(client_id)
            eng = state.engine(client_id)
            out = {"totals": eng.store.totals(client_id)}
            posts = state.posts.get(client_id)
            if posts:
                org = OrganicEngine(eng, posts)
                out["report_text"] = org.report()
                out["posts"] = [{"post_id": r.post.post_id, "views": r.post.views, "orders": r.orders,
                                 "real_value": round(r.real_value, 2), "halo_value": round(r.halo_value, 2),
                                 "value_per_1k_views": round(r.value_per_1k, 2), "tags": r.tags}
                                for r in org.scoreboard()]
                out["post_next"] = org.recommend()
            else:
                out["content"] = eng.content_report()
        return out

    @flask.post("/sync/meta/<client_id>")
    def sync_meta(client_id):
        state.require_key(client_id)
        return state.sync_meta(client_id)

    @flask.errorhandler(401)
    @flask.errorhandler(400)
    @flask.errorhandler(404)
    def err(e):
        return jsonify({"error": e.description}), e.code

    if start_worker if start_worker is not None else os.environ.get("PAYSIGNAL_WORKER", "1") == "1":
        _start_worker(state, int(os.environ.get("PAYSIGNAL_SYNC_SECONDS", "600")))
    return flask


def _start_worker(state: App, every: int) -> None:
    """Background loop: every few minutes, send new payments of every client to Meta."""
    def loop():
        while True:
            time.sleep(every)
            for cid in list(state.clients):
                try:
                    res = state.sync_meta(cid)
                    if res["events"] or res["errors"]:
                        log.info("meta sync %s: %s", cid, res)
                except Exception:                      # one client's error never stops the others
                    log.exception("meta sync failed for %s", cid)
    threading.Thread(target=loop, daemon=True, name="meta-sync").start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("PORT", "8000"))
    create_app().run(host="0.0.0.0", port=port, threaded=True)
