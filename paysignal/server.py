"""ProAds live server.

Receives payment notifications 24/7, checks they are genuine, stores them,
and regularly sends their real value to Meta.

Endpoints (one set per agency client, chosen by <client_id>):
  POST /webhooks/stripe/<client_id>    Stripe sends charge.succeeded / charge.refunded here
  POST /webhooks/shopify/<client_id>   Shopify sends orders/paid / refunds/create here
  POST /upload/csv/<client_id>         offline sales (cash, WhatsApp, POS)      [API key]
  POST /upload/posts/<client_id>       post stats for the organic engine        [API key]
  GET  /report/<client_id>             organic report + totals as JSON          [API key]
  POST /sync/meta/<client_id>          send unsent payments to Meta now         [API key]
  POST /import/<client_id>             day one: Shopify/Stripe/posts history     [API key]
  GET  /audit/<client_id>              first-week audit (?summary=1 for Muse)    [API key]
  POST /ask/<client_id>                ask a question about the results (AI)    [API key]
  POST /campaign/<client_id>           draft a Meta campaign + organic plan (AI)  [API key]
  POST /campaign/<client_id>/publish   create it in Meta Ads Manager, paused       [admin]
  POST /campaign/<client_id>/launch/<campaign_id>  go live / pause             [admin]
  GET  /health

Run locally:   python -m paysignal.server
Production:    gunicorn "paysignal.server:create_app()" --workers 1 --threads 8
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from typing import Optional
from urllib.parse import urlencode

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

from .connectors import parse_csv, parse_shopify_webhook, parse_stripe_event
from .connectors.shopify import verify_hmac
from .connectors.stripe import verify_signature
from .engine import Engine
from . import growth, mailer, shopify_app, whatsapp
from .identity import hash_phone
from .accounts import AccountStore, Limiter, check_password, hash_password, validate_signup
from .ai import MetaAI
from .attribution import make_promo_code, tag_link
from .dashboard import build_dashboard, _realized
from .ads_report import build_report
from .assistant import PROADS_PRODUCT, CampaignError, ask, build_campaign, business_context
from . import billing, competitors, learning, meta_ads, meta_connect
from .connectors.stripe import verify_signature as stripe_signature_ok
from .audit import audit_summary, audit_text, build_audit
from .importers import xlsx_to_csv, import_auto
from .labeler import label_posts, make_labeler
from .organic import OrganicEngine, load_posts_csv
from .store import PaymentStore

log = logging.getLogger("paysignal")
RETRAIN_SECONDS = 24 * 3600
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
_DUMMY_HASH = hash_password("not-a-real-password")


def load_clients(path: str | None) -> dict:
    """Client settings live in a JSON file outside git (see clients.example.json),
    or in the PAYSIGNAL_CLIENTS environment variable on a hosted server."""
    raw = (os.environ.get("PAYSIGNAL_CLIENTS") or "").strip()
    source = "the PAYSIGNAL_CLIENTS setting"
    if not raw and path and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            raw, source = f.read().strip(), path
    if not raw:
        return {}
    # curly quotes sneak in when copying from chat apps or documents
    raw = raw.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    try:
        clients = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"ProAds cannot start: {source} is not valid JSON ({e.msg} at character {e.pos}). "
            'It must start with { and look like: {"client-id": {"api_key": "...", "currency": "AED"}}. '
            "Check that it is the whole JSON, not only the key, and that quotes are straight quotes.") from None
    if not isinstance(clients, dict) or not all(isinstance(v, dict) for v in clients.values()):
        raise SystemExit(f'ProAds cannot start: {source} must be {{"client-id": {{...settings...}}}}.')
    for cid, cfg in clients.items():
        if not cfg.get("api_key") or len(str(cfg["api_key"])) < 20 or "REPLACE" in str(cfg["api_key"]).upper() \
                or "PUT-YOUR-KEY" in str(cfg["api_key"]).upper():
            log.warning("client %s has a missing, short or placeholder api_key: set a long random key", cid)
    return clients


class App:
    def __init__(self, clients: dict, db_path: str):
        self.clients = clients
        self.store = PaymentStore(db_path)
        self.lock = threading.Lock()          # one writer at a time
        self.engines: dict[str, Engine] = {}
        self.posts: dict[str, list] = {}
        self.trained_at: dict[str, float] = {}
        self.ai: MetaAI | None = None          # set in tests; otherwise built from MODEL_API_KEY
        self._shopify_http = None              # set in tests; otherwise a requests session
        self._imports: dict[str, threading.Thread] = {}
        self.accounts = AccountStore(self.store.db)
        self.login_limiter = Limiter()                       # 5 wrong tries -> 15 min lock
        self.signup_limiter = Limiter(max_failures=10, window=3600)
        self.growth_http = None                # set in tests
        self.growth_sent: list | None = None   # set in tests to capture sent events
        self.wa_http = None                    # set in tests
        self.wa_sync = False                   # tests: send WhatsApp events inline
        self.launches = meta_ads.LaunchStore(self.store.db)
        self.drafts = meta_ads.DraftStore(self.store.db)
        self.learnings = learning.LearningStore(self.store.db, os.environ.get("PROADS_SECRET_KEY", "proads"))
        self.ads_http = None                   # set in tests
        self.meta_conns = meta_connect.ConnectionStore(self.store.db)
        self.meta_http = None                  # set in tests
        self.billing = billing.BillingStore(self.store.db)
        self.stripe_http = None                # set in tests
        self.billing_sync = False              # tests: add invoice fees inline
        self.ads_cache: dict = {}              # (client, period) -> (time, report)

    def known(self, client_id: str) -> bool:
        return client_id in self.clients or self.accounts.get(client_id) is not None

    def config(self, client_id: str) -> dict:
        cfg = self.clients.get(client_id)
        if cfg is None:
            acct = self.accounts.get(client_id)
            if acct is None:
                abort(404, "unknown client")
            cfg = {"name": acct["name"], "currency": acct["currency"], "website": acct["website"] or "",
                   "self_serve": True}
        if not (cfg.get("meta") or {}).get("access_token"):
            meta = self.connected_meta(client_id)
            if meta:
                cfg = {**cfg, "meta": meta}
        return cfg

    def connected_meta(self, client_id: str) -> Optional[dict]:
        """Conversions API settings from the client's own Facebook connection, if they chose a Pixel."""
        c = self.meta_conns.get(client_id)
        if not c or not c["pixel_id"] or not c["send_sales"] or (c["expires_at"] and c["expires_at"] < time.time()):
            return None
        try:
            return {"dataset_id": c["pixel_id"], "access_token": meta_connect.decrypt(c["token"]), "live": True}
        except Exception:
            log.warning("could not read the Meta connection of %s", client_id)
            return None

    def ads_settings(self, client_id: str, is_admin: bool) -> Optional[dict]:
        """Where campaigns get published: the client's own connected ad account, or (for the
        ProAds admin without one) the ad account in the server settings."""
        c = self.meta_conns.get(client_id)
        if c and meta_connect.public_view(c)["ready"]:
            return meta_connect.ads_settings(c, meta_connect.decrypt(c["token"]))
        if is_admin and not meta_ads.missing():
            return meta_ads.settings()
        return None

    def maybe_activate(self, client_id: str) -> None:
        """First data in from a self-serve account: tell Meta a real user started (once)."""
        acct = self.accounts.get(client_id)
        if not acct or acct["activated_at"]:
            return
        with self.lock:
            first = self.accounts.mark_activated(client_id)
        ctx = growth.load_ctx(acct["ads_ctx"])
        if first and ctx:
            growth.send_later(growth.build_event("StartTrial", acct, ctx, f"activate:{client_id}"),
                              self.growth_http, self.growth_sent)

    def engine(self, client_id: str) -> Engine:
        if client_id not in self.engines:
            cfg = self.config(client_id)
            promo = {k.upper(): tuple(v) for k, v in cfg.get("promo_map", {}).items()}
            promo.update({r["code"]: (r["platform"], r["post_id"]) for r in self.store.promos(client_id)})
            self.engines[client_id] = Engine(client_id, store=self.store, promo_map=promo,
                                             default_margin=cfg.get("default_margin", 0.4))
        return self.engines[client_id]

    def posts_for(self, client_id: str) -> list:
        if client_id not in self.posts:
            self.posts[client_id] = self.store.load_posts(client_id)
        return self.posts[client_id]

    def save_posts(self, client_id: str, posts: list) -> None:
        """Merge by post id and keep in the database, so posts survive restarts."""
        merged = {p.post_id: p for p in self.posts_for(client_id)}
        merged.update({p.post_id: p for p in posts})
        self.store.save_posts(client_id, posts)
        self.posts[client_id] = sorted(merged.values(), key=lambda p: p.posted_at)

    def ensure_trained(self, client_id: str, force: bool = False) -> None:
        if force or time.time() - self.trained_at.get(client_id, 0) > RETRAIN_SECONDS:
            self.engine(client_id).train(datetime.now(timezone.utc))
            self.trained_at[client_id] = time.time()

    def require_key(self, client_id: str) -> None:
        """Accepts the business's access key (server settings) or a sign-in session token."""
        cfg = self.config(client_id)
        lock_key = f"key:{client_id}"
        wait = self.login_limiter.locked_for(lock_key)
        if wait:
            abort(429, f"Too many wrong attempts. Try again in {wait // 60 + 1} minutes.")
        given = request.headers.get("X-API-Key", "")
        expected = cfg.get("api_key", "")
        if expected and hmac.compare_digest(expected, given):
            return
        if given and self.accounts.session_client(given) == client_id:
            return
        self.login_limiter.fail(lock_key)
        abort(401, "bad or missing X-API-Key")

    def ingest(self, client_id: str, payments) -> dict:
        """Store payments. Sales from customers who first chatted from a WhatsApp ad are
        credited to that ad, and (if live) reported to Meta."""
        wa_matches = []
        with self.lock:
            new = 0
            for p in payments:
                if p.client_id != client_id:
                    continue
                match = self._wa_match(client_id, p)
                if self.store.add(p):
                    new += 1
                    if match and match.get("ctwa_clid"):
                        wa_matches.append((p, match))
        for p, match in wa_matches:
            self._wa_report(client_id, p, match)
        return {"received": len(payments), "new": new, "duplicates": len(payments) - new}

    # ----- WhatsApp -----
    def waba_client(self, waba_id: str) -> Optional[str]:
        for cid, cfg in self.clients.items():
            if str((cfg.get("whatsapp") or {}).get("waba_id") or "") == waba_id and waba_id:
                return cid
        return None

    def _wa_match(self, client_id: str, p) -> Optional[dict]:
        """If this purchase's phone belongs to a chat that came from an ad, credit the ad."""
        if p.kind != "purchase" or not p.phone:
            return None
        ph = hash_phone(p.phone)
        match = whatsapp.attribution_for(self.store.chat_for(client_id, ph) if ph else None, p.occurred_at)
        if match and not (p.utm_content or p.utm_source or p.promo_code):
            p.utm_source, p.utm_medium, p.utm_content = match["utm_source"], match["utm_medium"], match["utm_content"]
        return match

    def _wa_report(self, client_id: str, p, match: dict) -> None:
        cfg = (self.clients.get(client_id) or {}).get("whatsapp") or {}
        source_id = match["utm_content"].split(":", 1)[1]
        try:
            value = self.engine(client_id).scorer.score(p).real_value
        except Exception:
            value = p.amount
        live = bool(cfg.get("live") and cfg.get("dataset_id") and cfg.get("access_token") and cfg.get("waba_id"))
        status = "too old for Meta (over 7 days)" if whatsapp.too_old_for_meta(p) else ("sending" if live else "practice mode")
        with self.lock:
            first = self.store.log_wa_conversion(client_id, p.external_id, source_id, value, p.currency, status)
        if not first or status != "sending":
            return
        event = whatsapp.purchase_event(p, value, str(cfg["waba_id"]), match["ctwa_clid"])

        def run():
            res = whatsapp.send(event, str(cfg["dataset_id"]), cfg["access_token"], self.wa_http)
            with self.lock:
                self.store.set_wa_conversion(client_id, p.external_id, res["status"])
        if self.wa_sync:
            run()
        else:
            threading.Thread(target=run, daemon=True, name="wa-purchase").start()

    # ----- Shopify app -----
    def shopify_http(self):
        if self._shopify_http is None:
            import requests
            self._shopify_http = requests.Session()
        return self._shopify_http

    def shopify_client(self, client_id: str) -> shopify_app.ShopifyClient:
        """A client with a fresh access token (Shopify access tokens last one hour)."""
        with self.lock:
            shop = self.store.shop_for(client_id)
        if not shop:
            raise shopify_app.ShopifyError("No Shopify store is connected")
        tokens = json.loads(shopify_app.decrypt(shop["tokens"]))
        now = time.time()
        if tokens.get("access_expires") and now >= tokens["access_expires"]:
            if not tokens.get("refresh_token") or (tokens.get("refresh_expires") or 0) <= now:
                raise shopify_app.ShopifyError("Shopify access expired: please connect the store again")
            tokens = shopify_app.refresh_tokens(self.shopify_http(), shop["shop"], tokens["refresh_token"])
            with self.lock:
                self.store.update_shop_tokens(client_id, shopify_app.encrypt(json.dumps(tokens)))
        return shopify_app.ShopifyClient(shop["shop"], tokens["access_token"], self.shopify_http())

    def start_shopify_import(self, client_id: str, register_webhooks: str | None = None) -> None:
        with self.lock:
            running = self._imports.get(client_id)
            if running and running.is_alive():
                return
            self.store.set_sync(client_id, "running", "Importing orders…")
            t = threading.Thread(target=self._shopify_import, args=(client_id, register_webhooks),
                                 daemon=True, name=f"shopify-import-{client_id}")
            self._imports[client_id] = t
        t.start()

    def _shopify_import(self, client_id: str, register_webhooks: str | None) -> None:
        try:
            client = self.shopify_client(client_id)
            problems = client.register_webhooks(register_webhooks) if register_webhooks else []
            costs = self.config(client_id).get("costs", {}) if self.known(client_id) else {}
            orders, batch = 0, []
            for node in client.orders():
                pays = shopify_app.order_to_payments(node, client_id, costs)
                orders += sum(p.kind == "purchase" for p in pays)
                batch += pays
                if len(batch) >= 200:
                    self.ingest(client_id, batch)
                    batch = []
                    with self.lock:
                        self.store.set_sync(client_id, "running", f"Importing orders… {orders} so far")
            if batch:
                self.ingest(client_id, batch)
            with self.lock:
                scope = (self.store.shop_for(client_id) or {}).get("scope") or ""
            notes = [f"{orders} paid orders imported"]
            if "read_all_orders" not in scope:
                notes.append("Shopify allows the last 60 days only")
            if not client.protected:
                notes.append("without customer emails and phones (needs Shopify's approval)")
            if problems:
                notes.append("live updates not fully set up: " + "; ".join(problems)[:200])
            with self.lock:
                self.store.set_sync(client_id, "done", " · ".join(notes), imported=orders)
                self.trained_at.pop(client_id, None)
            if orders:
                self.maybe_activate(client_id)
        except Exception as e:
            log.exception("shopify import failed for %s", client_id)
            with self.lock:
                self.store.set_sync(client_id, "error", str(e)[:300])

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


def _admins(state: "App") -> set[str]:
    names = {x.strip() for x in os.environ.get("PROADS_ADMINS", "pro").split(",") if x.strip()}
    return {cid for cid, cfg in state.clients.items() if cfg.get("admin") or cid in names}


def _clear_admin_example_data(state: "App") -> None:
    """Once: remove example sales and posts that were uploaded to the ProAds account itself while testing.
    ProAds' own Home shows sign-ups and clients instead, so these were only ever made-up numbers."""
    db = state.store.db
    db.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, done_at TEXT NOT NULL DEFAULT (datetime('now')))")
    if db.execute("SELECT 1 FROM migrations WHERE name='clear_admin_example_data'").fetchone():
        return
    for cid in _admins(state):
        n = db.execute("DELETE FROM payments WHERE client_id=?", (cid,)).rowcount
        m = db.execute("DELETE FROM posts WHERE client_id=?", (cid,)).rowcount
        if n or m:
            log.info("removed %d example sales and %d posts from the %s account", n, m, cid)
    db.execute("INSERT INTO migrations (name) VALUES ('clear_admin_example_data')")
    db.commit()


def create_app(clients: dict | None = None, db_path: str | None = None, start_worker: bool = None) -> Flask:
    clients = clients if clients is not None else load_clients(os.environ.get("PAYSIGNAL_CLIENTS_FILE", "clients.json"))
    db_path = db_path or os.environ.get("PAYSIGNAL_DB", "paysignal.db")
    state = App(clients, db_path)
    flask = Flask(__name__)
    flask.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    flask.extensions["paysignal"] = state
    _clear_admin_example_data(state)

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
        out = {**state.ingest(client_id, payments), "errors": errors}
        if out["new"]:
            state.maybe_activate(client_id)
        return out

    @flask.post("/upload/posts/<client_id>")
    def upload_posts(client_id):
        state.require_key(client_id)
        posts, errors = load_posts_csv(request.get_data(as_text=True))
        if errors and not posts:
            return jsonify({"errors": errors}), 400
        labels = label_posts(posts, make_labeler())     # Meta AI, Claude, or rules: whichever is set up
        with state.lock:
            state.save_posts(client_id, posts)
        state.maybe_activate(client_id)
        return {"posts": len(posts), "errors": errors, "labels": labels}

    @flask.post("/import/<client_id>")
    def import_history(client_id):
        """Day one: upload a Shopify/Stripe export, a posts export or a sales CSV. Type is detected."""
        state.require_key(client_id)
        cfg = state.config(client_id)
        raw = request.get_data()
        if raw[:2] == b"PK":                              # an Excel file (.xlsx)
            try:
                text = xlsx_to_csv(raw)
            except Exception:
                abort(400, "This Excel file couldn't be read. Save it as CSV and upload again.")
        else:
            text = raw.decode("utf-8-sig", errors="replace")
        res = import_auto(text, client_id, cfg.get("costs", {}), request.args.get("platform", "instagram"),
                          cfg.get("currency", "AED"))
        if res["kind"] == "unknown":
            return jsonify({"errors": res["errors"]}), 400
        out = {"kind": res["kind"], "errors": res["errors"]}
        if res["payments"]:
            out.update(state.ingest(client_id, res["payments"]))
            state.trained_at.pop(client_id, None)       # retrain with the new history
        if res["posts"]:
            labels = label_posts(res["posts"], make_labeler())
            with state.lock:
                state.save_posts(client_id, res["posts"])
            out.update(posts=len(res["posts"]), labels=labels)
        if out.get("new") or res["posts"]:
            state.maybe_activate(client_id)
        return out

    @flask.get("/audit/<client_id>")
    def audit_endpoint(client_id):
        """First-week audit. ?summary=1&language=Arabic adds a Muse-written summary."""
        state.require_key(client_id)
        with state.lock:
            eng = state.engine(client_id)
            audit = build_audit(eng, (state.posts_for(client_id) or None), currency=state.config(client_id).get("currency", "AED"))
            state.trained_at[client_id] = time.time()
        if "error" in audit:
            return jsonify(audit), 400
        if not is_admin(client_id):
            with state.lock:
                state.learnings.keep(client_id, audit)       # anonymous ratios only, see learning.py
        out = {"audit": audit, "text": audit_text(audit)}
        if request.args.get("summary") == "1":
            ai = state.ai or MetaAI()
            if ai.available:
                try:
                    out["summary"] = audit_summary(ai, audit, request.args.get("language", "English"))
                except (PermissionError, RuntimeError) as e:
                    out["summary_error"] = str(e)
            else:
                out["summary_error"] = "AI is not set up: add MODEL_API_KEY from dev.meta.ai"
        return out

    @flask.post("/data/<client_id>/clear-sales")
    def clear_sales(client_id):
        """Remove every imported sale and post (e.g. example files), keeping the account and its connections."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "DELETE":
            abort(400, 'send {"confirm": "DELETE"}')
        kept = False
        with state.lock:
            if body.get("keep_lessons"):
                audit = build_audit(state.engine(client_id), (state.posts_for(client_id) or None),
                                    currency=state.config(client_id).get("currency", "AED"))
                kept = state.learnings.keep(client_id + ":archived", audit)
            else:
                state.learnings.forget(client_id)
            n = state.store.db.execute("DELETE FROM payments WHERE client_id=?", (client_id,)).rowcount
            posts = state.store.db.execute("DELETE FROM posts WHERE client_id=?", (client_id,)).rowcount
            state.store.db.commit()
            for cache in (state.engines, state.posts, state.trained_at):
                cache.pop(client_id, None)
        log.info("%s cleared %d sales and %d posts", client_id, n, posts)
        return {"sales_deleted": n, "posts_deleted": posts, "lessons_kept": kept}

    def proads_dashboard(period: str) -> dict:
        """Home for the ProAds account itself: real sign-ups, active companies, paying clients, campaigns."""
        from datetime import date as _date, timedelta as _td
        days = {"today": 1, "7": 7, "30": 30, "90": 90, "365": 365}.get(period, 30)
        end = datetime.now(timezone.utc).date()
        start, prev_start = end - _td(days=days - 1), end - _td(days=2 * days - 1)
        db = state.store.db
        def per_day(sql):
            cur, prev = [0] * days, [0] * days
            for (d,) in db.execute(sql).fetchall():
                if not d:
                    continue
                day = _date.fromisoformat(str(d)[:10])
                if start <= day <= end:
                    cur[(day - start).days] += 1
                elif prev_start <= day < start:
                    prev[(day - prev_start).days] += 1
            return cur, prev
        sign, sign_p = per_day("SELECT created_at FROM accounts")
        act, act_p = per_day("SELECT activated_at FROM accounts")
        camp, camp_p = per_day("SELECT created_at FROM ad_launches")
        paying = sum(1 for r in db.execute("SELECT status FROM billing").fetchall() if r[0] in billing.ACTIVE)
        chg = lambda a, b: round((a - b) / b, 3) if b else None
        m = [("signups", "Sign-ups", sum(sign), sum(sign_p)), ("activated", "Became active", sum(act), sum(act_p)),
             ("campaigns", "Campaigns published", sum(camp), sum(camp_p))]
        newest = [{"name": r[0], "created_at": r[1], "active": bool(r[2])} for r in db.execute(
            "SELECT name, created_at, activated_at FROM accounts ORDER BY created_at DESC LIMIT 6").fetchall()]
        return {"mode": "proads", "period": period, "days": days,
                "dates": [(start + _td(days=i)).isoformat() for i in range(days)],
                "metrics": [{"key": k, "label": l, "value": v, "previous": pv, "change": chg(v, pv)} for k, l, v, pv in m]
                           + [{"key": "paying", "label": "Paying clients", "value": paying, "previous": paying, "change": None}],
                "series": {"signups": {"current": sign, "previous": sign_p}, "activated": {"current": act, "previous": act_p},
                           "campaigns": {"current": camp, "previous": camp_p}},
                "accounts_total": db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
                "newest": newest, "campaigns": [{k: l.get(k) for k in ("campaign_id", "name", "status")}
                                                for l in state.launches.list(next(iter(_admins(state)), "pro"))[:5]]}

    @flask.get("/ads/<client_id>")
    def ads_performance(client_id):
        """Live results of the campaigns ProAds published, where they come from, and the truth check."""
        state.require_key(client_id)
        period = request.args.get("period", "30")
        days = {"today": 1, "7": 7, "30": 30, "90": 90, "365": 365}.get(period, 30)
        admin = is_admin(client_id)
        with state.lock:
            launches = state.launches.list(client_id)
        if not launches:
            return {"has_campaigns": False}
        hit = state.ads_cache.get((client_id, period))
        if hit and time.time() - hit[0] < 600 and not request.args.get("fresh"):
            return hit[1]
        s = state.ads_settings(client_id, admin)
        if s is None:
            return {"has_campaigns": True, "error": "Connect Facebook & Instagram again to see live results."}
        kinds = [l.get("conversion") for l in launches if l.get("conversion")]
        kind = "signup" if admin else (max(set(kinds), key=kinds.count) if kinds else "purchase")

        def truth(start, end):
            lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
            with state.lock:
                if kind == "signup" and admin:
                    rows = state.store.db.execute("SELECT ads_ctx, activated_at, client_id FROM accounts WHERE created_at >= ?"
                                                  " AND created_at < ?", (lo, hi)).fetchall()
                    paid = [r for r in rows if "utm_medium=paid" in (r[0] or "")]
                    paying = sum(1 for r in paid if state.billing.active(r[2]))
                    return {"label": "real sign-ups from your ads", "real": len(paid),
                            "active": sum(1 for r in paid if r[1]), "paying": paying, "all_signups": len(rows)}
                eng = state.engine(client_id)
                orders = revenue = refunds = profit = 0.0
                buyers = set()
                for p in eng.store.purchases(client_id):
                    d = p.occurred_at.date()
                    if not (start <= d <= end) or (p.utm_medium or "").lower() != "paid" or \
                            (p.utm_source or "").lower() not in ("meta", "facebook", "instagram", "fb", "ig"):
                        continue
                    ref = eng.store.refunded_amount(client_id, p.external_id)
                    orders += 1; revenue += p.amount; refunds += ref; profit += _realized(eng, p, ref)
                    buyers.add(p.email or p.phone or p.external_id)
                return {"label": "real paying orders from your ads", "real": int(orders), "buyers": len(buyers),
                        "revenue": round(revenue, 2), "refunds": round(refunds, 2), "real_profit": round(profit, 2),
                        "currency": state.config(client_id).get("currency", "AED")}
        try:
            rep = build_report(meta_ads.Publisher(state.ads_http, s), [l["campaign_id"] for l in launches], kind, days,
                               datetime.now(timezone.utc).date(), truth)
        except meta_ads.PublishError as e:
            return {"has_campaigns": True, "error": str(e)}
        rep.update(has_campaigns=True, live=sum(1 for l in launches if l["status"] == "ACTIVE"), campaigns=len(launches),
                   updated_at=int(time.time()))
        state.ads_cache[(client_id, period)] = (time.time(), rep)
        return rep

    @flask.get("/dashboard/<client_id>")
    def dashboard_endpoint(client_id):
        """Home: a period vs the one before (?period=today|7|30|90|365), plus a setup guide."""
        state.require_key(client_id)
        cfg = state.config(client_id)
        if is_admin(client_id):
            with state.lock:
                return proads_dashboard(request.args.get("period", "30"))
        with state.lock:
            out = build_dashboard(state.engine(client_id), request.args.get("period", "30"),
                                  currency=cfg.get("currency", "AED"))
            codes = len(state.store.promos(client_id))
            launches = state.launches.list(client_id)
            conn = meta_connect.public_view(state.meta_conns.get(client_id))
            shop = state.store.shop_for(client_id)
            plan = state.billing.active(client_id)
            out["ads_access"] = state.billing.ads_access(client_id) if billing.available() and not is_admin(client_id) else None
        out["campaigns"] = [{k: l.get(k) for k in ("campaign_id", "name", "status", "daily_budget", "currency",
                                                   "manager_url")} for l in launches[:5]]
        out["setup"] = [
            {"label": "Bring in your sales", "done": out["has_data"] or bool(shop), "href": "#/platforms",
             "how": "Upload a sales list or connect Shopify"},
            {"label": "Connect Facebook & Instagram", "done": bool(conn.get("ready")) or is_admin(client_id),
             "href": "#/upload", "how": "Pick your ad account, Page and Pixel"},
            {"label": "Track your next post", "done": codes > 0, "href": "#/upload",
             "how": "Give each post its own link and code"},
            {"label": "Launch your first campaign", "done": bool(launches), "href": "#/assistant",
             "how": "ProAds builds it, you press Go live"},
        ]
        if not is_admin(client_id) and billing.available():
            out["setup"].append({"label": "Choose your plan", "done": plan, "href": "#/billing",
                                 "how": "Free trial, cancel any time"})
        return out

    @flask.get("/report/<client_id>")
    def report(client_id):
        state.require_key(client_id)
        with state.lock:
            state.ensure_trained(client_id)
            eng = state.engine(client_id)
            out = {"totals": eng.store.totals(client_id)}
            posts = (state.posts_for(client_id) or None)
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

    def ai_context(client_id):
        """Aggregated business data for the AI (built under the lock, sent outside it)."""
        with state.lock:
            state.ensure_trained(client_id)
            eng = state.engine(client_id)
            posts = (state.posts_for(client_id) or None)
            org = OrganicEngine(eng, posts) if posts else None
            ctx = business_context(eng, org, state.config(client_id).get("currency", "AED"))
            bench = state.learnings.benchmarks()
            if bench:
                ctx["benchmarks_from_other_businesses"] = bench
            return ctx

    def need_ai() -> MetaAI:
        ai = state.ai or MetaAI()
        if not ai.available:
            abort(503, "AI is not set up: add MODEL_API_KEY from dev.meta.ai")
        return ai

    @flask.post("/ask/<client_id>")
    def ask_endpoint(client_id):
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        question = (body.get("question") or "").strip()
        if not question:
            abort(400, "send JSON with a 'question'")
        ai = need_ai()
        try:
            reply = ask(ai, question, ai_context(client_id), body.get("history"))
        except PermissionError as e:
            abort(503, str(e))
        except RuntimeError as e:
            abort(502, str(e))
        return {"answer": reply.text, "model": reply.model, "cost_usd": reply.cost_usd}

    @flask.post("/campaign/<client_id>")
    def campaign_endpoint(client_id):
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        try:
            budget = float(body.get("daily_budget", 0))
        except (TypeError, ValueError):
            budget = 0
        if budget <= 0:
            abort(400, "send JSON with a positive 'daily_budget'")
        cfg = state.config(client_id)
        own = is_admin(client_id)                       # ProAds advertising itself: the result is a sign-up
        conversion = body.get("conversion") or ("signup" if own else "purchase")
        if own:
            product, landing = PROADS_PRODUCT, public_base() + "/"
        else:
            product = " ".join(x for x in (cfg.get("name", ""), cfg.get("website", "")) if x)
            landing = cfg.get("website", "")
        if landing and not re.match(r"^https?://", landing):
            landing = "https://" + landing
        ai = need_ai()
        budget_currency = ads_currency(client_id)
        try:
            out = build_campaign(ai, ai_context(client_id), budget, body.get("goal", ""), body.get("notes", ""),
                                 conversion=conversion, product=product, landing_url=landing,
                                 budget_currency=budget_currency)
        except PermissionError as e:
            abort(503, str(e))
        except (RuntimeError, CampaignError) as e:
            abort(502, str(e))
        out["can_publish"] = state.ads_settings(client_id, own) is not None
        with state.lock:
            out["draft_id"] = state.drafts.add(client_id, {k: v for k, v in out.items() if k != "can_publish"})
        return out

    @flask.get("/campaign/<client_id>/history")
    def campaign_history(client_id):
        """Every campaign the builder made, newest first, with its status in Meta if it was published."""
        state.require_key(client_id)
        with state.lock:
            rows = state.drafts.list(client_id)
            launches = {l["campaign_id"]: l for l in state.launches.list(client_id)}
        for r in rows:
            l = launches.get(r["campaign_id"]) if r["campaign_id"] else None
            r["status"] = l["status"] if l else ("published" if r["campaign_id"] else "draft")
        return {"history": rows}

    @flask.get("/campaign/<client_id>/history/<draft_id>")
    def campaign_history_item(client_id, draft_id):
        state.require_key(client_id)
        with state.lock:
            d = state.drafts.get(client_id, draft_id)
            launch = state.launches.get(client_id, d["campaign_id"]) if d and d.get("campaign_id") else None
        if not d:
            abort(404, "unknown campaign")
        d["can_publish"] = state.ads_settings(client_id, is_admin(client_id)) is not None
        d["launch"] = launch
        return d

    @flask.post("/campaign/<client_id>/history/<draft_id>/delete")
    def campaign_history_delete(client_id, draft_id):
        state.require_key(client_id)
        with state.lock:
            if not state.drafts.delete(client_id, draft_id):
                abort(404, "unknown campaign")
        return {"deleted": True}

    # ---------------- Competitor X-ray (Premium) ----------------
    def require_premium(client_id):
        if is_admin(client_id):
            return
        with state.lock:
            tier = state.billing.tier(client_id)
        if tier != "premium":
            resp = jsonify({"error": "Competitor X-ray is part of Premium. Upgrade on Plan & billing.",
                            "needs_plan": True, "needs_tier": "premium"})
            resp.status_code = 402
            abort(resp)

    def meta_token(client_id) -> Optional[str]:
        with state.lock:
            c = state.meta_conns.get(client_id)
        if c and c.get("token"):
            try:
                return meta_connect.decrypt(c["token"])
            except Exception:
                log.warning("could not read the Meta connection of %s", client_id)
        if is_admin(client_id):
            return meta_ads.settings()["token"] or None
        return None

    def library_tokens(client_id) -> list[str]:
        """The Ad Library is public data, but Meta only answers people who confirmed their identity.
        ProAds' own (verified) connection is tried first, so clients don't have to verify themselves."""
        out = []
        env_tok = os.environ.get("PROADS_LIBRARY_TOKEN", "").strip()
        if env_tok:
            out.append(env_tok)
        for admin in sorted(_admins(state)):
            t = meta_token(admin)
            if t and t not in out:
                out.append(t)
        own = meta_token(client_id)
        if own and own not in out:
            out.append(own)
        return out

    @flask.post("/competitors/<client_id>/scan")
    def competitors_scan(client_id):
        """A competitor's live ads from Meta's Ad Library, plus what the numbers say."""
        state.require_key(client_id)
        require_premium(client_id)
        body = request.get_json(silent=True) or {}
        if (body.get("pasted") or "").strip():
            ads = competitors.from_pasted(body["pasted"])
        else:
            tokens = library_tokens(client_id)
            if not tokens:
                abort(409, "Connect your Facebook & Instagram first (Connect & upload), or paste the competitor's ads.")
            q = (body.get("query") or "").strip()
            page_ids = re.findall(r"\d{6,}", q) if re.search(r"facebook\.com|^\d{6,}$", q) else []
            country = (body.get("country") or "AE").strip().upper()[:2]
            ads, err = None, None
            for token in tokens:
                try:
                    ads = competitors.scan(state.ads_http, token, meta_ads.settings()["version"], query=q,
                                           page_ids=page_ids or None, country=country)
                    break
                except competitors.ScanError as e:
                    err = e
                    log.info("Ad Library scan failed with one token: %s", e)
            if ads is None:
                abort(502, str(err))
        return {"ads": ads, "summary": competitors.summarize(ads)}

    @flask.post("/competitors/<client_id>/counter")
    def competitors_counter(client_id):
        """An original campaign built to beat the competitor's ads; saved to the campaign history."""
        state.require_key(client_id)
        require_premium(client_id)
        body = request.get_json(silent=True) or {}
        ads = body.get("ads") if isinstance(body.get("ads"), list) else []
        ads = [competitors.normalize_client(a) for a in ads if isinstance(a, dict)][:40]
        if not ads:
            abort(400, "scan a competitor first")
        try:
            budget = float(body.get("daily_budget", 0))
        except (TypeError, ValueError):
            budget = 0
        if budget <= 0:
            abort(400, "send JSON with a positive 'daily_budget'")
        cfg = state.config(client_id)
        own = is_admin(client_id)
        conversion = body.get("conversion") or ("signup" if own else "purchase")
        if own:
            product, landing = PROADS_PRODUCT, public_base() + "/"
        else:
            product = " ".join(x for x in (cfg.get("name", ""), cfg.get("website", "")) if x)
            landing = cfg.get("website", "")
        if landing and not re.match(r"^https?://", landing):
            landing = "https://" + landing
        ai = need_ai()
        try:
            out = competitors.counter_campaign(ai, ads, ai_context(client_id), budget, conversion, product, landing,
                                               ads_currency(client_id), body.get("notes", ""))
        except PermissionError as e:
            abort(503, str(e))
        except (RuntimeError, CampaignError) as e:
            abort(502, str(e))
        out["can_publish"] = state.ads_settings(client_id, own) is not None
        if not out["problems"]:
            with state.lock:
                out["draft_id"] = state.drafts.add(client_id, {k: v for k, v in out.items() if k != "can_publish"})
        return out

    def ads_currency(client_id) -> str:
        """Budgets are in the currency the client's ad account bills in."""
        with state.lock:
            v = meta_connect.public_view(state.meta_conns.get(client_id))
        return (v.get("currency") if v.get("connected") and v.get("currency") else
                state.config(client_id).get("currency", "AED")).upper()

    def needs_payment(client_id) -> bool:
        """Ads are free to test for a few days; after that a plan is needed (organic stays free)."""
        if is_admin(client_id) or not billing.available():
            return False
        with state.lock:
            return not state.billing.ads_access(client_id)["allowed"]

    def publisher(client_id, gate: bool = True):
        if gate and needs_payment(client_id):
            days = billing.ads_trial_days()
            resp = jsonify({"error": f"Your free {days:g}-day ad test has ended. Add your card on Plan & billing "
                                     "to keep running ads. Organic tools stay free.", "needs_plan": True})
            resp.status_code = 402
            abort(resp)
        s = state.ads_settings(client_id, is_admin(client_id))
        if s is None:
            abort(409, "Connect your Facebook & Instagram first (Connect & upload), and pick an ad account, Page and Pixel.")
        return meta_ads.Publisher(state.ads_http, s)

    @flask.post("/campaign/<client_id>/publish")
    def campaign_publish(client_id):
        """Create the campaign in the client's Meta ad account, everything paused."""
        state.require_key(client_id)
        pub = publisher(client_id)
        body = request.get_json(silent=True) or {}
        plan, images = body.get("plan"), body.get("images")
        if not isinstance(plan, dict) or not isinstance(images, dict):
            abort(400, "send JSON with the 'plan' and its 'images'")
        landing = (body.get("landing_url") or (public_base() + "/" if is_admin(client_id)
                                                else state.config(client_id).get("website", ""))).strip()
        if landing and not re.match(r"^https?://", landing):
            landing = "https://" + landing
        landing = landing.replace("http://", "https://", 1) if landing.startswith("http://") else landing
        try:
            launch = pub.publish(plan, images, landing, ads_currency(client_id))
        except meta_ads.PublishError as e:
            abort(502, str(e))
        with state.lock:
            state.launches.add(client_id, launch)
            if body.get("draft_id"):
                state.drafts.link(client_id, str(body["draft_id"]), launch["campaign_id"])
        log.info("%s published campaign %s (paused)", client_id, launch["campaign_id"])
        return launch

    @flask.post("/campaign/<client_id>/launch/<campaign_id>")
    def campaign_launch(client_id, campaign_id):
        """Go live or pause. Going live needs {"confirm": true}: from then on Meta spends the budget."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in ("live", "pause"):
            abort(400, "send {\"action\": \"live\"} or {\"action\": \"pause\"}")
        if action == "live" and body.get("confirm") is not True:
            abort(400, "going live spends money: send \"confirm\": true")
        with state.lock:
            launch = state.launches.get(client_id, campaign_id)
        if not launch:
            abort(404, "unknown campaign")
        status = "ACTIVE" if action == "live" else "PAUSED"
        try:
            publisher(client_id, gate=action == "live").set_status(launch, status)
        except meta_ads.PublishError as e:
            abort(502, str(e))
        with state.lock:
            state.launches.set_status(client_id, campaign_id, status)
            if status == "ACTIVE" and not is_admin(client_id):
                state.billing.start_ads_trial(client_id)
        log.info("%s set campaign %s to %s", client_id, campaign_id, status)
        return {**launch, "status": status}

    @flask.get("/campaign/<client_id>/launches")
    def campaign_launches(client_id):
        state.require_key(client_id)
        with state.lock:
            return {"launches": state.launches.list(client_id), "missing": meta_ads.missing()}

    def page(name: str, referrer: str = "no-referrer"):
        resp = send_from_directory(WEB_DIR, name)
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = referrer
        return resp

    @flask.get("/")
    def landing():
        """Public page: what ProAds does, and free sign-up."""
        return page("landing.html", referrer="strict-origin-when-cross-origin")

    @flask.get("/app")
    def web_app():
        """The ProAds web app: sign-in, audit, upload, tracking links, AI assistant."""
        return page("index.html")

    @flask.get("/privacy")
    def privacy():
        return page("privacy.html", referrer="strict-origin-when-cross-origin")

    @flask.get("/public-config")
    def public_config():
        """Values the public pages need: the Pixel ID (public by nature) and contact email."""
        return {"pixel_id": growth.pixel_id(), "contact": os.environ.get("PROADS_CONTACT_EMAIL", "")}

    def client_ip() -> str:
        return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[-1].strip()

    @flask.post("/auth/signup")
    def signup():
        body = request.get_json(silent=True) or {}
        ip_key = f"signup:{client_ip()}"
        if state.signup_limiter.locked_for(ip_key):
            abort(429, "Too many sign-ups from this connection. Please try again later.")
        clean, errors = validate_signup(body)
        if errors:
            return jsonify({"errors": errors}), 400
        if state.accounts.by_email(clean["email"]):
            return jsonify({"errors": ["An account with this email already exists. Sign in instead."]}), 409
        ctx = growth.capture_context(body, request.headers, request.remote_addr)
        with state.lock:
            cid = state.accounts.create(clean, set(state.clients), json.dumps(ctx) if ctx else "")
            token = state.accounts.new_session(cid)
        state.signup_limiter.fail(ip_key)              # counts sign-ups per connection
        event_id = f"signup:{cid}"
        if ctx:
            growth.send_later(growth.build_event("CompleteRegistration", state.accounts.get(cid), ctx, event_id),
                              state.growth_http, state.growth_sent)
        log.info("new account %s", cid)
        return {"client_id": cid, "token": token, "event_id": event_id, "name": clean["name"]}

    @flask.post("/auth/login")
    def login():
        """Email + password (self-serve accounts) or business ID + access key (server settings)."""
        body = request.get_json(silent=True) or {}
        who = (body.get("login") or "").strip()
        secret = body.get("password") or ""
        lock_key = f"login:{who.lower()}"
        wait = state.login_limiter.locked_for(lock_key)
        if wait:
            abort(429, f"Too many wrong attempts. Try again in {wait // 60 + 1} minutes.")
        cid = None
        if "@" in who:
            acct = state.accounts.by_email(who)
            if acct and check_password(secret, acct["pw_hash"]):
                cid = acct["client_id"]
            elif not acct:
                check_password(secret, _DUMMY_HASH)     # same timing whether or not the email exists
        else:
            cfg = state.clients.get(who)
            if cfg and cfg.get("api_key") and hmac.compare_digest(cfg["api_key"], secret):
                cid = who
        if not cid:
            state.login_limiter.fail(lock_key)
            abort(401, "Wrong email/ID or password")
        state.login_limiter.clear(lock_key)
        with state.lock:
            token = state.accounts.new_session(cid)
        return {"client_id": cid, "token": token}

    @flask.get("/data-deletion")
    def data_deletion():
        return page("data-deletion.html", referrer="strict-origin-when-cross-origin")

    @flask.post("/account/delete/<client_id>")
    def delete_account(client_id):
        """Self-serve: delete the account and all its data. Needs the session and the password again."""
        state.require_key(client_id)
        acct = state.accounts.get(client_id)
        if not acct:
            abort(400, "This business is managed in the server settings; delete it there")
        body = request.get_json(silent=True) or {}
        lock_key = f"login:{acct['email']}"
        if state.login_limiter.locked_for(lock_key):
            abort(429, "Too many wrong attempts. Try again later.")
        if not check_password(body.get("password") or "", acct["pw_hash"]):
            state.login_limiter.fail(lock_key)
            abort(401, "Wrong password")
        with state.lock:
            counts = state.accounts.delete_everything(client_id)
            state.learnings.forget(client_id)
            state.learnings.forget(client_id + ":archived")
            for cache in (state.engines, state.posts, state.trained_at):
                cache.pop(client_id, None)
        log.info("account %s deleted by its owner: %s", client_id, counts)
        return {"deleted": True}

    # ---------------- password reset ----------------
    def public_base() -> str:
        configured = os.environ.get("PROADS_PUBLIC_URL", "").strip().rstrip("/")
        if configured:
            return configured
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        return f"{proto}://{request.host}"

    def reset_link(token: str) -> str:
        return f"{public_base()}/app#/reset?token={token}"     # after '#', so it never reaches server logs

    @flask.post("/auth/forgot")
    def forgot_password():
        """Same answer whether or not the email has an account, so nobody can probe emails."""
        email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
        if not mailer.available():
            contact = os.environ.get("PROADS_CONTACT_EMAIL", "")
            return {"sent": False, "message": "Password reset by email isn't switched on yet. "
                    + (f"Email {contact} and we'll help you." if contact else "Please contact us and we'll help you.")}
        key = f"forgot:{email}"
        if email and not state.signup_limiter.locked_for(key):
            state.signup_limiter.fail(key)              # at most 10 reset emails per address per hour
            acct = state.accounts.by_email(email)
            if acct:
                with state.lock:
                    token = state.accounts.new_reset(acct["client_id"])
                link = reset_link(token)
                threading.Thread(target=mailer.send, daemon=True, args=(acct["email"], "Reset your ProAds password",
                    f"Hi {acct['name']},\n\nSomeone asked to reset the password for your ProAds account. "
                    f"If it was you, open this link within one hour:\n\n{link}\n\n"
                    "If it wasn't you, ignore this email. Your password stays the same.\n\nProAds")).start()
        return {"sent": True, "message": "If that email has a ProAds account, a reset link is on its way. "
                                         "It works for one hour."}

    @flask.post("/auth/reset")
    def reset_password():
        body = request.get_json(silent=True) or {}
        password = body.get("password") or ""
        if len(password) < 10:
            abort(400, "Use a password of at least 10 characters")
        with state.lock:
            cid = state.accounts.use_reset(body.get("token") or "")
            if not cid or not state.accounts.get(cid):
                abort(400, "This reset link has expired or was already used. Ask for a new one.")
            state.accounts.set_password(cid, password)
            token = state.accounts.new_session(cid)
        acct = state.accounts.get(cid)
        state.login_limiter.clear(f"login:{acct['email']}")
        log.info("password reset for %s", cid)
        return {"client_id": cid, "token": token}

    # ---------------- admin ----------------
    def is_admin(client_id: str) -> bool:
        """Only businesses from the server settings can be admins: set "admin": true there,
        or list their IDs in PROADS_ADMINS (default: pro)."""
        cfg = state.clients.get(client_id)
        if cfg is None:
            return False
        admins = {x.strip() for x in os.environ.get("PROADS_ADMINS", "pro").split(",") if x.strip()}
        return bool(cfg.get("admin")) or client_id in admins

    def require_admin(admin_id: str) -> None:
        state.require_key(admin_id)
        if not is_admin(admin_id):
            abort(403, "admins only")

    @flask.get("/admin/<admin_id>/accounts")
    def admin_accounts(admin_id):
        require_admin(admin_id)
        with state.lock:
            rows = state.accounts.overview()
        week_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 7 * 86400))
        return {"accounts": rows, "email_on": mailer.available(), "totals": {
            "accounts": len(rows), "activated": sum(1 for r in rows if r["activated_at"]),
            "new_this_week": sum(1 for r in rows if r["created_at"] >= week_ago),
            "with_shopify": sum(1 for r in rows if r["shopify"]), "with_whatsapp": sum(1 for r in rows if r["wa_chats"]),
            "with_facebook": sum(1 for r in rows if r["facebook"]),
            "paying": sum(1 for r in rows if r["plan"] in billing.ACTIVE),
            "live_campaigns": sum(r["live_campaigns"] for r in rows),
            "lessons_from": state.learnings.count()}}

    @flask.get("/admin/<admin_id>/setup")
    def admin_setup(admin_id):
        """What's switched on, read from the server settings. Never returns secret values."""
        require_admin(admin_id)
        env = os.environ.get
        g = growth.settings()
        wa = whatsapp.settings()
        key = str(state.clients.get(admin_id, {}).get("api_key", ""))
        db = env("PAYSIGNAL_DB", "paysignal.db")
        items = [
            ("Your password is strong", len(key) >= 16 and not key.isalpha(),
             "Use 16+ characters with numbers: edit api_key in PAYSIGNAL_CLIENTS."),
            ("Data saved on the permanent disk", db.startswith("/var/data/"),
             "Set PAYSIGNAL_DB to /var/data/paysignal.db, or data is lost on every deploy."),
            ("Website address set", bool(env("PROADS_PUBLIC_URL")),
             "Set PROADS_PUBLIC_URL to https://pro.ezdevsoft.net."),
            ("Contact email on privacy pages", bool(env("PROADS_CONTACT_EMAIL")),
             "Set PROADS_CONTACT_EMAIL, e.g. admin@ezdevsoft.net."),
            ("Password reset emails", mailer.available(),
             "Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM (Zoho)."),
            ("Tracking for your own ads", bool(g["dataset_id"] and g["token"]),
             "Set PROADS_META_DATASET_ID and PROADS_META_TOKEN from Meta Events Manager."),
            ("Meta AI (assistant, campaigns, summaries)", (state.ai or MetaAI()).available,
             "Set MODEL_API_KEY from dev.meta.ai (billing must be active)."),
            ("WhatsApp chats from ads", bool(wa["app_secret"] and wa["verify_token"]),
             "Set META_APP_SECRET and PROADS_WHATSAPP_VERIFY_TOKEN, then the webhook in Meta."),
            ("Your own ads can be published", state.ads_settings(admin_id, True) is not None,
             "Connect your own Facebook in Connect & upload (easiest), or set PROADS_AD_ACCOUNT_ID, "
             "PROADS_PAGE_ID and PROADS_ADS_TOKEN."),
            ("Clients can connect Facebook & Instagram", meta_connect.available(),
             "Set META_APP_ID, META_APP_SECRET, META_LOGIN_CONFIG_ID and PROADS_SECRET_KEY (Facebook Login for Business)."),
            ("Paid plans (Stripe)", billing.available(),
             "Set PROADS_STRIPE_SECRET_KEY, PROADS_STRIPE_PRICE_ID and PROADS_STRIPE_WEBHOOK_SECRET (+ PROADS_SPEND_FEE_PERCENT)."),
            ("Connect Shopify button", shopify_app.available(),
             "Optional: SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, PROADS_SECRET_KEY."),
        ]
        if g["dataset_id"] and g["token"]:
            items.insert(6, ("Ad tracking out of test mode", not g["test_code"],
                             "Remove PROADS_META_TEST_CODE once events show in Test events."))
        return {"items": [{"label": l, "done": bool(d), "how": h} for l, d, h in items]}

    @flask.post("/admin/<admin_id>/companies")
    def admin_add_company(admin_id):
        """Add a client company by hand (e.g. one you already work with). Optionally move the sales,
        posts and tracking codes sitting in your own account into it. The owner gets a link to set a password."""
        require_admin(admin_id)
        b = request.get_json(silent=True) or {}
        clean, errors = validate_signup({**b, "password": secrets.token_urlsafe(18), "terms": True})
        if errors:
            abort(400, "; ".join(errors))
        if state.accounts.by_email(clean["email"]):
            abort(409, "An account with that email already exists")
        moved = {"sales": 0, "posts": 0, "codes": 0}
        with state.lock:
            cid = state.accounts.create(clean, reserved=set(state.clients))
            if b.get("move_data"):
                db = state.store.db
                rows = db.execute("SELECT source, external_id, kind, amount, currency, occurred_at, customer_key, refund_of,"
                                  " data, sent_to_meta FROM payments WHERE client_id=?", (admin_id,)).fetchall()
                for r in rows:
                    d = json.loads(r[8]); d["client_id"] = cid
                    db.execute("INSERT OR IGNORE INTO payments (client_id, source, external_id, kind, amount, currency,"
                               " occurred_at, customer_key, refund_of, data, sent_to_meta) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (cid, *r[:8], json.dumps(d), r[9]))
                db.execute("DELETE FROM payments WHERE client_id=?", (admin_id,))
                moved["sales"] = len(rows)
                moved["posts"] = db.execute("UPDATE posts SET client_id=? WHERE client_id=?", (cid, admin_id)).rowcount
                moved["codes"] = db.execute("UPDATE promo_codes SET client_id=? WHERE client_id=?", (cid, admin_id)).rowcount
                db.commit()
                for cache in (state.engines, state.posts, state.trained_at):
                    cache.pop(admin_id, None)
                if moved["sales"] or moved["posts"]:
                    state.accounts.mark_activated(cid)
            token = state.accounts.new_reset(cid)
        log.info("admin %s added company %s (moved %s)", admin_id, cid, moved)
        return {"client_id": cid, "moved": moved, "link": reset_link(token), "expires_in_minutes": 60}

    @flask.post("/admin/<admin_id>/reset-link/<client_id>")
    def admin_reset_link(admin_id, client_id):
        """A one-hour reset link the admin can send by WhatsApp when email isn't set up."""
        require_admin(admin_id)
        if not state.accounts.get(client_id):
            abort(404, "unknown account")
        with state.lock:
            token = state.accounts.new_reset(client_id)
        log.info("admin %s made a reset link for %s", admin_id, client_id)
        return {"link": reset_link(token), "expires_in_minutes": 60}

    @flask.post("/admin/<admin_id>/delete/<client_id>")
    def admin_delete(admin_id, client_id):
        require_admin(admin_id)
        if (request.get_json(silent=True) or {}).get("confirm") != client_id:
            abort(400, "send {\"confirm\": \"<account id>\"} to delete")
        if not state.accounts.get(client_id):
            abort(404, "unknown account")
        with state.lock:
            state.accounts.delete_everything(client_id)
            state.learnings.forget(client_id)
            state.learnings.forget(client_id + ":archived")
            for cache in (state.engines, state.posts, state.trained_at):
                cache.pop(client_id, None)
        log.info("admin %s deleted account %s", admin_id, client_id)
        return {"deleted": True}

    @flask.post("/auth/logout")
    def logout():
        token = request.headers.get("X-API-Key", "")
        if token:
            with state.lock:
                state.accounts.end_session(token)
        return {"ok": True}

    @flask.get("/status/<client_id>")
    def status(client_id):
        """What is connected for this business; also used to check a sign-in."""
        state.require_key(client_id)
        cfg = state.config(client_id)
        meta = cfg.get("meta", {})
        ai = state.ai or MetaAI()
        with state.lock:
            payments = state.store.last_payment_by_source(client_id)
            totals = state.store.totals(client_id)
            posts = len(state.posts_for(client_id))
            codes = len(state.store.promos(client_id))
            shop = state.store.shop_for(client_id)
        shop_info = {"available": shopify_app.available(), "connected": bool(shop)}
        if shop:
            shop_info.update({k: shop[k] for k in ("shop", "connected_at", "sync_status", "sync_message",
                                                   "last_sync", "imported")})
            shop_info["all_orders"] = "read_all_orders" in (shop["scope"] or "")
        return {
            "client_id": client_id,
            "name": cfg.get("name", client_id),
            "self_serve": bool(cfg.get("self_serve")),
            "admin": is_admin(client_id),
            "currency": cfg.get("currency", "AED"),
            "website": cfg.get("website", ""),
            "connections": {
                "shopify": {"configured": bool(cfg.get("shopify_webhook_secret") or shop),
                            **payments.get("shopify", {})},
                "shopify_app": shop_info,
                "stripe": {"configured": bool(cfg.get("stripe_webhook_secret")), **payments.get("stripe", {})},
                "uploads": payments.get("csv", {}),
                "meta_capi": {"configured": bool(meta.get("dataset_id") and meta.get("access_token")),
                              "live": bool(meta.get("live"))},
                "ai": {"configured": ai.available},
                "ads_publish": {"configured": state.ads_settings(client_id, is_admin(client_id)) is not None},
                "meta_login": {k: v for k, v in meta_connect.public_view(state.meta_conns.get(client_id)).items()
                               if k not in ("ad_accounts", "pages")},
            },
            "totals": totals, "posts": posts, "tracking_codes": codes,
            "webhooks": {"stripe": f"/webhooks/stripe/{client_id}", "shopify": f"/webhooks/shopify/{client_id}"},
        }

    # ---------------- ProAds subscriptions (Stripe Billing) ----------------
    def stripe_api():
        if not billing.available():
            abort(503, "Paid plans aren't switched on yet.")
        return billing.Stripe(state.stripe_http)

    @flask.get("/billing/<client_id>")
    def billing_status(client_id):
        state.require_key(client_id)
        with state.lock:
            b = state.billing.get(client_id) or {}
        out = {"available": billing.available(), "status": b.get("status"), "active": b.get("status") in billing.ACTIVE,
               "period_end": b.get("period_end"), "cancel_at_period_end": bool(b.get("cancel_at_period_end")),
               "has_customer": bool(b.get("customer_id")), "trial_days": billing.settings()["trial_days"],
               "ads": state.billing.ads_access(client_id), "price": None,
               "fee_percent": billing.fee_percent(), "fees": state.billing.fees(client_id), "spend_so_far": None}
        if b.get("status") in billing.ACTIVE and b.get("period_start") and billing.fee_percent() > 0:
            try:
                spend, cur = spend_between(client_id, int(b["period_start"]), int(time.time()) + 86400)
                out["spend_so_far"] = {"spend": spend, "currency": cur,
                                       "fee": billing.fee_amount(spend, billing.fee_percent()) / 100, "fee_currency": cur}
            except Exception as e:
                log.info("could not read ad spend for %s: %s", client_id, e)
        with state.lock:
            out["tier"] = "premium" if is_admin(client_id) else state.billing.tier(client_id)
        out["premium_available"] = billing.premium_available()
        out["premium_price"] = None
        if out["available"]:
            try:
                st = billing.Stripe(state.stripe_http)
                out["price"] = billing.price_info(st)
                if out["premium_available"]:
                    out["premium_price"] = billing.price_info(st, billing.settings()["premium_price"])
            except billing.BillingError as e:
                log.warning("could not read the Stripe price: %s", e)
        return out

    @flask.post("/billing/<client_id>/checkout")
    def billing_checkout(client_id):
        """Stripe's hosted payment page for the monthly plan. The browser goes there next."""
        state.require_key(client_id)
        stripe = stripe_api()
        tier = "premium" if (request.get_json(silent=True) or {}).get("tier") == "premium" else "pro"
        if tier == "premium" and not billing.premium_available():
            abort(503, "Premium isn't switched on yet.")
        with state.lock:
            b = state.billing.get(client_id) or {}
        if b.get("status") in billing.ACTIVE:
            abort(409, "You already have an active plan. Use the switch button to change it.")
        acct = state.accounts.get(client_id) or {}
        try:
            url = billing.checkout_session(stripe, client_id, acct.get("email", ""), b.get("customer_id"),
                                           public_base(), first_time=not b.get("subscription_id"), tier=tier)
        except billing.BillingError as e:
            abort(502, f"Stripe: {e}")
        return {"url": url}

    @flask.post("/billing/<client_id>/switch")
    def billing_switch(client_id):
        """Move an active plan between Pro and Premium (Stripe charges or credits the difference)."""
        state.require_key(client_id)
        stripe = stripe_api()
        tier = (request.get_json(silent=True) or {}).get("tier")
        if tier not in ("pro", "premium"):
            abort(400, "send JSON with 'tier': 'pro' or 'premium'")
        if tier == "premium" and not billing.premium_available():
            abort(503, "Premium isn't switched on yet.")
        with state.lock:
            b = state.billing.get(client_id) or {}
        if b.get("status") not in billing.ACTIVE or not b.get("subscription_id"):
            abort(409, "No active plan yet. Choose a plan first.")
        try:
            billing.change_tier(stripe, b["subscription_id"], tier)
        except billing.BillingError as e:
            abort(502, f"Stripe: {e}")
        with state.lock:
            state.billing.upsert(client_id, tier=tier)
        return {"tier": tier}

    @flask.post("/billing/<client_id>/portal")
    def billing_portal(client_id):
        """Stripe's page to change card, download invoices or cancel."""
        state.require_key(client_id)
        stripe = stripe_api()
        with state.lock:
            b = state.billing.get(client_id) or {}
        if not b.get("customer_id"):
            abort(400, "No billing account yet. Upgrade first.")
        try:
            return {"url": billing.portal_session(stripe, b["customer_id"], public_base() + "/app#/billing")}
        except billing.BillingError as e:
            abort(502, f"Stripe: {e}")

    @flask.post("/webhooks/proads-billing")
    def billing_webhook():
        secret = billing.settings()["webhook_secret"]
        payload = request.get_data()
        if not secret or not stripe_signature_ok(payload, request.headers.get("Stripe-Signature", ""), secret):
            abort(400, "bad signature")
        try:
            event = json.loads(payload)
        except ValueError:
            abort(400, "bad JSON")
        with state.lock:
            cid = state.billing.handle(event, state.known)
        if cid:
            log.info("billing %s for %s", event.get("type"), cid)
            if state.billing_sync:
                enforce_ads_trials()
            else:
                threading.Thread(target=enforce_ads_trials, daemon=True, name="ads-access").start()
        inv = (event.get("data") or {}).get("object") or {}
        if event.get("type") == "invoice.created" and inv.get("status") == "draft" \
                and inv.get("billing_reason") == "subscription_cycle":
            if state.billing_sync:
                charge_spend_fee(inv)
            else:
                threading.Thread(target=charge_spend_fee, args=(inv,), daemon=True, name="spend-fee").start()
        return {"received": True}

    def enforce_ads_trials() -> list[str]:
        """Pause live campaigns of clients whose free test ended without a plan; resume them once they pay."""
        changed = []
        if not billing.available():
            return changed
        with state.lock:
            clients = {r[0] for r in state.store.db.execute(
                "SELECT DISTINCT client_id FROM ad_launches WHERE status IN ('ACTIVE','PAUSED_BILLING')").fetchall()}
        for cid in sorted(clients):
            if is_admin(cid) or not state.known(cid):
                continue
            with state.lock:
                allowed = state.billing.ads_access(cid)["allowed"]
                launches = state.launches.list(cid)
            want_from, want_to = ("PAUSED_BILLING", "ACTIVE") if allowed else ("ACTIVE", "PAUSED_BILLING")
            for l in launches:
                if l["status"] != want_from:
                    continue
                try:
                    publisher(cid, gate=False).set_status(l, "ACTIVE" if want_to == "ACTIVE" else "PAUSED")
                except Exception as e:
                    log.warning("could not %s campaign %s of %s: %s", "resume" if allowed else "pause",
                                l["campaign_id"], cid, e)
                    continue
                with state.lock:
                    state.launches.set_status(cid, l["campaign_id"], want_to)
                changed.append(f"{cid}:{l['campaign_id']}:{want_to}")
                log.info("%s campaign %s of %s", "resumed" if allowed else "paused (test ended)", l["campaign_id"], cid)
        return changed
    state.enforce_ads_trials = enforce_ads_trials

    def spend_between(client_id: str, start: int, end: int) -> tuple[float, str]:
        """Ad spend of the campaigns this client published through ProAds."""
        with state.lock:
            ids = [l["campaign_id"] for l in state.launches.list(client_id)]
        s = state.ads_settings(client_id, is_admin(client_id))
        if not ids or not s:
            return 0.0, ""
        return meta_ads.Publisher(state.ads_http, s).spend(ids, start, end)

    def retry_held_fees() -> None:
        """Invoices held because Meta's spend couldn't be read: try again; after a day, send them without the fee."""
        with state.lock:
            held = state.billing.held_fees()
        for h_ in held:
            stripe = billing.Stripe(state.stripe_http)
            give_up = time.time() - h_["held_at"] > 86400
            try:
                if not give_up:
                    with state.lock:
                        state.billing.fee_retry(h_["invoice_id"])
                    spend, currency = spend_between(h_["client_id"], int(h_["invoice"]["period_start"]), int(h_["invoice"]["period_end"]))
                    added = billing.add_spend_fee(stripe, h_["invoice"], spend, currency, billing.fee_percent())
                    with state.lock:
                        state.billing.log_fee(h_["invoice_id"], h_["client_id"], spend, currency,
                                              added["amount"] if added else 0.0, (h_["invoice"].get("currency") or "").upper())
                else:
                    log.error("ad spend fee for %s on %s given up after a day: invoice sent without it",
                              h_["client_id"], h_["invoice_id"])
                stripe.post(f"invoices/{h_['invoice_id']}", {"auto_advance": "true"})
                with state.lock:
                    state.billing.release_fee(h_["invoice_id"])
            except Exception as e:
                log.warning("held invoice %s still waiting: %s", h_["invoice_id"], e)
    state.retry_held_fees = retry_held_fees

    def charge_spend_fee(inv: dict) -> None:
        """Renewal invoice: add the % of last period's ad spend before Stripe finalizes it (about 1 hour)."""
        percent = billing.fee_percent()
        with state.lock:
            cid = state.billing.by_customer(inv.get("customer") or "")
            done = state.billing.fee_logged(inv.get("id", ""))
        if not cid or done or percent <= 0:
            return
        stripe = billing.Stripe(state.stripe_http)
        try:
            spend, currency = spend_between(cid, int(inv["period_start"]), int(inv["period_end"]))
            added = billing.add_spend_fee(stripe, inv, spend, currency, percent)
        except Exception as e:
            # never finalize without the fee by accident: keep the invoice as a draft and retry
            log.warning("ad spend fee for %s on %s failed, holding the invoice: %s", cid, inv.get("id"), e)
            try:
                stripe.post(f"invoices/{inv['id']}", {"auto_advance": "false"})
                with state.lock:
                    state.billing.hold_fee(inv, cid)
            except Exception as e2:
                log.error("could not hold invoice %s: %s", inv.get("id"), e2)
            return
        if spend and not added:
            log.warning("ad spend fee skipped for %s: spend in %s, invoice in %s", cid, currency, inv.get("currency"))
        with state.lock:
            state.billing.log_fee(inv["id"], cid, spend, currency, added["amount"] if added else 0.0,
                                  (inv.get("currency") or "").upper())
        log.info("ad spend fee for %s: %s", cid, added)

    # ---------------- Facebook & Instagram connection (each client's own) ----------------
    def meta_back(result, message=""):
        q = urlencode({"meta": result, **({"message": message} if message else {})})
        return redirect("/app#/upload?" + q)

    @flask.post("/meta/connect/<client_id>")
    def meta_connect_start(client_id):
        """Returns Facebook's approval page. The browser goes there next."""
        state.require_key(client_id)
        if not meta_connect.available():
            abort(503, "Connecting Facebook isn't switched on yet.")
        nonce = secrets.token_urlsafe(24)
        with state.lock:
            state.store.put_oauth_state(nonce, client_id, "meta", time.time())
        return {"url": meta_connect.authorize_url(nonce, public_url() + "/meta/callback")}

    @flask.get("/meta/callback")
    def meta_callback():
        if request.args.get("error"):
            return meta_back("error", "Facebook connection was cancelled.")
        with state.lock:
            found = state.store.pop_oauth_state(request.args.get("state", ""))
        if not found or found[1] != "meta" or time.time() - found[2] > meta_connect.STATE_TTL:
            return meta_back("error", "This connect link expired. Please start again.")
        client_id = found[0]
        if not state.known(client_id) or not meta_connect.available():
            return meta_back("error", "Unknown ProAds account.")
        try:
            token = meta_connect.exchange_code(state.meta_http, request.args.get("code", ""), public_url() + "/meta/callback")
            assets = meta_connect.discover(state.meta_http, token["access_token"])
        except Exception as e:
            log.warning("meta connect failed for %s: %s", client_id, e)
            return meta_back("error", "Facebook did not finish the connection. Please try again.")
        if not assets["ad_accounts"]:
            return meta_back("error", "No ad account was shared. Connect again and tick your ad account.")
        with state.lock:
            state.meta_conns.save(client_id, token, assets)
        log.info("%s connected Facebook (%d ad accounts, %d pages)", client_id,
                 len(assets["ad_accounts"]), len(assets["pages"]))
        return meta_back("connected")

    @flask.get("/meta/<client_id>")
    def meta_connection(client_id):
        state.require_key(client_id)
        with state.lock:
            return meta_connect.public_view(state.meta_conns.get(client_id))

    @flask.get("/meta/<client_id>/pixels")
    def meta_pixels(client_id):
        state.require_key(client_id)
        acct = request.args.get("ad_account", "")
        with state.lock:
            c = state.meta_conns.get(client_id)
        if not c or acct not in {a["id"] for a in c["assets"]["ad_accounts"]}:
            abort(400, "pick one of your connected ad accounts")
        try:
            return {"pixels": meta_connect.pixels(state.meta_http, meta_connect.decrypt(c["token"]), acct,
                                                  c["assets"].get("businesses") or [])}
        except meta_connect.ConnectError as e:
            abort(502, f"Meta: {e}")

    @flask.post("/meta/<client_id>/select")
    def meta_select(client_id):
        state.require_key(client_id)
        b = request.get_json(silent=True) or {}
        page_id = re.sub(r"[^\w]", "", str(b.get("page_id") or ""))
        with state.lock:
            c = state.meta_conns.get(client_id)
        if c and page_id and page_id not in {p["id"] for p in c["assets"]["pages"]}:
            try:                                     # a Page typed by ID: check it's really theirs
                page = meta_connect.lookup_page(state.meta_http, meta_connect.decrypt(c["token"]), page_id)
            except meta_connect.ConnectError:
                abort(400, "ProAds can't reach that Page. Make sure you're an admin of it, then Disconnect and "
                           "Connect with Facebook again and tick the Page.")
            with state.lock:
                state.meta_conns.add_page(client_id, page)
        b["page_id"] = page_id
        b["pixel_id"] = re.sub(r"[^\w]", "", str(b.get("pixel_id") or ""))
        try:
            with state.lock:
                state.meta_conns.select(client_id, b.get("ad_account") or "", b.get("page_id") or "",
                                        b.get("pixel_id") or "", b.get("send_sales", True) is not False)
        except meta_connect.ConnectError as e:
            abort(400, str(e))
        with state.lock:
            return meta_connect.public_view(state.meta_conns.get(client_id))

    @flask.post("/meta/<client_id>/disconnect")
    def meta_disconnect(client_id):
        """Forget the access and ask Facebook to remove ProAds' permission."""
        state.require_key(client_id)
        with state.lock:
            token = state.meta_conns.token(client_id)
            state.meta_conns.delete(client_id)
        if token:
            def revoke():
                try:
                    http = state.meta_http
                    if http is None:
                        import requests as http
                    http.delete(f"https://graph.facebook.com/{meta_connect.settings()['version']}/me/permissions",
                                params={"access_token": token}, timeout=20)
                except Exception as e:
                    log.warning("could not revoke Facebook permission for %s: %s", client_id, e)
            threading.Thread(target=revoke, daemon=True).start()
        return {"connected": False}

    # ---------------- Shopify app connection ----------------
    def public_url() -> str:
        configured = shopify_app.settings()["public_url"]
        if configured:
            return configured
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        return f"{proto}://{request.host}"

    @flask.post("/shopify/connect/<client_id>")
    def shopify_connect(client_id):
        """Returns Shopify's approval page for this store. The browser goes there next."""
        state.require_key(client_id)
        if not shopify_app.available():
            abort(503, "Shopify connection is not set up on this server yet "
                       "(needs SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET and PROADS_SECRET_KEY)")
        shop = shopify_app.normalize_shop((request.get_json(silent=True) or {}).get("shop", ""))
        if not shop:
            abort(400, "Enter your Shopify store address, like your-store.myshopify.com. If you use your own domain, "
                       "find the .myshopify.com address in Shopify: Settings > Domains.")
        owner = state.store.client_for_shop(shop)
        if owner and owner != client_id:
            abort(400, "This store is already connected to another ProAds account")
        nonce = secrets.token_urlsafe(24)
        with state.lock:
            state.store.put_oauth_state(nonce, client_id, shop, time.time())
        return {"url": shopify_app.authorize_url(shop, nonce, public_url() + "/shopify/callback")}

    @flask.get("/shopify/callback")
    def shopify_callback():
        args = {k: v for k, v in request.args.items()}
        def back(result, message=""):
            q = urlencode({"shopify": result, **({"message": message} if message else {})})
            return redirect("/app#/upload?" + q)
        s = shopify_app.settings()
        if not shopify_app.available() or not shopify_app.verify_callback(args, s["client_secret"]):
            log.warning("shopify callback rejected: bad signature")
            return back("error", "Shopify's reply could not be verified. Please try again.")
        with state.lock:
            found = state.store.pop_oauth_state(args.get("state", ""))
        shop = shopify_app.normalize_shop(args.get("shop", ""))
        if not found or time.time() - found[2] > shopify_app.STATE_TTL or shop != found[1]:
            return back("error", "This connect link expired or doesn't match. Please start again.")
        client_id = found[0]
        if not state.known(client_id):
            return back("error", "Unknown ProAds account.")
        try:
            tokens = shopify_app.exchange_code(state.shopify_http(), shop, args.get("code", ""))
        except Exception as e:
            log.warning("shopify token exchange failed for %s: %s", shop, e)
            return back("error", "Shopify did not finish the connection. Please try again.")
        if "orders" not in tokens["scope"]:
            return back("error", "Order access was not granted, so ProAds can't see sales.")
        with state.lock:
            state.store.save_shop(client_id, shop, shopify_app.encrypt(json.dumps(tokens)), tokens["scope"])
        state.start_shopify_import(client_id, register_webhooks=public_url() + "/webhooks/shopify-app")
        return back("connected")

    @flask.post("/shopify/sync/<client_id>")
    def shopify_sync(client_id):
        state.require_key(client_id)
        if not state.store.shop_for(client_id):
            abort(400, "No Shopify store is connected")
        state.start_shopify_import(client_id)
        return {"started": True}

    @flask.post("/shopify/disconnect/<client_id>")
    def shopify_disconnect(client_id):
        """Forget the store's access. Orders already imported stay (delete is a separate request)."""
        state.require_key(client_id)
        with state.lock:
            state.store.delete_shop(client_id)
        return {"disconnected": True}

    @flask.post("/webhooks/shopify-app")
    def shopify_app_webhook():
        """Webhooks for stores connected through the app, signed with the app's secret."""
        body = request.get_data()
        s = shopify_app.settings()
        if not s["client_secret"] or not shopify_app.verify_webhook(
                body, request.headers.get("X-Shopify-Hmac-Sha256", ""), s["client_secret"]):
            abort(401, "bad signature")
        shop = shopify_app.normalize_shop(request.headers.get("X-Shopify-Shop-Domain", ""))
        client_id = state.store.client_for_shop(shop) if shop else None
        if not client_id or not state.known(client_id):
            return {"ignored": "store not connected"}
        topic = request.headers.get("X-Shopify-Topic", "")
        if topic == "app/uninstalled":
            with state.lock:
                state.store.delete_shop(client_id)
            return {"disconnected": True}
        try:
            payload = json.loads(body)
        except ValueError:
            abort(400, "invalid json")
        cfg = state.config(client_id)
        return state.ingest(client_id, parse_shopify_webhook(topic, payload, client_id, cfg.get("costs", {})))

    @flask.post("/webhooks/shopify-compliance")
    def shopify_compliance():
        """Shopify's mandatory privacy webhooks."""
        body = request.get_data()
        s = shopify_app.settings()
        if not s["client_secret"] or not shopify_app.verify_webhook(
                body, request.headers.get("X-Shopify-Hmac-Sha256", ""), s["client_secret"]):
            abort(401, "bad signature")
        topic = request.headers.get("X-Shopify-Topic", "")
        try:
            payload = json.loads(body)
        except ValueError:
            abort(400, "invalid json")
        shop = shopify_app.normalize_shop(payload.get("shop_domain", "") or request.headers.get("X-Shopify-Shop-Domain", ""))
        client_id = state.store.client_for_shop(shop) if shop else None
        if topic == "customers/data_request":
            log.warning("PRIVACY: customer data request from %s (order ids %s). Reply to the merchant "
                        "with that customer's stored orders within 30 days.", shop, payload.get("orders_requested"))
            return {"received": True}
        if not client_id:
            return {"received": True, "note": "no data stored for this store"}
        with state.lock:
            if topic == "customers/redact":
                n = state.store.delete_payments(client_id, "shopify", shopify_app.compliance_customer_keys(payload))
            elif topic == "shop/redact":
                n = state.store.delete_payments(client_id, "shopify")
                state.store.delete_shop(client_id)
            else:
                return {"received": True}
            state.trained_at.pop(client_id, None)
        log.info("privacy %s for %s: %d records deleted", topic, shop, n)
        return {"received": True, "deleted": n}

    # ---------------- WhatsApp ----------------
    @flask.get("/webhooks/whatsapp")
    def whatsapp_verify():
        """Meta's one-time check when you save the webhook in the App Dashboard."""
        token = whatsapp.settings()["verify_token"]
        if (request.args.get("hub.mode") == "subscribe" and token
                and hmac.compare_digest(request.args.get("hub.verify_token", ""), token)):
            return request.args.get("hub.challenge", ""), 200, {"Content-Type": "text/plain"}
        abort(403, "verify token does not match PROADS_WHATSAPP_VERIFY_TOKEN")

    @flask.post("/webhooks/whatsapp")
    def whatsapp_webhook():
        body = request.get_data()
        if not whatsapp.verify_signature(body, request.headers.get("X-Hub-Signature-256", ""),
                                         whatsapp.settings()["app_secret"]):
            log.warning("rejected WhatsApp webhook: bad signature")
            abort(401, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            abort(400, "invalid json")
        seen = 0
        for msg in whatsapp.parse_webhook(payload):
            cid = state.waba_client(msg["waba_id"])
            if not cid or not msg["phone_hash"]:
                continue
            with state.lock:
                state.store.record_chat(cid, msg["phone_hash"], msg["at"], msg["referral"])
            seen += 1
        return {"received": seen}                  # always 200 for genuine events, so Meta doesn't retry

    @flask.get("/whatsapp/<client_id>")
    def whatsapp_report(client_id):
        """Ads -> WhatsApp chats -> buyers -> real value."""
        state.require_key(client_id)
        cfg = (state.config(client_id).get("whatsapp") or {})
        with state.lock:
            state.ensure_trained(client_id)
            eng = state.engine(client_id)
            sources = state.store.chats_by_source(client_id)
            recent = state.store.wa_conversions(client_id)
            sales: dict[str, dict] = {}
            for p in state.store.purchases(client_id):
                if (p.utm_content or "").startswith(("wa-ad:", "wa-post:")):
                    sid = p.utm_content.split(":", 1)[1]
                    refunded = state.store.refunded_amount(client_id, p.external_id)
                    keep = 1 - min(refunded / p.amount, 1.0) if p.amount else 0
                    s = sales.setdefault(sid, {"orders": 0, "buyers": set(), "revenue": 0.0, "real_value": 0.0})
                    s["orders"] += 1
                    s["buyers"].add(hash_phone(p.phone))
                    s["revenue"] += p.amount
                    s["real_value"] += eng.scorer.margin(p) * keep
        rows = []
        for src in sources:
            s = sales.get(src["source_id"], {})
            buyers = len(s.get("buyers", ()))
            rows.append({**src, "buyers": buyers, "orders": s.get("orders", 0),
                         "revenue": round(s.get("revenue", 0.0), 2), "real_value": round(s.get("real_value", 0.0), 2),
                         "chat_to_buyer": round(buyers / src["chats"], 3) if src["chats"] else 0.0})
        rows.sort(key=lambda r: (r["source_id"] == "", -r["real_value"], -r["chats"]))
        return {
            "setup": {"webhook_url": "/webhooks/whatsapp", "verify_token_set": bool(whatsapp.settings()["verify_token"]),
                      "app_secret_set": bool(whatsapp.settings()["app_secret"]), "waba_id": cfg.get("waba_id", ""),
                      "sending_to_meta": bool(cfg.get("live") and cfg.get("dataset_id") and cfg.get("access_token"))},
            "totals": {"chats": sum(r["chats"] for r in rows),
                       "from_ads": sum(r["chats"] for r in rows if r["source_type"] == "ad"),
                       "buyers": sum(r["buyers"] for r in rows), "real_value": round(sum(r["real_value"] for r in rows), 2)},
            "sources": rows, "recent_sales_reported": recent,
        }

    @flask.post("/tracking/<client_id>")
    def new_tracking(client_id):
        """A tracking link and promo code for one new post, saved so sales match it."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        name = (body.get("post_name") or "").strip()
        platform = (body.get("platform") or "instagram").strip().lower()
        if not name:
            abort(400, "send JSON with a 'post_name'")
        if platform not in ("instagram", "tiktok", "facebook", "snapchat", "youtube", "whatsapp"):
            abort(400, "unknown platform")
        cfg = state.config(client_id)
        url = (body.get("url") or cfg.get("website") or "").strip()
        if url and not re.match(r"^https?://", url):
            url = "https://" + url
        post_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "post"
        link = tag_link(url, platform, post_id) if url else ""
        with state.lock:
            existing = {r["code"] for r in state.store.promos(client_id)}
            code = make_promo_code(post_id, prefix=name)
            while code in existing:
                code = make_promo_code(post_id, prefix=name)
            state.store.add_promo(client_id, code, platform, post_id, link)
            state.engine(client_id).promo_map[code] = (platform, post_id)
        return {"post_id": post_id, "platform": platform, "link": link, "code": code}

    @flask.get("/tracking/<client_id>")
    def list_tracking(client_id):
        state.require_key(client_id)
        with state.lock:
            return {"codes": state.store.promos(client_id)}

    @flask.errorhandler(403)
    @flask.errorhandler(429)
    @flask.errorhandler(409)
    @flask.errorhandler(401)
    @flask.errorhandler(400)
    @flask.errorhandler(404)
    @flask.errorhandler(502)
    @flask.errorhandler(503)
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
            try:
                if getattr(state, "enforce_ads_trials", None):
                    state.enforce_ads_trials()
            except Exception:
                log.exception("ad test check failed")
            try:
                if getattr(state, "retry_held_fees", None):
                    state.retry_held_fees()
            except Exception:
                log.exception("held fee retry failed")
            for cid in sorted(set(state.clients) | set(state.meta_conns.all_clients())):
                try:
                    if not state.known(cid):
                        continue
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
