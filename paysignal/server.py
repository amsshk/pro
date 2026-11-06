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
  POST /campaign/<client_id>           draft a Meta campaign (AI, best model)   [API key]
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
from datetime import datetime, timezone

from urllib.parse import urlencode

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

from .connectors import parse_csv, parse_shopify_webhook, parse_stripe_event
from .connectors.shopify import verify_hmac
from .connectors.stripe import verify_signature
from .engine import Engine
from . import growth, shopify_app
from .accounts import AccountStore, Limiter, check_password, hash_password, validate_signup
from .ai import MetaAI
from .attribution import make_promo_code, tag_link
from .assistant import CampaignError, ask, build_campaign, business_context
from .audit import audit_summary, audit_text, build_audit
from .importers import import_auto
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

    def known(self, client_id: str) -> bool:
        return client_id in self.clients or self.accounts.get(client_id) is not None

    def config(self, client_id: str) -> dict:
        cfg = self.clients.get(client_id)
        if cfg is not None:
            return cfg
        acct = self.accounts.get(client_id)
        if acct is None:
            abort(404, "unknown client")
        return {"name": acct["name"], "currency": acct["currency"], "website": acct["website"] or "",
                "self_serve": True}

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
        with self.lock:
            new = self.engine(client_id).ingest(payments)
        return {"received": len(payments), "new": new, "duplicates": len(payments) - new}

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
        res = import_auto(request.get_data(as_text=True), client_id, cfg.get("costs", {}),
                          request.args.get("platform", "instagram"))
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
            return business_context(eng, org, state.config(client_id).get("currency", "AED"))

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
        ai = need_ai()
        try:
            return build_campaign(ai, ai_context(client_id), budget, body.get("goal", ""), body.get("notes", ""))
        except PermissionError as e:
            abort(503, str(e))
        except (RuntimeError, CampaignError) as e:
            abort(502, str(e))

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
            for cache in (state.engines, state.posts, state.trained_at):
                cache.pop(client_id, None)
        log.info("account %s deleted by its owner: %s", client_id, counts)
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
            },
            "totals": totals, "posts": posts, "tracking_codes": codes,
            "webhooks": {"stripe": f"/webhooks/stripe/{client_id}", "shopify": f"/webhooks/shopify/{client_id}"},
        }

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
            abort(400, "Enter your store address, like your-store.myshopify.com")
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
            return redirect("/#/upload?" + q)
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
