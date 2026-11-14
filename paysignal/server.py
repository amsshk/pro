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

import base64
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
from . import growth, mailer, shopify_app, tiktok, whatsapp
from .identity import hash_phone
from .accounts import AccountStore, Limiter, check_password, hash_password, validate_signup
from .ai import MetaAI
from .attribution import make_promo_code, tag_link
from .dashboard import build_dashboard, _realized
from .ads_report import build_report
from .assistant import PROADS_PRODUCT, CampaignError, ask, build_campaign, business_context
from . import autopilot, billing, buyers, competitors, engines, forecast, leaks, learning, meta_capi, proads_revenue, purchase_funnel, delivery, scaling, social, stripe_sync, media, signals, ledger, meta_ads, meta_connect, settlement
from .connectors.stripe import verify_signature as stripe_signature_ok
from .audit import audit_summary, audit_text, build_audit
from .importers import xlsx_to_csv, import_auto
from .labeler import label_posts, make_labeler
from .organic import OrganicEngine, load_posts_csv
from .store import PaymentStore
from .models import LineItem, Payment

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
        self.lock = threading.RLock()         # one writer at a time; re-entrant so helpers can be called with it held
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
        self._ig_cache: dict = {}
        self.meta_conns = meta_connect.ConnectionStore(self.store.db)
        self.meta_http = None                  # set in tests
        self.billing = billing.BillingStore(self.store.db)
        self.ledger = ledger.SpendLedger(self.store.db)
        self.fees = settlement.FeePeriods(self.store.db)
        self.economics = engines.EconomicsStore(self.store.db)
        self.unpaid = leaks.UnpaidStore(self.store.db)
        self.signals = signals.SignalLog(self.store.db)
        self.experiments = buyers.ExperimentStore(self.store.db)
        self.revenue = proads_revenue.RevenueStore(self.store.db)
        self.funnel = purchase_funnel.FunnelStore(self.store.db)
        self.delivery = delivery.DeliveryLog(self.store.db)
        self.scaling = scaling.ScalingStore(self.store.db)
        self.social = social.SocialStore(self.store.db)
        self.social_http = None                    # requests, unless a test swaps it
        self.stripe_links = stripe_sync.StripeLinks(self.store.db)
        self.tiktok = tiktok.TikTokStore(self.store.db)
        self.tiktok_http = None                # set in tests
        self.stripe_http = None
        self.media = media.MediaStore(self.store.db)
        self.media_http = None
        self.media_resolver = None                 # socket.getaddrinfo, unless a test swaps it
        self._nudged: dict = {}
        self.stripe_http = None                # set in tests
        self.billing_sync = False              # tests: add invoice fees inline
        self.ads_cache: dict = {}              # (client, period) -> (time, report)

    def emit_growth(self, event: dict) -> bool:
        """Send one of ProAds' own events to Meta, recorded so a failure is seen and retried, never lost silently.
        The TikTok version (when PROADS_TIKTOK_PIXEL / _TOKEN are set) goes out alongside it."""
        tt = event.pop("_tiktok", None)
        if tt:
            self.emit_tiktok_growth(tt)
        with self.lock:
            if not self.delivery.queued("proads", event):
                return False                         # already sent or queued: never twice

        def done(result, eid=event["event_id"]):
            with self.lock:
                status = self.delivery.result(eid, result)
            if status != "accepted":
                log.warning("ProAds event %s not accepted by Meta yet (%s)", eid, status)
        growth.send_later(event, self.growth_http, self.growth_sent, on_done=done)
        return True

    def emit_tiktok_growth(self, ev: dict) -> None:
        cfg = tiktok.proads_settings()
        if not (cfg["pixel"] and cfg["token"]):
            return
        with self.lock:
            if self.tiktok.was_sent("proads", ev["event_id"]):
                return

        def run():
            res = tiktok.send([ev], cfg["pixel"], cfg["token"], self.tiktok_http or __import__("requests"), cfg["test_code"] or None)
            if res and res[0]["ok"]:
                with self.lock:
                    self.tiktok.mark_sent("proads", [ev["event_id"]])
            else:
                log.warning("TikTok didn't accept ProAds event %s: %s", ev["event"], res and res[0]["message"])
        if self.tiktok_http is not None:
            run()                                    # tests: synchronous
        else:
            threading.Thread(target=run, daemon=True, name="tiktok-growth").start()

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
            def dead(cid=client_id):
                with self.lock:
                    self.meta_conns.mark_expired(cid)
                log.warning("Facebook session for %s was invalidated: marked for reconnect", cid)
            s = {**meta_connect.ads_settings(c, meta_connect.decrypt(c["token"])), "on_token_invalid": dead}
            if not s["instagram_id"]:
                if is_admin and meta_ads.settings()["instagram_id"]:
                    s["instagram_id"] = meta_ads.settings()["instagram_id"]
                else:
                    found = self.instagram_lookup(client_id, s)
                    if found.get("id"):
                        with self.lock:
                            self.meta_conns.set_instagram(client_id, found["id"])
                        s["instagram_id"] = found["id"]
            return s
        if is_admin and not meta_ads.missing():
            s = meta_ads.settings()
            if not s["instagram_id"]:
                s["instagram_id"] = self.instagram_lookup(client_id, s).get("id", "")
            return {**s, "token_invalid_message":
                    "Facebook cancelled the token in Render's PROADS_ADS_TOKEN (a password change or security check). "
                    "Replace it with a System User token from Business Settings, which isn't tied to your password."}
        return None

    def instagram_lookup(self, client_id: str, s: dict, fresh: bool = False) -> dict:
        """Ask Meta which Instagram account goes with this Page / ad account (remembered for 10 minutes)."""
        key = (client_id, s.get("page_id"), s.get("ad_account"))
        hit = self._ig_cache.get(key)
        if hit and not fresh and hit[0] > time.time():
            return hit[1]
        try:
            found = meta_connect.find_instagram(self.ads_http, s["token"], s.get("page_id", ""), s.get("ad_account", ""))
        except Exception as e:                        # never block publishing on this check
            found = {"why": f"couldn't ask Meta ({type(e).__name__})"}
        self._ig_cache[key] = (time.time() + 600, found)
        return found

    def maybe_activate(self, client_id: str) -> None:
        """First data in from a self-serve account: tell Meta a real user started (once)."""
        acct = self.accounts.get(client_id)
        if not acct or acct["activated_at"]:
            return
        with self.lock:
            first = self.accounts.mark_activated(client_id)
        ctx = growth.load_ctx(acct["ads_ctx"])
        if first and ctx:
            self.emit_growth(growth.build_event("StartTrial", acct, ctx, f"activate:{client_id}"))

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
                if p.kind == "refund" and not self._refund_delta(client_id, p):
                    continue
                match = self._wa_match(client_id, p)
                if self.store.add(p):
                    new += 1
                    if match and match.get("ctwa_clid"):
                        wa_matches.append((p, match))
        for p, match in wa_matches:
            self._wa_report(client_id, p, match)
        if new and getattr(self, "instant_meta", False):
            self.nudge_meta(client_id)
        return {"received": len(payments), "new": new, "duplicates": len(payments) - new}

    def _refund_delta(self, client_id: str, p) -> bool:
        """Stripe reports the running total refunded on a charge ("<charge>:refund:<total>"). Record only the part
        not recorded yet, and never refund more than the sale. False = nothing new to record."""
        if not p.refund_of:
            return True
        db = self.store.db
        if db.execute("SELECT 1 FROM payments WHERE client_id=? AND source=? AND external_id=?",
                      (client_id, p.source, p.external_id)).fetchone():
            return False
        if ":refund:" in p.external_id:
            try:
                total = int(p.external_id.rsplit(":refund:", 1)[1])
            except ValueError:
                total = 0
            prev = 0
            for (eid,) in db.execute("SELECT external_id FROM payments WHERE client_id=? AND kind='refund' AND refund_of=?"
                                     " AND external_id LIKE ?", (client_id, p.refund_of, p.refund_of + ":refund:%")):
                try:
                    prev = max(prev, int(eid.rsplit(":refund:", 1)[1]))
                except ValueError:
                    pass
            if total <= prev:
                return False
            if total:
                p.amount = round(p.amount * (total - prev) / total, 2)
        sale = db.execute("SELECT amount FROM payments WHERE client_id=? AND kind='purchase' AND external_id=?",
                          (client_id, p.refund_of)).fetchone()
        if sale:
            room = round(sale[0] - self.store.refunded_amount(client_id, p.refund_of), 2)
            p.amount = min(p.amount, max(0.0, room))
        return p.amount > 0

    def nudge_meta(self, client_id: str) -> None:
        """Send new sales to Meta now instead of waiting for the 10-minute cycle (at most every 20 seconds)."""
        now = time.time()
        if now - self._nudged.get(client_id, 0) < 20:
            return
        self._nudged[client_id] = now

        def run():
            try:
                self.sync_meta(client_id)
            except Exception:
                log.exception("instant meta send failed for %s", client_id)
        threading.Thread(target=run, daemon=True, name="meta-now").start()

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
        value = round(p.amount, 2)                     # a Purchase is what was paid (or real profit, if chosen)
        if self.signals.value_mode(client_id) == "profit" and p.cost_of_goods() is not None:
            value = round(self.engine(client_id).scorer.margin(p), 2)
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
                dry_run=not live, test_event_code=meta.get("test_event_code"),
                value_mode=meta.get("value_mode") or self.signals.value_mode(client_id))
        with self.lock:
            self.signals.log(client_id, "live" if live else "practice", results)
        batches = [r for r in results if not r.get("report")]
        sent = sum(r["count"] if r.get("dry_run") else int(r.get("body", {}).get("events_received", 0) or 0)
                   for r in batches)
        errors = [r["body"] for r in batches if not r.get("dry_run") and r.get("status") != 200]
        return {"mode": "live" if live else "dry_run", "batches": len(batches), "events": sent, "errors": errors}


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
    VIDEO_MAX = 120 * 1024 * 1024

    @flask.before_request
    def _bigger_uploads_for_video():
        if request.path.endswith("/upload-video"):
            request.max_content_length = VIDEO_MAX + 1024
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

    # ---------- Stripe read automatically (restricted key) ----------
    def stripe_http():
        if state.stripe_http is not None:
            return state.stripe_http
        import requests
        return requests

    def stripe_sync_one(client_id: str) -> dict:
        row = state.stripe_links.get(client_id)
        if not row:
            return {"ok": False}
        now = int(time.time())
        since = (row["last_sync"] - stripe_sync.OVERLAP) if row["last_sync"] else now - stripe_sync.HISTORY_DAYS * 86400
        try:
            key = shopify_app.decrypt(row["key_enc"])
            got = stripe_sync.fetch(stripe_http(), key, client_id, since, now)
        except stripe_sync.StripeSyncError as e:
            with state.lock:
                state.stripe_links.result(client_id, False, str(e))
            log.info("stripe sync for %s: %s", client_id, e)
            return {"ok": False, "error": str(e)}
        except Exception as e:
            with state.lock:
                state.stripe_links.result(client_id, False, "Couldn't read Stripe right now. ProAds will try again shortly.")
            log.warning("stripe sync for %s failed: %s", client_id, e)
            return {"ok": False}
        res = state.ingest(client_id, got["payments"])
        msg = ("Imported your most recent 10,000 payments; older ones were skipped." if got["truncated"] else None)
        with state.lock:
            state.stripe_links.result(client_id, True, msg, res["new"], got["truncated"], now)
        if res["new"]:
            state.maybe_activate(client_id)
        return {"ok": True, **res}
    state.stripe_sync_one = stripe_sync_one

    def stripe_sync_all() -> None:
        with state.lock:
            ids = state.stripe_links.all()
        for cid in ids:
            stripe_sync_one(cid)
    state.stripe_sync_all = stripe_sync_all

    @flask.post("/connect/<client_id>/stripe")
    def stripe_connect(client_id):
        """Paste a restricted, read-only Stripe key once; payments then come in by themselves."""
        state.require_key(client_id)
        key = str((request.get_json(silent=True) or {}).get("key") or "").strip()
        try:
            info = stripe_sync.check_key(stripe_http(), key)
        except stripe_sync.StripeSyncError as e:
            abort(400, str(e))
        try:
            enc = shopify_app.encrypt(key)
        except shopify_app.ShopifyError:
            abort(503, "ProAds can't store keys safely yet (PROADS_SECRET_KEY is missing on the server).")
        with state.lock:
            state.stripe_links.save(client_id, enc, info["live"], info["kind"])
        if getattr(state, "billing_sync", False):
            stripe_sync_one(client_id)
        else:
            threading.Thread(target=stripe_sync_one, args=(client_id,), daemon=True).start()
        log.info("stripe key connected for %s (%s)", client_id, "live" if info["live"] else "test")
        return {"ok": True, **stripe_sync.StripeLinks.public(state.stripe_links.get(client_id)),
                "warning": None if info["live"] else "This is a TEST key: only Stripe test payments will come in."}

    # ---------- TikTok: real sales to TikTok's Events API ----------
    def tiktok_http():
        if state.tiktok_http is not None:
            return state.tiktok_http
        import requests
        return requests

    def tiktok_push(client_id: str) -> dict:
        """Send this business's new paid purchases to TikTok."""
        with state.lock:
            row = state.tiktok.get(client_id)
            if not row:
                return {"skipped": "not connected"}
            todo = tiktok.pending(state.store.purchases(client_id), state.tiktok, client_id)
        events = [tiktok.purchase_event(p) for p in todo]
        events = [e for e in events if tiktok.has_match_key(e)]
        if not events:
            with state.lock:
                if row["status"] == "waiting":
                    state.tiktok.result(client_id, True, "Connected. New sales will be sent to TikTok as they come in.")
            return {"sent": 0}
        token = shopify_app.decrypt(row["token_enc"])
        results = tiktok.send(events, row["pixel_code"], token, tiktok_http(), row["test_code"])
        sent_ids, bad = [], None
        for i, r in enumerate(results):
            if r["ok"]:
                sent_ids += [e["event_id"] for e in events[i * tiktok.BATCH:(i + 1) * tiktok.BATCH]]
            else:
                bad = r["message"]
        with state.lock:
            state.tiktok.mark_sent(client_id, sent_ids)
            if bad:
                state.tiktok.result(client_id, False, tiktok.explain(bad), len(sent_ids))
            else:
                state.tiktok.result(client_id, True, f"Sent {len(sent_ids)} sale(s) to TikTok" +
                                    (" as TEST events (see Events Manager > Test events)." if row["test_code"] else "."), len(sent_ids))
        if bad:
            log.warning("TikTok refused events of %s: %s", client_id, bad)
        return {"sent": len(sent_ids), "error": bad}
    state.tiktok_push = tiktok_push

    def tiktok_push_all() -> None:
        with state.lock:
            ids = state.tiktok.all()
        for cid in ids:
            try:
                tiktok_push(cid)
            except Exception:
                log.exception("TikTok send failed for %s", cid)
    state.tiktok_push_all = tiktok_push_all

    @flask.post("/connect/<client_id>/tiktok")
    def tiktok_connect(client_id):
        """Pixel ID + Events API access token from TikTok Events Manager; optional test event code."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        pixel = str(body.get("pixel_code") or "").strip().upper()
        token = str(body.get("access_token") or "").strip()
        test = str(body.get("test_event_code") or "").strip() or None
        if not tiktok.PIXEL_RE.match(pixel):
            abort(400, "That doesn't look like a TikTok Pixel ID. In TikTok Events Manager, copy the ID shown under the Pixel's name (letters and numbers, like C4ABC123DEF456).")
        if len(token) < 20 or " " in token:
            abort(400, "Paste the Events API access token from TikTok Events Manager (Pixel > Settings > Generate access token).")
        try:
            enc = shopify_app.encrypt(token)
        except shopify_app.ShopifyError:
            abort(503, "ProAds can't store keys safely yet (PROADS_SECRET_KEY is missing on the server).")
        with state.lock:
            state.tiktok.save(client_id, pixel, enc, test)
        res = tiktok_push(client_id)
        log.info("tiktok connected for %s (%s)", client_id, "test" if test else "live")
        with state.lock:
            out = tiktok.TikTokStore.public(state.tiktok.get(client_id))
        return {"ok": not res.get("error"), **out}

    @flask.post("/connect/<client_id>/tiktok/disconnect")
    def tiktok_disconnect(client_id):
        state.require_key(client_id)
        with state.lock:
            state.tiktok.remove(client_id)
        return {"ok": True, "note": "ProAds deleted the TikTok token. You can also delete it in TikTok Events Manager."}

    @flask.post("/connect/<client_id>/stripe/disconnect")
    def stripe_disconnect(client_id):
        state.require_key(client_id)
        with state.lock:
            state.stripe_links.remove(client_id)
        return {"ok": True, "note": "ProAds deleted the key. To be thorough, also delete it in Stripe (Developers → API keys)."}

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
        kind = (b"\x89PNG" == raw[:4] and "picture (PNG)") or (raw[:3] == b"\xff\xd8\xff" and "photo (JPG)") or \
            (raw[:4] == b"%PDF" and "PDF") or (raw[4:12] in (b"ftypheic", b"ftypmif1", b"ftypheix") and "photo (HEIC)") or \
            (raw[:4] in (b"GIF8", b"RIFF") and "picture")
        if kind:
            abort(400, f"That's a {kind}, not a spreadsheet. ProAds reads sales from a CSV or Excel file: in Shopify or "
                       "Stripe use Export, or save your sheet as CSV. To show a screenshot to support, send it in chat instead.")
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
        if res.get("unpaid"):
            with state.lock:
                out["unpaid_kept"] = state.unpaid.add(client_id, res["unpaid"])
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

    @flask.get("/lessons/<client_id>")
    def lessons_get(client_id):
        state.require_key(client_id)
        with state.lock:
            return {"share": state.learnings.sharing(client_id)}

    @flask.post("/lessons/<client_id>")
    def lessons_set(client_id):
        """Share anonymous lessons or not. Turning it off also deletes what this business contributed."""
        state.require_key(client_id)
        share = (request.get_json(silent=True) or {}).get("share")
        if not isinstance(share, bool):
            abort(400, "send JSON with 'share': true or false")
        with state.lock:
            state.learnings.set_sharing(client_id, share)
        return {"share": share}

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
            state.unpaid.clear(client_id)
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
        return ads_report_for(client_id, request.args.get("period", "30"), bool(request.args.get("fresh")))

    def ads_report_for(client_id: str, period: str = "30", fresh: bool = False) -> dict:
        days = {"today": 1, "7": 7, "30": 30, "90": 90, "365": 365}.get(period, 30)
        admin = is_admin(client_id)
        sync_launch_status(client_id, force=fresh)
        with state.lock:
            launches = state.launches.list(client_id)
        if not launches:
            return {"has_campaigns": False}
        hit = state.ads_cache.get((client_id, period))
        if hit and time.time() - hit[0] < 600 and not fresh:
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

    # ---------------- Profit engines: Zero-Waste Budget + Ad Autopsy ----------------
    def economics_hints(client_id) -> dict:
        """Numbers ProAds already knows from the sales history, to prefill the form."""
        with state.lock:
            eng = state.engine(client_id)
            audit = build_audit(eng, None, currency=state.config(client_id).get("currency", "AED"))
        if "error" in audit or not audit.get("orders"):
            return {}
        out = {"price": round(audit["gross"] / audit["orders"], 2), "refund_rate": audit.get("refund_rate", 0)}
        cu = audit.get("customers") or {}
        if cu.get("known"):
            out["orders_per_customer"] = round(audit["orders"] / cu["known"], 2)
        return out

    def economics_for(client_id) -> Optional[dict]:
        with state.lock:
            inp = state.economics.get(client_id)
        return engines.economics(inp) if inp else None

    @flask.post("/engines/<client_id>/economics")
    def economics_save(client_id):
        state.require_key(client_id)
        try:
            inp = engines.clean_inputs(request.get_json(silent=True) or {})
        except ValueError as e:
            abort(400, str(e))
        with state.lock:
            state.economics.save(client_id, inp)
        state.ads_cache.pop(("engines", client_id), None)
        return engines.economics(inp)

    @flask.get("/engines/<client_id>")
    def engines_view(client_id):
        """Max affordable cost per customer, each campaign judged against it, and its autopsy."""
        state.require_key(client_id)
        return engines_for(client_id, bool(request.args.get("fresh")))

    def engines_for(client_id: str, fresh: bool = False) -> dict:
        admin = is_admin(client_id)
        econ = economics_for(client_id)
        out = {"economics": econ, "hints": {} if econ else economics_hints(client_id), "campaigns": [],
               "currency": ads_currency(client_id)}
        with state.lock:
            launches = state.launches.list(client_id)
        if not launches:
            return out
        hit = state.ads_cache.get(("engines", client_id))
        if hit and time.time() - hit[0] < 600 and not fresh:
            return {**out, **hit[1]}
        s = state.ads_settings(client_id, admin)
        if s is None:
            return {**out, "error": "Connect Facebook & Instagram again to judge your campaigns."}
        today = datetime.now(timezone.utc).date()
        since = (today - timedelta(days=29)).isoformat()
        pub = meta_ads.Publisher(state.ads_http, s)
        rows = []
        for l in launches:
            kind = "signup" if admin else (l.get("conversion") or "purchase")
            try:
                data = pub.insights([l["campaign_id"]], since, today.isoformat(),
                                    fields="spend,impressions,clicks,inline_link_clicks,frequency,cpm,actions,action_values,account_currency")
            except meta_ads.PublishError as e:
                rows.append({"campaign_id": l["campaign_id"], "name": l.get("name", ""), "error": str(e)[:200]})
                continue
            f = engines.funnel(data[0] if data else {})
            rows.append({"campaign_id": l["campaign_id"], "name": l.get("name", ""), "status": l["status"], "kind": kind,
                         "f": f, "currency": (data[0].get("account_currency") if data else "") or ""})
        # real paying orders from Meta ads (after refunds), to see how much of Meta's count is real money
        meta_total = sum((r["f"]["purchase"] if r.get("kind") == "purchase" else r["f"]["signup"]) for r in rows if "f" in r)
        real = None
        if not admin and meta_total:
            with state.lock:
                eng = state.engine(client_id)
                start = today - timedelta(days=29)
                from_meta = lambda p: (p.utm_medium or "").lower() == "paid" and \
                    (p.utm_source or "").lower() in ("meta", "facebook", "instagram", "fb", "ig")
                purchases = list(eng.store.purchases(client_id))
                # only compare when this business's sales data actually carries ad tracking; otherwise "unknown", not zero
                if any(from_meta(p) for p in purchases):
                    real = sum(1 for p in purchases if start <= p.occurred_at.date() <= today and from_meta(p)
                               and eng.store.refunded_amount(client_id, p.external_id) < p.amount)
        ratio = round(real / meta_total, 2) if real is not None and meta_total else None
        camps = []
        for r in rows:
            if "f" not in r:
                camps.append(r)
                continue
            f, kind = r.pop("f"), r["kind"]
            goal = f["purchase"] if kind == "purchase" else f["signup"]
            item = {**r, "spend": f["spend"], "results": goal, "autopsy": engines.autopsy(f, kind, econ, ratio)}
            if econ and kind == "purchase":
                sales = goal * ratio if ratio is not None else goal
                item["verdict"] = engines.verdict(f["spend"], sales, econ["target_cpa"], econ["break_even_cpa"])
                item["verdict"]["based_on"] = "real paid sales (estimated from your sales data)" if ratio is not None else "Meta's reported purchases"
            camps.append(item)
        result = {"campaigns": camps, "real_ratio": ratio, "period_days": 30, "updated_at": int(time.time())}
        state.ads_cache[("engines", client_id)] = (time.time(), result)
        return {**out, **result}

    @flask.get("/learning/<client_id>")
    def learning_view(client_id):
        """Meta Learning Accelerator: what reached Meta, how well it can be matched, double counting, learning status."""
        state.require_key(client_id)
        admin = is_admin(client_id)
        cfg = state.config(client_id)
        meta = cfg.get("meta", {})
        with state.lock:
            sig = state.signals.summary(client_id)
            last_sale = max((p.occurred_at for p in state.engine(client_id).store.purchases(client_id)), default=None)
            launches = state.launches.list(client_id)
            econ_in = state.economics.get(client_id)
            sent7 = state.signals.sent_purchases_since(client_id, int(time.time()) - 7 * 86400)
        with state.lock:
            mode = state.signals.value_mode(client_id)
            costs_known = any(p.cost_of_goods() is not None for p in state.engine(client_id).store.purchases(client_id)[-200:])
        out = {"signals": sig, "pixel": meta.get("dataset_id") or None, "live": bool(meta.get("live")),
               "value_mode": mode, "costs_known": costs_known,
               "last_sale_at": int(last_sale.timestamp()) if last_sale else None, "admin": admin}
        hit = state.ads_cache.get(("learning", client_id))
        if hit and time.time() - hit[0] < 600 and not request.args.get("fresh"):
            return {**out, **hit[1]}
        s = state.ads_settings(client_id, admin)
        extra = {"double_count": None, "campaigns": []}
        if s:
            version = meta_ads.settings()["version"]
            token = s.get("token")
            if meta.get("dataset_id") and not admin:
                n = signals.pixel_purchases(state.ads_http, token, meta["dataset_id"], version, int(time.time()) - 7 * 86400)
                extra["double_count"] = signals.double_count_check(n, sent7)
            target = engines.economics(econ_in)["target_cpa"] if econ_in else None
            for l in launches:
                kind = "signup" if admin else (l.get("conversion") or "purchase")
                try:
                    r = state.ads_http.get(f"https://graph.facebook.com/{version}/{l['campaign_id']}/adsets",
                                           params={"access_token": token, "limit": 50,
                                                   "fields": "name,effective_status,daily_budget,learning_stage_info"}, timeout=20)
                    body = r.json()
                except Exception as e:
                    extra["campaigns"].append({"campaign_id": l["campaign_id"], "name": l.get("name", ""), "error": str(e)[:150]})
                    continue
                if r.status_code != 200 or "error" in body:
                    extra["campaigns"].append({"campaign_id": l["campaign_id"], "name": l.get("name", ""),
                                               "error": ((body.get("error") or {}).get("message") or "Meta refused")[:150]})
                    continue
                cur = (s.get("currency") or ads_currency(client_id) or "").upper()
                div = 1 if cur in meta_ads.ZERO_DECIMAL else 100
                sets = []
                for a in body.get("data") or []:
                    li = a.get("learning_stage_info") or {}
                    st = (li.get("status") or "").upper()
                    sets.append({"name": a.get("name", ""), "effective_status": a.get("effective_status"),
                                 "daily_budget": float(a.get("daily_budget") or 0) / div,
                                 "learning": {"LEARNING": "learning", "SUCCESS": "learned", "FAIL": "limited"}.get(st, "unknown"),
                                 "conversions_week": li.get("conversions"), "last_edit": li.get("last_sig_edit_ts")})
                extra["campaigns"].append({"campaign_id": l["campaign_id"], "name": l.get("name", ""), "status": l["status"],
                                           "kind": kind, "adsets": sets,
                                           "advice": signals.learning_advice(sets, target, kind)})
        state.ads_cache[("learning", client_id)] = (time.time(), extra)
        return {**out, **extra}

    @flask.post("/learning/<client_id>/settings")
    def learning_settings(client_id):
        """What a Purchase's value means for this business: amount paid (default) or actual profit on the order."""
        state.require_key(client_id)
        mode = (request.get_json(silent=True) or {}).get("value_mode")
        if mode not in meta_capi.VALUE_MODES:
            abort(400, "value_mode must be 'amount' or 'profit'")
        with state.lock:
            state.signals.set_value_mode(client_id, mode)
        return {"value_mode": mode}

    # ---------------- Buyer Quality Intelligence ----------------
    def buyer_inputs(client_id):
        with state.lock:
            eng = state.engine(client_id)
            promo = {p["code"].upper(): p["platform"] for p in eng.store.promos(client_id)} if hasattr(eng.store, "promos") else {}
            launches = state.launches.list(client_id)
            names = {meta_ads._slug(l.get("name", ""), 30): l.get("name", "") for l in launches}
            custs, basis, last = buyers.build_customers(eng, promo, names)
        return custs, basis, last, launches

    def refund_delay_days(client_id) -> int:
        """How long refunds take to show up at THIS business (90th percentile), so results aren't trusted too early."""
        with state.lock:
            store = state.engine(client_id).store
            buys = {p.external_id: p.occurred_at for p in store.purchases(client_id)}
            rows = store.db.execute("SELECT data FROM payments WHERE client_id=? AND kind='refund'", (client_id,)).fetchall()
        delays = []
        for (raw,) in rows:
            d = json.loads(raw)
            t0 = buys.get(d.get("refund_of") or "")
            if t0:
                delays.append((datetime.fromisoformat(d["occurred_at"]) - t0).days)
        if len(delays) < 10:
            return 14
        delays.sort()
        return max(7, min(60, delays[int(0.9 * (len(delays) - 1))]))

    def experiment_arms(client_id, e: dict, launches: list, settle_days: int) -> tuple[dict, dict]:
        """Per arm, since the test started: recorded spend and the net value of each real paid purchase.
        'settled' only counts purchases (and spend) old enough for refunds to have arrived."""
        start = datetime.fromtimestamp(e["started_at"], timezone.utc)
        today = datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=settle_days)
        slug = {l["campaign_id"]: l.get("utm_slug") or meta_ads._slug(l.get("name", ""), 30) for l in launches}
        prelim, settled = {}, {}
        with state.lock:
            eng = state.engine(client_id)
            purchases = eng.store.purchases(client_id)
            for arm, camp in (("control", e["control_campaign"]), ("variant", e["variant_campaign"])):
                spend, _ = state.ledger.total([camp], start.date().isoformat(), today.isoformat())
                spend_s, _ = (state.ledger.total([camp], start.date().isoformat(), cutoff.isoformat())
                              if cutoff >= start.date() else (0.0, ""))
                vals, vals_s = [], []
                for p in purchases:
                    if p.occurred_at < start or (p.utm_medium or "").lower() != "paid" or (p.utm_campaign or "") != slug.get(camp):
                        continue
                    ref = min(eng.store.refunded_amount(client_id, p.external_id), p.amount)
                    v = p.amount - ref - (p.cost_of_goods() or 0.0)        # a fully refunded sale counts as 0, not dropped
                    vals.append(v)
                    if p.occurred_at.date() <= cutoff:
                        vals_s.append(v)
                prelim[arm] = {"campaign_id": camp, "spend": spend, "values": vals}
                settled[arm] = {"campaign_id": camp, "spend": spend_s, "values": vals_s}
        return prelim, settled

    @flask.get("/buyers/<client_id>")
    def buyers_view(client_id):
        """Which real buyers made this business money, what brought them, and what to test next on Meta."""
        state.require_key(client_id)
        custs, basis, last, launches = buyer_inputs(client_id)
        cur = state.config(client_id).get("currency", "AED")
        an = buyers.analyse(custs, last, cur)
        an["value_basis"] = basis
        with state.lock:
            history = state.experiments.list(client_id)
            econ_in = state.economics.get(client_id)
        econ = engines.economics(econ_in) if econ_in else None
        settle_days = refund_delay_days(client_id) if any(e["status"] == "running" for e in history) else 14
        for e in history:
            if e["status"] == "running" and e["started_at"]:
                prelim, settled = experiment_arms(client_id, e, launches, settle_days)
                e["result"] = buyers.evaluate(prelim, settled, (e["design"] or {}).get("min_spend_per_arm"), e["started_at"],
                                              (e["design"] or {}).get("max_days", 14), settle_days)
                with state.lock:
                    state.experiments.save_result(client_id, e["id"], e["result"])
        names = {l["campaign_id"]: l.get("name", "") for l in launches}
        return {"analysis": an, "suggestions": buyers.suggest(an, history, econ, (econ_in or {}).get("daily_budget")),
                "experiments": history, "campaigns": [{"campaign_id": k, "name": v} for k, v in names.items()]}

    @flask.post("/buyers/<client_id>/experiments")
    def experiment_create(client_id):
        """Save a suggested experiment (rebuilt on the server from real data, never taken from the browser)."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        custs, basis, last, _ = buyer_inputs(client_id)
        an = buyers.analyse(custs, last, state.config(client_id).get("currency", "AED"))
        with state.lock:
            history = state.experiments.list(client_id)
            econ_in = state.economics.get(client_id)
        if any(h["dimension"] == body.get("dimension") and h["segment"] == body.get("segment")
               and h["status"] in ("planned", "running") for h in history):
            abort(409, "This test is already planned or running.")
        sug = buyers.suggest(an, history, engines.economics(econ_in) if econ_in else None, (econ_in or {}).get("daily_budget"))
        pick = next((x for x in sug if x["dimension"] == body.get("dimension") and x["segment"] == body.get("segment")), None)
        if not pick:
            abort(404, "That idea isn't supported by your data any more.")
        with state.lock:
            eid = state.experiments.add(client_id, pick)
        return {"id": eid}

    @flask.post("/buyers/<client_id>/experiments/<int:eid>/start")
    def experiment_start(client_id, eid):
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        with state.lock:
            ids = {l["campaign_id"] for l in state.launches.list(client_id)}
            e = state.experiments.get(client_id, eid)
        if not e:
            abort(404, "no such experiment")
        c, v = str(body.get("control_campaign") or ""), str(body.get("variant_campaign") or "")
        if c not in ids or v not in ids or c == v:
            abort(400, "pick two different campaigns published through ProAds: the control and the variant")
        split_id = re.sub(r"\D", "", str(body.get("split_test_id") or ""))[:30]
        s_ = state.ads_settings(client_id, is_admin(client_id))
        if not s_:
            abort(409, "Connect Facebook & Instagram so ProAds can check both campaigns are a fair test.")
        sets = {}
        for camp in (c, v):
            try:
                r = state.ads_http.get(f"https://graph.facebook.com/{meta_ads.settings()['version']}/{camp}/adsets",
                                       params={"access_token": s_["token"], "limit": 50, "fields":
                                               "optimization_goal,promoted_object,attribution_spec,targeting,daily_budget"}, timeout=20)
                body_ = r.json()
            except Exception as ex:
                abort(502, f"Couldn't read campaign {camp} from Meta: {ex}")
            if r.status_code != 200 or "error" in body_:
                abort(502, "Meta: " + ((body_.get("error") or {}).get("message") or "refused"))
            sets[camp] = body_.get("data") or []
        checks = buyers.control_checks(sets[c], sets[v], split_id)
        bad = [x for x in checks if not x["ok"] and not x.get("warning_only")]
        if bad and not body.get("force"):
            resp = jsonify({"error": "Not a fair test yet: " + "; ".join(x["label"].lower() + " fails" for x in bad) + ".",
                            "checks": checks, "can_force": True})
            resp.status_code = 409
            abort(resp)
        with state.lock:
            state.experiments.start(client_id, eid, c, v, checks, split_id)
        return {"started": True, "checks": checks}

    @flask.post("/buyers/<client_id>/experiments/<int:eid>/decide")
    def experiment_decide(client_id, eid):
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        if body.get("decision") not in ("keep", "reject", "inconclusive"):
            abort(400, "decision must be keep, reject or inconclusive")
        with state.lock:
            if not state.experiments.get(client_id, eid):
                abort(404, "no such experiment")
            state.experiments.decide(client_id, eid, body["decision"], str(body.get("note") or ""))
        return {"decided": body["decision"]}

    # ---------------- Autonomous Experiment Engine ----------------
    ACTIVE_EXP = ("proposed", "approved", "running")

    def autopilot_state(client_id):
        """Stages 1-3: opportunities from every engine, three proposals, sizing and the next best experiment."""
        custs, basis, last, launches = buyer_inputs(client_id)
        cur = state.config(client_id).get("currency", "AED")
        an = buyers.analyse(custs, last, cur)
        try:
            eng = engines_for(client_id)
        except Exception as e:
            log.info("autopilot: profit engine skipped: %s", e)
            eng = {}
        try:
            lk = leaks_for(client_id)
        except Exception as e:
            log.info("autopilot: leaks skipped: %s", e)
            lk = {}
        learn = (state.ads_cache.get(("learning", client_id)) or (0, {}))[1]
        with state.lock:
            history = state.experiments.list(client_id)
            econ_in = state.economics.get(client_id)
        hist = [{**h, "kind": h.get("kind") or {"product": "product", "offer": "offer"}.get(h["dimension"], "creative")}
                for h in history]
        ops = autopilot.discover(an, eng, lk, learn, hist)
        econ = engines.economics(econ_in) if econ_in else None
        spent = sum(c.get("spend") or 0 for c in eng.get("campaigns", []) if c.get("results"))
        results = sum(c.get("results") or 0 for c in eng.get("campaigns", []) if c.get("results"))
        cpp = (spent / results) if results >= 5 else ((econ or {}).get("target_cpa") or (econ or {}).get("break_even_cpa"))
        sizing = autopilot.test_budget(cpp, (econ or {}).get("margin_per_sale"), (econ_in or {}).get("daily_budget"))
        if results >= 5:
            sizing["based_on"] = "your real cost per purchase from recent campaigns"
        elif cpp:
            sizing["based_on"] = "your target cost per customer (no campaign history yet)"
        props = autopilot.proposals(ops, sizing)
        return {"opportunities": ops, "proposals": props, "next_best": autopilot.next_best(props), "sizing": sizing,
                "experiments": [h for h in history if h.get("kind")], "launches": launches, "buyers_ready": an.get("ready")}

    @flask.get("/autopilot/<client_id>")
    def autopilot_view(client_id):
        state.require_key(client_id)
        st_ = autopilot_state(client_id)
        settle_days = refund_delay_days(client_id)
        for e in st_["experiments"]:
            if e["status"] == "running" and e["started_at"]:
                prelim, settled = experiment_arms(client_id, e, st_["launches"], settle_days)
                e["result"] = buyers.evaluate(prelim, settled, (e["design"] or {}).get("min_spend_per_arm"), e["started_at"],
                                              14, settle_days)
                # loss guard: settled value the variant lost vs the control's rate, against the agreed limit
                sv = e["result"]["settled"]
                limit = (e["design"] or {}).get("max_acceptable_loss")
                if sv["control"]["spend"] and sv["variant"]["spend"] and limit:
                    lost = (sv["control"]["value"] / sv["control"]["spend"] - sv["variant"]["value"] / sv["variant"]["spend"]) * sv["variant"]["spend"]
                    e["result"]["loss_so_far"] = round(max(0.0, lost), 2)
                    e["result"]["loss_alert"] = lost > limit
                with state.lock:
                    state.experiments.save_result(client_id, e["id"], e["result"])
        st_["campaigns"] = [{"campaign_id": l["campaign_id"], "name": l.get("name", ""), "status": l["status"]}
                            for l in st_.pop("launches") if not l.get("experiment")]
        return st_

    def meta_test_for(client_id) -> autopilot.MetaTest:
        s_ = state.ads_settings(client_id, is_admin(client_id))
        if not s_:
            abort(409, "Connect Facebook & Instagram (with an ad account) first.")
        return autopilot.MetaTest(state.ads_http, s_["token"], s_["ad_account"], meta_ads.settings()["version"])

    def busy_campaigns(client_id) -> set:
        with state.lock:
            return {c for e in state.experiments.list(client_id) if e["status"] in ACTIVE_EXP + ("planned",)
                    for c in (e.get("control_campaign"), e.get("variant_campaign")) if c}

    @flask.post("/autopilot/<client_id>/prepare")
    def autopilot_prepare(client_id):
        """Stage 4a: read the control from Meta and show EXACTLY what the variant would change. Nothing is created."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        st_ = autopilot_state(client_id)
        prop = next((p for p in st_["proposals"] if p["key"] == body.get("key")), None)
        if not prop:
            abort(404, "That proposal isn't supported by your data any more.")
        if prop.get("not_a_split_test"):
            abort(400, "This one is a structure fix, not a split test: apply it in Ads Manager (merge the ad sets).")
        control = str(body.get("control_campaign") or prop.get("control_campaign") or "")
        if control not in {l["campaign_id"] for l in st_["launches"]}:
            abort(400, "Pick the control: a live campaign published through ProAds.")
        if control in busy_campaigns(client_id):
            abort(409, "That campaign is already part of another test. One test per campaign keeps control groups clean.")
        change = {k: (str(v).strip() or None) for k, v in (body.get("change") or {}).items() if k in ("message", "name", "link") and v}
        mt = meta_test_for(client_id)
        try:
            snap = mt.snapshot(control)
        except autopilot.MetaTestError as e:
            abort(502, str(e))
        if not change and prop["kind"] in ("creative", "product", "offer"):
            ai = state.ai or MetaAI()
            if ai.available:
                ld = ((snap["ads"][0].get("creative") or {}).get("object_story_spec") or {}).get("link_data") or {} if snap["ads"] else {}
                try:
                    reply = ai.chat("campaign_builder", [
                        {"role": "developer", "content": "Rewrite ONE Meta ad for a controlled test. Change only what the brief "
                         "says; keep the offer and facts unless the brief is about the offer. Never invent prices, results "
                         "or discounts. English. Reply with only JSON {\"message\": str (max 300 chars), \"name\": str (max 40 chars)}."},
                        {"role": "user", "content": json.dumps({"change": prop["change"], "why": prop["why"],
                                                                 "current_text": ld.get("message"), "current_headline": ld.get("name")})}],
                        max_completion_tokens=800)
                    t = reply.text
                    j = json.loads(t[t.find("{"):t.rfind("}") + 1])
                    change = {"message": str(j.get("message", ""))[:300] or None, "name": str(j.get("name", ""))[:40] or None}
                except Exception as e:
                    log.info("autopilot copy draft failed: %s", e)
        if prop["kind"] == "landing" and not change.get("link"):
            abort(400, "Give the new landing page link for the variant.")
        if not any(change.values()):
            abort(400, "Write the variant's text, headline or link (the one thing that changes).")
        preview = autopilot.MetaTest.preview(snap, change)
        if not preview:
            abort(400, "The variant would be identical to the control. Change the text, headline or link.")
        plan = {"proposal": {k: prop[k] for k in ("key", "kind", "label", "change", "why", "benefit_text", "unchanged", "measure")},
                "change": change, "preview": preview,
                "control": {"campaign_id": control, "adsets": len(snap["adsets"]), "ads": len(snap["ads"]),
                            "daily_budget": sum(float(a.get("daily_budget") or 0) for a in snap["adsets"])
                            / (1 if ads_currency(client_id).upper() in meta_ads.ZERO_DECIMAL else 100)}}
        with state.lock:
            eid = state.experiments.add_proposal(client_id, {**prop, "budget": {**(prop["budget"] or {})}}, plan, control)
        return {"id": eid, "plan": plan, "budget": prop["budget"]}

    @flask.post("/autopilot/<client_id>/dismiss")
    def autopilot_dismiss(client_id):
        """Owner rejects a proposal without testing it: recorded, and never proposed again."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        st_ = autopilot_state(client_id)
        prop = next((p for p in st_["proposals"] if p["key"] == body.get("key")), None)
        if not prop:
            abort(404, "No such proposal.")
        with state.lock:
            eid = state.experiments.add_proposal(client_id, prop, {"proposal": {k: prop[k] for k in ("key", "kind", "label", "change", "why")},
                                                                     "dismissed": True}, prop.get("control_campaign") or "")
            state.experiments.decide(client_id, eid, "reject", str(body.get("reason") or "Rejected by the owner without a test")[:300])
        return {"dismissed": True}

    @flask.post("/autopilot/<client_id>/experiments/<int:eid>/approve")
    def autopilot_approve(client_id, eid):
        """Stage 4b (approval 1): create the variant in Meta, PAUSED, with Meta's A/B test. Nothing goes live."""
        state.require_key(client_id)
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            abort(400, 'Approval needed: send {"confirm": true}')
        with state.lock:
            e = state.experiments.get(client_id, eid)
            launches = state.launches.list(client_id)
        if not e or e["status"] != "proposed":
            abort(404, "No proposed test with that number.")
        with state.lock:
            others = [x for x in state.experiments.list(client_id) if x["id"] != eid and x["status"] in ACTIVE_EXP + ("planned",)]
        if any(e["control_campaign"] in (x.get("control_campaign"), x.get("variant_campaign")) for x in others):
            abort(409, "The control is already in another active test. One test per campaign keeps control groups clean.")
        control = next((l for l in launches if l["campaign_id"] == e["control_campaign"]), None)
        if not control:
            abort(404, "The control campaign is no longer in ProAds.")
        plan = e["plan"] or {}
        change = dict(plan.get("change") or {})
        # the variant gets its own tracking tag, so real sales can be told apart from the control's
        ctl_slug = control.get("utm_slug") or meta_ads._slug(control.get("name", ""), 30)
        var_slug = f"{ctl_slug[:22]}-test{eid}"
        mt = meta_test_for(client_id)
        try:
            snap = mt.snapshot(control["campaign_id"])
            base_link = change.get("link") or next((((a.get("creative") or {}).get("object_story_spec") or {}).get("link_data", {}).get("link")
                                                   for a in snap["ads"]), None)
            if base_link:
                from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
                u = urlparse(base_link)
                q = dict(parse_qsl(u.query))
                q["utm_campaign"] = var_slug
                if q.get("utm_content"):
                    q["utm_content"] = q["utm_content"].replace(ctl_slug, var_slug, 1)
                change["link"] = urlunparse(u._replace(query=urlencode(q)))
            made = mt.create_paused(snap, change, f"ProAds test {eid}: {plan.get('proposal', {}).get('label', '')}", 14)
        except autopilot.MetaTestError as ex:
            abort(502, str(ex))
        with state.lock:
            state.launches.add(client_id, {"campaign_id": made["variant_campaign"], "name": f"{control.get('name', '')} · test {eid}",
                                           "ad_sets": [], "ads": [a["ad_id"] for a in made["ads"]], "status": "PAUSED",
                                           "conversion": control.get("conversion"), "utm_slug": var_slug, "experiment": eid})
            state.experiments.set_fields(client_id, eid, status="approved", variant_campaign=made["variant_campaign"],
                                         split_test_id=made.get("split_test_id"), meta=made)
        return {"status": "approved", **made, "live": False}

    @flask.post("/autopilot/<client_id>/experiments/<int:eid>/start")
    def autopilot_start(client_id, eid):
        """Approval 2: switch the variant on. The control must already be live; budgets are untouched."""
        state.require_key(client_id)
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            abort(400, 'Approval needed: send {"confirm": true}')
        with state.lock:
            e = state.experiments.get(client_id, eid)
            launches = {l["campaign_id"]: l for l in state.launches.list(client_id)}
        if not e or e["status"] != "approved":
            abort(404, "No approved test with that number.")
        if (launches.get(e["control_campaign"]) or {}).get("status") != "ACTIVE":
            abort(409, "Switch the control campaign on first (Go live), so both sides run at the same time.")
        try:
            meta_test_for(client_id).set_status(e["variant_campaign"], "ACTIVE")
        except autopilot.MetaTestError as ex:
            abort(502, str(ex))
        with state.lock:
            state.launches.set_status(client_id, e["variant_campaign"], "ACTIVE")
            state.experiments.set_fields(client_id, eid, status="running", started_at=int(time.time()))
        return {"status": "running"}

    @flask.post("/autopilot/<client_id>/experiments/<int:eid>/pause-variant")
    def autopilot_pause(client_id, eid):
        """Owner-approved stop (e.g. the loss limit was reached). Pausing never needs a second approval to be safe."""
        state.require_key(client_id)
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            abort(400, 'Approval needed: send {"confirm": true}')
        with state.lock:
            e = state.experiments.get(client_id, eid)
        if not e or not e.get("variant_campaign"):
            abort(404, "No test with a variant.")
        try:
            meta_test_for(client_id).set_status(e["variant_campaign"], "PAUSED")
        except autopilot.MetaTestError as ex:
            abort(502, str(ex))
        with state.lock:
            state.launches.set_status(client_id, e["variant_campaign"], "PAUSED")
        return {"variant": "PAUSED"}

    @flask.get("/leaks/<client_id>")
    def leaks_endpoint(client_id):
        """Where the business loses money outside winning new customers, ranked by AED a month."""
        state.require_key(client_id)
        return leaks_for(client_id)

    def leaks_for(client_id):
        cur = state.config(client_id).get("currency", "AED")
        losing = []
        try:
            eng_out = engines_for(client_id)
            losing = [{"campaign_id": c["campaign_id"], "name": c.get("name", ""), "spend": c["spend"],
                       **{k: c["verdict"].get(k) for k in ("label", "confidence", "cpa")}}
                      for c in eng_out.get("campaigns", []) if c.get("verdict", {}).get("verdict") == "losing" and c.get("spend")]
        except Exception as e:
            log.info("losing ads for leaks skipped: %s", e)
        econ = economics_for(client_id)                       # takes the lock itself: not nested
        with state.lock:
            eng = state.engine(client_id)
            since = (datetime.now(timezone.utc).date() - timedelta(days=leaks.LOOKBACK_DAYS - 1)).isoformat()
            return leaks.build_leaks(eng, cur, state.unpaid.since(client_id, since), econ, losing)

    @flask.get("/forecast/<client_id>")
    def forecast_endpoint(client_id):
        """Next 30 days of real revenue (sales minus refunds), with a tested range."""
        state.require_key(client_id)
        cur = state.config(client_id).get("currency", "AED")
        with state.lock:
            eng = state.engine(client_id)
            purchases = list(eng.store.purchases(client_id))
            rows = [(p.occurred_at.date(), p.amount - eng.store.refunded_amount(client_id, p.external_id)) for p in purchases]
        if not rows:
            return forecast.build_forecast([], 0, datetime.now(timezone.utc).date(), cur)
        today = datetime.now(timezone.utc).date()
        last = max(d for d, _ in rows)
        end = today if (today - last).days < 30 else last          # old uploads: forecast from where the data ends
        out = forecast.build_forecast([r for r in rows if r[0] <= end], len(purchases), end, cur)
        out["live"] = end == today
        return out

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
            econ_in = state.economics.get(client_id)
            if econ_in:
                e = engines.economics(econ_in)
                ctx["unit_economics"] = {k: e.get(k) for k in ("break_even_cpa", "target_cpa", "lifetime_break_even_cpa", "viable")} | \
                    {"note": "Max the business can pay per customer. Plan budgets, ad counts and stop rules around target_cpa; "
                             "with a small budget, use one ad set and at most 2 ads."}
            if bench:      # rebuilt from the table on every request, so opt-outs and deletions apply at once
                ctx["benchmarks_from_other_businesses"] = {
                    **bench, "note": "Rounded medians across several anonymous businesses. Never describe or guess any "
                                     "single business behind them, and never say how one business differs from them."}
        # the business's ads (live from Meta, cached 10 minutes) and, for ProAds itself, its own growth numbers
        try:
            rep = ads_report_for(client_id, "30")
            if rep.get("has_campaigns"):
                ctx["ads_last_30_days"] = {k: rep.get(k) for k in ("kind", "currency", "totals", "truth", "countries", "placements",
                                                                   "live", "error") if rep.get(k) is not None}
            else:
                ctx["ads_last_30_days"] = "no campaigns published through ProAds yet"
        except Exception as e:
            log.info("ads summary for the AI skipped: %s", e)
        with state.lock:
            ctx["campaigns"] = [{k: l.get(k) for k in ("name", "status", "conversion")} for l in state.launches.list(client_id)[:10]]
        if is_admin(client_id):
            d = proads_dashboard("30")
            ctx["your_business"] = {
                "what_it_is": PROADS_PRODUCT,
                "how_it_makes_money": "software subscriptions (Pro, Premium) plus 5% of clients' ad spend; it does not sell products, "
                                      "so there are no orders or shop sales. Judge it by sign-ups, companies that become active, "
                                      "paying clients and its own ads.",
                "last_30_days": {m["label"]: m["value"] for m in d["metrics"]},
                "previous_30_days": {m["label"]: m["previous"] for m in d["metrics"] if m["key"] != "paying"},
                "companies_total": d["accounts_total"],
            }
            try:
                a = acquisition_for()
                ctx["your_business"]["paying_subscribers_from_stripe"] = {
                    k: a[k] for k in ("new_subscribers", "first_payments", "renewals", "fees", "refunds",
                                      "service_cost_each", "service_cost_is_estimate")}
                ctx["your_business"]["customer_acquisition_profit_by_campaign"] = a["campaigns"][:10]
            except Exception as e:
                log.info("acquisition summary for the AI skipped: %s", e)
            ctx["note"] = ("This is the ProAds account itself. Empty sales and posts are expected: answer from your_business, "
                           "campaigns and ads_last_30_days.")
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

    # ---------------- media library: the client's own photos and videos for ads ----------------
    def media_http():
        if state.media_http is not None:
            return state.media_http
        import requests
        return requests

    def media_resolver():
        import socket
        return state.media_resolver or socket.getaddrinfo

    def media_view(it: dict) -> dict:
        labels = {"none": None, "enhanced": "Enhanced (sharpened and enlarged, nothing changed)",
                  "generated": "AI-generated: check it shows your product accurately"}
        return {**it, "label": labels.get(it.get("ai") or "none"), "needs_approval": not it.get("approved"),
                "usable": it["kind"] == "video" or it.get("quality") in ("good", "ok")}

    def store_image(client_id: str, raw: bytes, item: dict) -> Optional[int]:
        w, h = media.measure(raw)
        q = media.quality(w, h)
        if q == "tiny":
            return None
        jpg = media.to_jpeg(raw)
        with state.lock:
            return state.media.add(client_id, {**item, "kind": "image", "width": w, "height": h, "quality": q}, jpg)

    @flask.post("/media/<client_id>/scan")
    def media_scan(client_id):
        """Collect photos and videos from the client's own website."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        site = (body.get("url") or ((public_base() + "/") if is_admin(client_id) else
                                    (state.accounts.get(client_id) or {}).get("website") or
                                    state.config(client_id).get("website", ""))).strip()
        if not site:
            abort(400, "Add your website address first.")
        http, res = media_http(), media_resolver()
        try:
            found = media.discover(http, site, res)
        except media.MediaError as e:
            abort(400, str(e))
        added = skipped = 0
        for c in found:
            if c["kind"] == "video":
                with state.lock:
                    if state.media.add(client_id, {"source": "website", "origin": c["origin"], "url": c["url"], "kind": "video",
                                                   "title": c["title"], "poster": c["poster"], "quality": "video"}, None):
                        added += 1
                continue
            try:
                raw, ctype, _ = media.safe_get(http, c["url"], media.MAX_IMAGE, res)
                if store_image(client_id, raw, {"source": "website", "origin": c["origin"], "url": c["url"],
                                                "title": c["title"], "price": c["price"]}):
                    added += 1
                else:
                    skipped += 1
            except media.MediaError:
                skipped += 1
        with state.lock:
            items = [media_view(i) for i in state.media.list(client_id)]
        return {"added": added, "skipped": skipped, "items": items}

    @flask.get("/media/<client_id>")
    def media_list(client_id):
        state.require_key(client_id)
        with state.lock:
            items = [media_view(i) for i in state.media.list(client_id)]
        return {"items": items}

    @flask.get("/media/<client_id>/<int:item_id>/image")
    def media_image(client_id, item_id):
        state.require_key(client_id)
        with state.lock:
            blob = state.media.blob(client_id, item_id)
        if not blob:
            abort(404, "No picture stored for this item.")
        from flask import Response
        return Response(blob, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @flask.post("/media/<client_id>/upload")
    def media_upload(client_id):
        state.require_key(client_id)
        raw = request.get_data()
        if len(raw) > media.MAX_IMAGE:
            abort(400, "That picture is larger than 12 MB.")
        try:
            iid = store_image(client_id, raw, {"source": "upload", "origin": "your upload",
                                               "url": f"upload:{secrets.token_hex(8)}", "title": request.args.get("title", "")[:120]})
        except media.MediaError as e:
            abort(400, str(e))
        if not iid:
            abort(400, "That picture is too small to use in an ad.")
        with state.lock:
            return media_view(state.media.get(client_id, iid))

    @flask.post("/media/<client_id>/upload-video")
    def media_upload_video(client_id):
        """A video file (MP4 or MOV) for video ads. Sent straight to Meta when an ad uses it."""
        state.require_key(client_id)
        raw = request.get_data()
        if len(raw) > VIDEO_MAX:
            abort(400, "That video is larger than 120 MB. Export it smaller (1080p is plenty for Instagram).")
        if raw[4:8] != b"ftyp":
            abort(400, "That isn't an MP4 or MOV video. Export it as MP4 and try again.")
        with state.lock:
            iid = state.media.add(client_id, {"source": "upload", "origin": "your upload", "kind": "video", "quality": "video",
                                              "url": f"upload-video:{secrets.token_hex(8)}",
                                              "title": request.args.get("title", "")[:120]}, raw)
            return media_view(state.media.get(client_id, iid))

    @flask.get("/media/<client_id>/<int:item_id>/video")
    def media_video(client_id, item_id):
        state.require_key(client_id)
        with state.lock:
            it, blob = state.media.get(client_id, item_id), state.media.blob(client_id, item_id)
        if not it or it["kind"] != "video" or not blob:
            abort(404, "No video stored for this item.")
        from flask import Response
        return Response(blob, mimetype="video/mp4", headers={"Cache-Control": "private, max-age=86400"})

    @flask.post("/media/<client_id>/<int:item_id>/enhance")
    def media_enhance(client_id, item_id):
        """Bigger and sharper with ordinary image processing. Nothing is invented, so no approval is needed."""
        state.require_key(client_id)
        with state.lock:
            it, blob = state.media.get(client_id, item_id), state.media.blob(client_id, item_id)
        if not it or not blob:
            abort(404, "Picture not found.")
        out = media.enhance(blob)
        w, h = media.measure(out)
        with state.lock:
            nid = state.media.add(client_id, {"source": "enhanced", "origin": it["origin"], "url": f"enhanced:{item_id}",
                                              "kind": "image", "width": w, "height": h, "quality": media.quality(w, h),
                                              "title": it["title"], "price": it["price"], "ai": "enhanced",
                                              "parent_id": item_id}, out)
            if not nid:
                nid = state.store.db.execute("SELECT id FROM media_items WHERE client_id=? AND url=?",
                                             (client_id, f"enhanced:{item_id}")).fetchone()[0]
            return media_view(state.media.get(client_id, nid))

    @flask.post("/media/<client_id>/<int:item_id>/similar")
    def media_similar(client_id, item_id):
        """An AI image made to look like the original. It stays unusable until the owner confirms it's accurate."""
        state.require_key(client_id)
        if not state.ai.available:
            abort(503, "AI image generation isn't set up (MODEL_API_KEY).")
        with state.lock:
            it, blob = state.media.get(client_id, item_id), state.media.blob(client_id, item_id)
        if not it or not blob:
            abort(404, "Picture not found.")
        try:
            desc = state.ai.describe_image(media.to_jpeg(blob, 1024), it.get("title") or "")
            prompt = ("Professional advertising photo, sharp, well lit, high resolution. Recreate faithfully, same subject, "
                      "same colours and details, no extra text or logos: " + desc)
            img = state.ai.generate_image(prompt)
        except PermissionError as e:
            abort(402 if "billing" in str(e).lower() else 503, str(e))
        except Exception as e:
            log.warning("similar image for %s/%s failed: %s", client_id, item_id, e)
            abort(502, "The AI couldn't make a similar image right now. Try enhancing the photo instead.")
        try:
            w, h = media.measure(img)
        except media.MediaError:
            abort(502, "The AI returned something that isn't a picture.")
        with state.lock:
            nid = state.media.add(client_id, {"source": "generated", "origin": it["origin"],
                                              "url": f"generated:{item_id}:{secrets.token_hex(4)}", "kind": "image",
                                              "width": w, "height": h, "quality": media.quality(w, h), "title": it["title"],
                                              "price": it["price"], "ai": "generated", "parent_id": item_id,
                                              "approved": 0, "prompt": prompt[:4000]}, media.to_jpeg(img))
            return media_view(state.media.get(client_id, nid))

    @flask.post("/media/<client_id>/<int:item_id>/approve")
    def media_approve(client_id, item_id):
        state.require_key(client_id)
        if (request.get_json(silent=True) or {}).get("accurate") is not True:
            abort(400, "Confirm the picture shows your product or service accurately.")
        with state.lock:
            if not state.media.get(client_id, item_id):
                abort(404, "Picture not found.")
            state.media.approve(client_id, item_id)
            return media_view(state.media.get(client_id, item_id))

    @flask.post("/media/<client_id>/<int:item_id>/hide")
    def media_hide(client_id, item_id):
        state.require_key(client_id)
        with state.lock:
            state.media.hide(client_id, item_id)
        return {"ok": True}

    @flask.post("/media/<client_id>/social")
    def media_social(client_id):
        """Photos and videos from the client's own Facebook Page and Instagram posts."""
        state.require_key(client_id)
        s = state.ads_settings(client_id, is_admin(client_id))
        if not s:
            abort(400, "Connect Facebook & Instagram first.")
        pub = meta_ads.Publisher(state.ads_http, s)
        added, problems = 0, []
        http, res = media_http(), media_resolver()
        try:
            page = pub._get(s["page_id"], "access_token", "reading your Page")
            ptok = page.get("access_token") or s["token"]
            r = state.ads_http.get(f"https://graph.facebook.com/{s.get('version', 'v25.0')}/{s['page_id']}/posts",
                                   params={"fields": "id,message,full_picture,attachments{media_type,media,url}",
                                           "limit": 40, "access_token": ptok}, timeout=30).json()
            if r.get("error"):
                raise meta_ads.PublishError(r["error"].get("message", "Meta refused"))
            for post in r.get("data") or []:
                att = ((post.get("attachments") or {}).get("data") or [{}])[0]
                if (att.get("media_type") or "").lower() == "video" and (att.get("media") or {}).get("source"):
                    with state.lock:
                        if state.media.add(client_id, {"source": "facebook", "origin": "Facebook post", "kind": "video",
                                                       "url": att["media"]["source"], "title": (post.get("message") or "")[:120],
                                                       "poster": post.get("full_picture") or "", "quality": "video"}, None):
                            added += 1
                elif post.get("full_picture"):
                    try:
                        raw, _, _ = media.safe_get(http, post["full_picture"], media.MAX_IMAGE, res)
                        if store_image(client_id, raw, {"source": "facebook", "origin": "Facebook post",
                                                        "url": post["full_picture"], "title": (post.get("message") or "")[:120]}):
                            added += 1
                    except media.MediaError:
                        pass
        except meta_ads.PublishError as e:
            problems.append("Facebook Page: " + str(e)[:200])
        if s.get("instagram_id"):
            try:
                r = state.ads_http.get(f"https://graph.facebook.com/{s.get('version', 'v25.0')}/{s['instagram_id']}/media",
                                       params={"fields": "id,media_type,media_url,thumbnail_url,caption", "limit": 40,
                                               "access_token": s["token"]}, timeout=30).json()
                if r.get("error"):
                    raise meta_ads.PublishError(r["error"].get("message", "Meta refused"))
                for m in r.get("data") or []:
                    if m.get("media_type") == "VIDEO" and m.get("media_url"):
                        with state.lock:
                            if state.media.add(client_id, {"source": "instagram", "origin": "Instagram post", "kind": "video",
                                                           "url": m["media_url"], "title": (m.get("caption") or "")[:120],
                                                           "poster": m.get("thumbnail_url") or "", "quality": "video"}, None):
                                added += 1
                    elif m.get("media_url"):
                        try:
                            raw, _, _ = media.safe_get(http, m["media_url"], media.MAX_IMAGE, res)
                            if store_image(client_id, raw, {"source": "instagram", "origin": "Instagram post",
                                                            "url": m["media_url"], "title": (m.get("caption") or "")[:120]}):
                                added += 1
                        except media.MediaError:
                            pass
            except meta_ads.PublishError as e:
                problems.append("Instagram: " + str(e)[:200])
        if problems and not added:
            abort(403, "Meta didn't share your posts yet. " + " ".join(problems) +
                  " This needs Meta's approval of ProAds (in progress); your website photos work meanwhile.")
        return {"added": added, "problems": problems}

    @flask.post("/media/<client_id>/suggest")
    def media_suggest(client_id):
        """The best media for each ad in a plan: {"<set>-<ad>": item}."""
        state.require_key(client_id)
        plan = (request.get_json(silent=True) or {}).get("plan") or {}
        with state.lock:
            items = state.media.list(client_id)
        out, used = {}, set()
        for i, s in enumerate([x for x in plan.get("ad_sets") or [] if x.get("ads")]):
            for j, ad in enumerate(s["ads"]):
                text = " ".join(str(ad.get(k) or "") for k in ("headline", "primary_text", "offer", "hook"))
                ranked = [x for x in media.rank(items, text) if x["approved"]]
                pick = next((x for x in ranked if x["id"] not in used), ranked[0] if ranked else None)
                if pick:
                    used.add(pick["id"])
                    out[f"{i}-{j}"] = media_view(pick)
        return {"suggestions": out}

    def resolve_media(client_id: str, images: dict) -> dict:
        """'media:<id>' -> the stored picture (or a video to hand to Meta). AI images must be approved first."""
        out = {}
        for k, v in images.items():
            if isinstance(v, str) and v.startswith("media:"):
                try:
                    iid = int(v.split(":", 1)[1])
                except ValueError:
                    abort(400, "Unknown media item.")
                with state.lock:
                    it, blob = state.media.get(client_id, iid), state.media.blob(client_id, iid)
                if not it:
                    abort(400, "A chosen picture no longer exists. Pick another.")
                if not it["approved"]:
                    abort(400, "An AI-generated picture hasn't been confirmed as accurate yet. Confirm it or pick another.")
                if it["kind"] == "video" and blob:                 # an uploaded file: Meta gets the bytes directly
                    out[k] = {"video_bytes": blob, "name": it.get("title") or "video"}
                    continue
                if it["kind"] == "video":
                    thumb = None
                    if it.get("poster"):
                        try:
                            raw, _, _ = media.safe_get(media_http(), it["poster"], media.MAX_IMAGE, media_resolver())
                            thumb = base64.b64encode(media.to_jpeg(raw)).decode()
                        except media.MediaError:
                            thumb = None
                    out[k] = {"video_url": it["url"], "thumb": thumb}
                else:
                    if not blob:
                        abort(400, "A chosen picture is missing. Pick another.")
                    out[k] = base64.b64encode(blob).decode()
            else:
                out[k] = v
        return out

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
        images = resolve_media(client_id, images)
        mode = body.get("placements") or "automatic"
        if mode not in meta_ads.PLACEMENTS:
            abort(400, "Unknown placement choice.")
        plan = {**plan, "placement_mode": mode}
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

    launch_checked: dict = {}

    def sync_launch_status(client_id: str, force: bool = False) -> dict:
        """Make ProAds show what Meta really shows: a campaign switched off, deleted or with no running ad in
        Ads Manager stops being called "Live" here. Checked at most every 2 minutes per client."""
        if not force and time.time() - launch_checked.get(client_id, 0) < 120:
            return {}
        launch_checked[client_id] = time.time()
        with state.lock:
            launches = state.launches.list(client_id)
        s = state.ads_settings(client_id, is_admin(client_id))
        if not launches or not s:
            return {}
        try:
            real = meta_ads.Publisher(state.ads_http, s).live_states([l["campaign_id"] for l in launches])
        except Exception as e:                         # Meta unreachable: keep showing the last known state
            log.warning("campaign status check for %s failed: %s", client_id, e)
            return {}
        changed = {}
        with state.lock:
            for l in launches:
                now = real.get(l["campaign_id"])
                if not now or now == l["status"]:
                    continue
                if l["status"] == "PAUSED_BILLING" and now in ("PAUSED", "NOT_RUNNING"):
                    continue                           # ProAds paused it for an unpaid plan: keep that reason
                state.launches.set_status(client_id, l["campaign_id"], now)
                changed[l["campaign_id"]] = now
        if changed:
            log.info("campaign status for %s updated from Meta: %s", client_id, changed)
        return changed
    state.sync_launch_status = sync_launch_status

    def sync_all_launch_status() -> None:
        for cid in state.launches.clients():
            try:
                sync_launch_status(cid, force=True)
            except Exception:
                log.exception("campaign status check failed for %s", cid)
    state.sync_all_launch_status = sync_all_launch_status

    @flask.get("/campaign/<client_id>/launches")
    def campaign_launches(client_id):
        state.require_key(client_id)
        sync_launch_status(client_id)
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

    @flask.get("/fees")
    def fees_page():
        return page("fees.html", referrer="strict-origin-when-cross-origin")

    @flask.get("/privacy")
    def privacy():
        return page("privacy.html", referrer="strict-origin-when-cross-origin")

    @flask.get("/public-config")
    def public_config():
        """Values the public pages need: the Pixel ID (public by nature) and contact email."""
        # Cloudflare (in front of the site) says which country the visitor is in: the page picks its language from it
        country = (request.headers.get("CF-IPCountry") or "").strip().upper()
        return {"pixel_id": growth.pixel_id(), "tiktok_pixel": tiktok.proads_settings()["pixel"], "contact": os.environ.get("PROADS_CONTACT_EMAIL", ""),
                "sign_in": social.configured(),
                "country": country if len(country) == 2 and country.isalpha() and country not in ("XX", "T1") else ""}

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
        # which campaign/ad brought them: from the tags kept on the first visit, or the sign-up page's own address
        from urllib.parse import parse_qsl, urlparse
        raw_utm = body.get("utm") if isinstance(body.get("utm"), dict) else {}
        from_url = dict(parse_qsl(urlparse(str(body.get("source_url") or "")).query))
        utm = {k: str(raw_utm.get(k) or from_url.get(k) or "")[:120] for k in
               ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")}
        utm = {k: v for k, v in utm.items() if v}
        with state.lock:
            cid = state.accounts.create(clean, set(state.clients), json.dumps(ctx) if ctx else "")
            if utm:
                state.accounts.set_attribution(cid, utm)
            token = state.accounts.new_session(cid)
        state.signup_limiter.fail(ip_key)              # counts sign-ups per connection
        event_id = f"signup:{cid}"
        if ctx:
            state.emit_growth(growth.build_event("CompleteRegistration", state.accounts.get(cid), ctx, event_id))
        log.info("new account %s", cid)
        return {"client_id": cid, "token": token, "event_id": event_id, "name": clean["name"]}

    # ---------------- sign in with Google, Apple or Facebook ----------------
    BIND_COOKIE = "proads_oauth"

    def oauth_redirect_uri(provider: str) -> str:
        return f"{public_base()}/auth/oauth/{provider}/callback"

    def oauth_fail(msg: str):
        from urllib.parse import quote
        return redirect("/app#/signin?oauth_error=" + quote(msg))

    @flask.get("/auth/oauth/<provider>/start")
    def oauth_start(provider):
        if provider not in social.configured():
            abort(404, "This sign-in option isn't set up.")
        a = request.args
        intent = "signup" if a.get("intent") == "signup" else "signin"
        ads = a.get("ads") == "1"
        ctx = growth.capture_context({"ads_consent": ads, "fbp": a.get("fbp", ""), "fbc": a.get("fbc", ""),
                                      "fbclid": a.get("fbclid", ""), "ttclid": a.get("ttclid", ""), "ttp": a.get("ttp", ""),
                                      "source_url": a.get("source_url", "")},
                                     request.headers, request.remote_addr) if ads else None
        try:
            raw = json.loads(a.get("utm") or "{}")
        except ValueError:
            raw = {}
        utm = {k: str(raw.get(k) or "")[:120] for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
               if isinstance(raw, dict) and raw.get(k)}
        with state.lock:
            st = state.social.new_state(provider, intent, {"ctx": ctx, "utm": utm})
        resp = redirect(social.auth_url(provider, oauth_redirect_uri(provider), st["state"], st["nonce"], st["verifier"]))
        secure = request.is_secure or request.headers.get("X-Forwarded-Proto", "") == "https"
        # SameSite=None so Apple's cross-site form post still carries it; HttpOnly, only sent to /auth/oauth
        resp.set_cookie(BIND_COOKIE, st["bind"], max_age=social.STATE_TTL, httponly=True, secure=secure,
                        samesite="None" if secure else "Lax", path="/auth/oauth")
        return resp

    @flask.route("/auth/oauth/<provider>/callback", methods=["GET", "POST"])
    def oauth_callback(provider):
        src = request.form if request.method == "POST" else request.args
        if src.get("error"):
            return oauth_fail("Sign-in was cancelled." if src.get("error") in ("access_denied", "user_cancelled_authorize")
                              else "The sign-in provider returned an error. Please try again.")
        with state.lock:
            st = state.social.take_state(src.get("state", ""), provider, request.cookies.get(BIND_COOKIE))
        if not st:
            return oauth_fail("That sign-in link expired or was opened in another browser. Please try again.")
        try:
            http = state.social_http
            if http is None:
                import requests as http
            who = social.identity(provider, http, src.get("code", ""), oauth_redirect_uri(provider),
                                  st["nonce"], st["verifier"], apple_user=src.get("user"))
        except social.SocialError as e:
            return oauth_fail(str(e))
        except Exception as e:
            log.warning("%s sign-in failed: %s", provider, e)
            return oauth_fail("Couldn't reach the sign-in provider. Please try again.")
        if not who["subject"]:
            return oauth_fail("The sign-in provider didn't identify you. Please try again.")
        with state.lock:
            cid = state.social.client_for(provider, who["subject"])
            if cid and not state.accounts.get(cid):
                cid = None                            # the account was deleted
            if not cid and who["email"]:
                acct = state.accounts.by_email(who["email"])
                if acct and who["email_verified"]:
                    cid = acct["client_id"]
                    state.social.link(provider, who["subject"], cid, who["email"])
                    log.info("linked %s sign-in to %s", provider, cid)
                elif acct:
                    return oauth_fail("An account with this email exists, but the provider hasn't verified the email. "
                                      "Sign in with your password instead.")
            if cid:
                code = state.social.new_code("session", {"client_id": cid}, social.CODE_TTL)
        if cid:
            resp = redirect("/app#/home?login_code=" + code)
        else:
            if not who["email"] or not who["email_verified"]:
                return oauth_fail("We need a verified email address to create your account. Try another option or "
                                  "sign up with your email.")
            with state.lock:
                code = state.social.new_code("pending", {"who": who, "ctx": st["extra"].get("ctx"),
                                                         "utm": st["extra"].get("utm") or {}}, social.PENDING_TTL)
            resp = redirect("/?finish=" + code + "#signup")
        resp.delete_cookie(BIND_COOKIE, path="/auth/oauth")
        return resp

    @flask.post("/auth/oauth/exchange")
    def oauth_exchange():
        """The app swaps its one-time code for a session (never a session token in a URL)."""
        code = str((request.get_json(silent=True) or {}).get("code") or "")
        with state.lock:
            got = state.social.take_code(code, "session")
            if not got:
                abort(401, "That sign-in expired. Please sign in again.")
            token = state.accounts.new_session(got["client_id"])
        return {"client_id": got["client_id"], "token": token}

    @flask.get("/auth/oauth/pending")
    def oauth_pending():
        with state.lock:
            got = state.social.peek_code(request.args.get("code", ""), "pending")
        if not got:
            abort(404, "This sign-up link expired. Please start again.")
        return {"email": got["who"]["email"], "name": got["who"]["name"], "provider": got["who"]["provider"]}

    @flask.post("/auth/oauth/finish")
    def oauth_finish():
        """New account from a Google/Apple/Facebook sign-in: business details and terms, then the account."""
        body = request.get_json(silent=True) or {}
        ip_key = f"signup:{client_ip()}"
        if state.signup_limiter.locked_for(ip_key):
            abort(429, "Too many sign-ups from this connection. Please try again later.")
        with state.lock:
            got = state.social.peek_code(str(body.get("code") or ""), "pending")
        if not got:
            abort(404, "This sign-up link expired. Please start again.")
        who = got["who"]
        clean, errors = validate_signup({**body, "email": who["email"], "password": secrets.token_urlsafe(24)})
        if errors:
            return jsonify({"errors": errors}), 400
        ctx = got.get("ctx")
        with state.lock:
            if not state.social.take_code(str(body.get("code") or ""), "pending"):
                abort(404, "This sign-up link expired. Please start again.")
            existing = state.accounts.by_email(who["email"])
            if existing:                              # created in another tab meanwhile: link instead of duplicating
                cid = existing["client_id"]
                state.social.link(who["provider"], who["subject"], cid, who["email"])
                token = state.accounts.new_session(cid)
                return {"client_id": cid, "token": token, "event_id": None, "name": existing["name"]}
            cid = state.accounts.create(clean, set(state.clients), json.dumps(ctx) if ctx else "")
            state.accounts.mark_no_password(cid)
            state.social.link(who["provider"], who["subject"], cid, who["email"])
            if got.get("utm"):
                state.accounts.set_attribution(cid, got["utm"])
            token = state.accounts.new_session(cid)
        state.signup_limiter.fail(ip_key)
        event_id = f"signup:{cid}"
        if ctx:
            state.emit_growth(growth.build_event("CompleteRegistration", state.accounts.get(cid), ctx, event_id))
        log.info("new account %s via %s", cid, who["provider"])
        return {"client_id": cid, "token": token, "event_id": event_id if ctx else None, "name": clean["name"],
                "ads": bool(ctx)}

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
        if state.accounts.has_password(client_id):
            if not check_password(body.get("password") or "", acct["pw_hash"]):
                state.login_limiter.fail(lock_key)
                abort(401, "Wrong password")
        elif (body.get("confirm_email") or "").strip().lower() != acct["email"]:
            state.login_limiter.fail(lock_key)       # signed up with Google/Apple/Facebook: type the email to confirm
            abort(401, "Type your account's email address to confirm")
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
        ig_s = state.ads_settings(client_id, is_admin(client_id)) or {}
        if shop:
            shop_info.update({k: shop[k] for k in ("shop", "connected_at", "sync_status", "sync_message",
                                                   "last_sync", "imported")})
            shop_info["all_orders"] = "read_all_orders" in (shop["scope"] or "")
        return {
            "client_id": client_id,
            "name": cfg.get("name", client_id),
            "self_serve": bool(cfg.get("self_serve")),
            "has_password": state.accounts.has_password(client_id) if state.accounts.get(client_id) else True,
            "instagram_ready": bool(ig_s.get("instagram_id")),
            "instagram_why": "" if ig_s.get("instagram_id") or not ig_s else
                             state.instagram_lookup(client_id, ig_s).get("why", ""),
            "admin": is_admin(client_id),
            "currency": cfg.get("currency", "AED"),
            "website": cfg.get("website", ""),
            "connections": {
                "shopify": {"configured": bool(cfg.get("shopify_webhook_secret") or shop),
                            **payments.get("shopify", {})},
                "shopify_app": shop_info,
                "tiktok": tiktok.TikTokStore.public(state.tiktok.get(client_id)),
                "stripe": {"configured": bool(cfg.get("stripe_webhook_secret")) or bool(state.stripe_links.get(client_id)),
                           **payments.get("stripe", {})},
                "stripe_api": stripe_sync.StripeLinks.public(state.stripe_links.get(client_id)),
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
               "fee_percent": billing.fee_percent(), "spend_so_far": None}
        if b.get("status") in billing.ACTIVE and b.get("period_start") and billing.fee_percent() > 0:
            try:
                spend, cur = spend_so_far(client_id, int(b["period_start"]))
                out["spend_so_far"] = {"spend": spend, "currency": cur, "estimate": True,
                                       "fee": billing.fee_amount(spend, billing.fee_percent()) / 100, "fee_currency": cur}
            except Exception as e:
                log.info("could not read ad spend for %s: %s", client_id, e)
        with state.lock:
            out["fee_periods"] = [{k: p[k] for k in ("id", "kind", "parent_id", "status", "completeness", "reason",
                                                     "period_start", "period_end", "invoice_id", "invoice_currency")}
                                  | {"fee": (p["fee_minor"] or 0) / 100 if p["fee_minor"] is not None else None,
                                     "spend": (p["detail"] or {}).get("spend_in_invoice_currency")}
                                  for p in state.fees.for_client(client_id)]
        out["settle_hours"] = settlement.SETTLE_HOURS
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

    @flask.get("/billing/<client_id>/fees/<int:period_id>")
    def billing_fee_detail(client_id, period_id):
        """How one fee was worked out: every campaign, ad account, time zone, day, exchange rate and the formula."""
        state.require_key(client_id)
        with state.lock:
            p = state.fees.get(period_id)
        if not p or p["client_id"] != client_id:
            abort(404, "no such fee")
        return {k: p[k] for k in ("id", "kind", "parent_id", "status", "completeness", "reason", "period_start",
                                  "period_end", "invoice_id", "invoice_currency", "detail")} | \
            {"fee": (p["fee_minor"] or 0) / 100, "reference": f"FP-{p['id']}",
             "description": settlement.describe(p) if p["detail"] else None,
             "note": settlement.INCOMPLETE_NOTE if p["completeness"] == "incomplete" else None,
             "final_after": (p["billed_at"] + settlement.RECHECK_AFTER_DAYS * 86400) if p.get("billed_at") else None}

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
        with state.lock:
            state.funnel.mark(client_id, "checkout", int(time.time()))
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
        if cid and event.get("type") == "checkout.session.completed":
            with state.lock:
                state.funnel.mark(cid, "plan", int(time.time()))
        if cid:
            log.info("billing %s for %s", event.get("type"), cid)
            if state.billing_sync:
                enforce_ads_trials()
            else:
                threading.Thread(target=enforce_ads_trials, daemon=True, name="ads-access").start()
        obj = (event.get("data") or {}).get("object") or {}
        if event.get("type") == "invoice.created" and obj.get("status") == "draft" \
                and obj.get("billing_reason") == "subscription_cycle":
            if state.billing_sync:
                on_invoice_created(obj)
            else:
                threading.Thread(target=on_invoice_created, args=(obj,), daemon=True, name="spend-fee").start()
        if event.get("type") == "customer.subscription.deleted" and cid:
            # the last, partial period: settled and billed on its own invoice like any other
            start = billing._period_start(obj)
            end = int(obj.get("ended_at") or obj.get("canceled_at") or time.time())
            with state.lock:
                state.fees.open(cid, obj.get("customer") or "", (obj.get("currency") or "").upper(), start, end)
        try:
            if event.get("type") == "invoice.paid":
                record_proads_invoice(obj)
            elif event.get("type") == "charge.refunded":
                record_proads_refund(obj)
            elif event.get("type") == "charge.dispute.closed" and obj.get("status") == "lost":
                record_proads_chargeback(obj)
        except Exception as e:                       # never fail the webhook (Stripe would retry forever)
            log.warning("ProAds revenue record for %s failed: %s", event.get("type"), e)
        return {"received": True}

    # ---------- ProAds' own revenue: paid invoices become ProAds' purchase data ----------
    REV_SOURCE = "proads_billing"

    def proads_admin() -> str:
        return next(iter(sorted(_admins(state))), "pro")

    def record_proads_invoice(inv: dict) -> Optional[dict]:
        """One paid Stripe invoice. First paid subscription invoice = a new customer (sent to Meta as a Purchase,
        with the collected value); renewals and ad-spend fees are revenue only. Idempotent by invoice ID."""
        inv_id = inv.get("id")
        amount_paid = int(inv.get("amount_paid") or 0)
        if not inv_id or amount_paid <= 0 or inv.get("status") not in (None, "paid"):
            return None
        with state.lock:
            sub_details = inv.get("subscription_details") or ((inv.get("parent") or {}).get("subscription_details")) or {}
            cid = state.billing.by_customer(inv.get("customer") or "") or (sub_details.get("metadata") or {}).get("client_id")
            if not cid or is_admin(cid) or not state.known(cid) or state.revenue.get(inv_id):
                return None
            currency = (inv.get("currency") or "").upper()
            net_minor = max(0, amount_paid - proads_revenue.invoice_tax(inv))
            meta = inv.get("metadata") or {}
            if meta.get("kind") == "ad_spend_fee":
                kind = "fee"
            elif inv.get("billing_reason") in ("subscription_create", "subscription_cycle", "subscription_update",
                                               "subscription") or inv.get("subscription") or sub_details.get("subscription"):
                kind = "renewal" if state.revenue.has_first(cid) else "first"
            else:
                kind = "fee"
            acct = state.accounts.get(cid) or {}
            utm = state.accounts.attribution(cid) or {}
            ctx = growth.load_ctx(acct.get("ads_ctx")) or {}
            charge, pi = proads_revenue.invoice_payment_refs(inv)
            paid_at = int(((inv.get("status_transitions") or {}).get("paid_at")) or time.time())
            row = {"invoice_id": inv_id, "client_id": cid, "kind": kind, "tier": state.billing.tier(cid),
                   "amount": round(proads_revenue.major(net_minor, currency), 2),
                   "tax": round(proads_revenue.major(amount_paid - net_minor, currency), 2),
                   "currency": currency, "paid_at": paid_at, "charge_id": charge, "payment_intent": pi, "utm": utm}
            if not state.revenue.add(row):
                return None
            admin = proads_admin()
            fbclid = None
            if ctx.get("fbc") and ctx["fbc"].count(".") >= 3:
                fbclid = ctx["fbc"].split(".", 3)[3]
            p = Payment(client_id=admin, source=REV_SOURCE, external_id=inv_id, kind="purchase", amount=row["amount"],
                        currency=currency, occurred_at=datetime.fromtimestamp(paid_at, timezone.utc),
                        email=acct.get("email"), phone=acct.get("phone"), customer_id=cid, fbclid=fbclid,
                        items=[LineItem(sku=f"proads-{kind}", name=f"ProAds {row['tier'].title()} ({kind})", quantity=1,
                                        unit_price=row["amount"])],
                        utm_source=utm.get("utm_source") or None, utm_medium=utm.get("utm_medium") or None,
                        utm_campaign=utm.get("utm_campaign") or None, utm_content=utm.get("utm_content") or None,
                        client_user_agent=ctx.get("ua"), client_ip_address=ctx.get("ip"),
                        event_source_url=ctx.get("url"), fbp=ctx.get("fbp"), fbc=ctx.get("fbc"))
            state.store.add(p)
            # ProAds' own Pixel gets these directly below; the admin's general pipeline must never send them again
            state.store.mark_sent(admin, REV_SOURCE, inv_id)
        if kind == "first" and ctx:                  # only with the visitor's ads consent, like sign-up events
            ev = growth.build_event("Purchase", acct, ctx, f"proads:first:{inv_id}", value=row["amount"], currency=currency,
                                    custom={"content_name": f"ProAds {row['tier'].title()}", "proads_source": "stripe_first_payment"},
                                    event_time=paid_at if paid_at > time.time() - 6 * 86400 else None)
            state.emit_growth(ev)
            with state.lock:
                state.store.db.execute("UPDATE proads_revenue SET sent_to_meta=1 WHERE invoice_id=?", (inv_id,))
                state.store.db.commit()
        log.info("ProAds revenue: %s %s %s from %s (%s)", kind, row["amount"], currency, cid, inv_id)
        return row

    def _net_share(rec: dict, gross_minor: int, currency: str) -> float:
        """A refund in Stripe includes tax; take out the same tax share the invoice had."""
        gross = proads_revenue.major(gross_minor, currency)
        total = rec["amount"] + rec["tax"]
        return round(gross * (rec["amount"] / total) if total else gross, 2)

    def _record_reversal(rec: dict, ext_id: str, amount: float, why: str) -> None:
        admin = proads_admin()
        acct = state.accounts.get(rec["client_id"]) or {}
        p = Payment(client_id=admin, source=REV_SOURCE, external_id=ext_id, kind="refund", amount=amount,
                    currency=rec["currency"], occurred_at=datetime.now(timezone.utc), email=acct.get("email"),
                    phone=acct.get("phone"), customer_id=rec["client_id"], refund_of=rec["invoice_id"])
        if state.store.add(p):
            state.store.mark_sent(admin, REV_SOURCE, ext_id)
        if rec["kind"] == "first":
            ctx = growth.load_ctx(acct.get("ads_ctx"))
            if ctx:
                state.emit_growth(growth.build_event("Refund", acct, ctx, f"proads:{ext_id}", value=amount,
                                                     currency=rec["currency"], custom={"proads_source": why}))

    def record_proads_refund(charge: dict) -> Optional[dict]:
        """charge.refunded carries the running total refunded: record only the increase, once."""
        inv = charge.get("invoice") if isinstance(charge.get("invoice"), str) else None
        with state.lock:
            rec = state.revenue.find_payment(charge.get("id"), charge.get("payment_intent"), inv)
            if not rec:
                return None
            total = _net_share(rec, int(charge.get("amount_refunded") or 0), rec["currency"])
            total = min(total, rec["amount"])
            delta = round(total - rec["refunded"], 2)
            if delta <= 0:
                return None
            state.revenue.set_refunded(rec["invoice_id"], total)
            _record_reversal(rec, f"refund:{charge.get('id')}:{int(charge.get('amount_refunded') or 0)}", delta, "stripe_refund")
        log.info("ProAds refund %s on %s", delta, rec["invoice_id"])
        return {**rec, "refunded": total}

    def record_proads_chargeback(dispute: dict) -> Optional[dict]:
        with state.lock:
            ext = f"chargeback:{dispute.get('id')}"
            if state.store.db.execute("SELECT 1 FROM payments WHERE source=? AND external_id=?", (REV_SOURCE, ext)).fetchone():
                return None
            rec = state.revenue.find_payment(dispute.get("charge"), dispute.get("payment_intent"), None)
            if not rec:
                return None
            amount = min(_net_share(rec, int(dispute.get("amount") or 0), rec["currency"]),
                         round(rec["amount"] - rec["refunded"] - rec["chargeback"], 2))
            if amount <= 0:
                return None
            state.revenue.add_chargeback(rec["invoice_id"], amount)
            _record_reversal(rec, ext, amount, "stripe_chargeback")
        log.info("ProAds chargeback %s on %s", amount, rec["invoice_id"])
        return rec
    state.record_proads_invoice = record_proads_invoice

    def acquisition_for() -> dict:
        """Customer Acquisition Profit for ProAds itself, per campaign that brought paying subscribers."""
        admin = proads_admin()
        econ_in = None
        with state.lock:
            rows = state.revenue.all()
            launches = state.launches.list(admin)
            econ_in = state.economics.get(admin)
        slug_to = {}
        for l in launches:
            slug_to.setdefault(l.get("utm_slug") or meta_ads._slug(l.get("name", ""), 30), []).append(l)
        spend, names, raw = {}, {}, {}
        today = datetime.now(timezone.utc).date().isoformat()
        rev_cur = (rows[0]["currency"] if rows else "AED") or "AED"
        with state.lock:
            for slug, ls in slug_to.items():
                try:
                    raw[slug] = state.ledger.total([l["campaign_id"] for l in ls], "2000-01-01", "2100-01-01")
                except ValueError:
                    continue
                names[slug] = ls[0].get("name") or slug
        for slug, (amt, cur) in raw.items():         # e.g. THB ad spend vs AED subscriptions: compare in one currency
            try:
                spend[slug] = round(amt * billing.fx_rate(cur or rev_cur, rev_cur), 2)
            except Exception as e:
                log.info("no exchange rate %s->%s for acquisition cost: %s", cur, rev_cur, e)
        env_cost = os.environ.get("PROADS_SERVICE_COST_PER_CLIENT", "").strip()
        service, is_estimate, cost_source = None, True, "not set"
        if env_cost:
            try:
                service = float(env_cost)
                is_estimate = os.environ.get("PROADS_SERVICE_COST_IS_ACTUAL", "") not in ("1", "true", "yes")
                cost_source = "server setting"
            except ValueError:
                pass
        if service is None and econ_in:
            service = round(float(econ_in.get("product_cost", 0)) + float(econ_in.get("other_costs", 0)) +
                            float(econ_in.get("delivery_cost", 0)), 2)
            cost_source = "Profit engine inputs"
        out = proads_revenue.acquisition_profit(rows, spend, names, service, is_estimate)
        out["service_cost_source"] = cost_source
        out["spend_converted_from"] = sorted({c for _, c in raw.values() if c and c != rev_cur})
        out["note"] = ("Acquisition contribution = first payment collected (after tax, discounts, refunds and lost chargebacks) "
                       "- campaign spend per new subscriber - service cost per subscriber. "
                       + ("Service cost is an ESTIMATE until actual costs are set." if is_estimate else "Service cost is actual."))
        return out

    # ---------- Purchase Funnel Optimiser ----------
    META_SOURCES = ("meta", "facebook", "instagram", "fb", "ig")

    def from_meta_ads(u: dict) -> bool:
        return (u.get("utm_medium") or "").lower() == "paid" and (u.get("utm_source") or "").lower() in META_SOURCES

    @flask.post("/t/visit")
    def landing_visit():
        """One landing page load, counted per day and campaign only: no cookies, no IP, nothing about the person."""
        body = request.get_json(silent=True) or {}
        clean = lambda k: re.sub(r"[^a-z0-9_.\-]", "", str(body.get(k) or "").lower())[:80]
        with state.lock:
            state.funnel.visit(datetime.now(timezone.utc).date().isoformat(), clean("utm_campaign"), clean("utm_source"),
                               clean("utm_medium"))
        return ("", 204)

    def meta_rows(client_id: str, launches: list, since: str, until: str) -> dict:
        """campaign_id -> engines.funnel() counts from Meta for the window (cached 10 minutes)."""
        key = ("funnel-meta", client_id, since, until)
        hit = state.ads_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        s = state.ads_settings(client_id, is_admin(client_id))
        if not s or not launches:
            return {}
        pub = meta_ads.Publisher(state.ads_http, s)
        out = {}
        for l in launches:
            try:
                data = pub.insights([l["campaign_id"]], since, until,
                                    fields="spend,impressions,clicks,inline_link_clicks,actions,action_values,account_currency")
            except meta_ads.PublishError as e:
                log.info("funnel insights for %s skipped: %s", l["campaign_id"], e)
                continue
            out[l["campaign_id"]] = engines.funnel(data[0] if data else {})
        state.ads_cache[key] = (time.time(), out)
        return out

    def proads_funnel(period: int, pick: str) -> dict:
        """ProAds' own funnel: Meta clicks -> landing visits -> sign-ups -> active -> checkout -> plan -> first payment.
        Everything after the click follows the people who signed up in the window (a cohort), whenever they went on."""
        admin = proads_admin()
        today = datetime.now(timezone.utc).date()
        with state.lock:
            started = state.funnel.started_at()
            launches = state.launches.list(admin)
            marks = state.funnel.marks()
            firsts = {r["client_id"]: r for r in state.revenue.all() if r["kind"] == "first"}
        start = max(today - timedelta(days=period - 1), datetime.fromtimestamp(started, timezone.utc).date())
        since = start.isoformat()
        days = (today - start).days + 1
        slug_of = {l["campaign_id"]: l.get("utm_slug") or meta_ads._slug(l.get("name", ""), 30) for l in launches}
        names = {}
        for l in launches:
            names.setdefault(slug_of[l["campaign_id"]], l.get("name") or slug_of[l["campaign_id"]])
        meta = meta_rows(admin, launches, since, today.isoformat())
        with state.lock:
            visits = state.funnel.visits(since)
            signed = state.store.db.execute("SELECT client_id, activated_at, attribution FROM accounts WHERE created_at >= ?",
                                            (since,)).fetchall()
        try:
            trial_days = max(0, int(os.environ.get("PROADS_TRIAL_DAYS", "0") or 0))
        except ValueError:
            trial_days = 0
        people = []
        for cid, activated, attr in signed:
            try:
                u = json.loads(attr) if attr else {}
            except ValueError:
                u = {}
            m = marks.get(cid, {})
            with state.lock:
                plan = "plan" in m or (state.billing.get(cid) or {}).get("subscription_id") is not None
            plan_at = m.get("plan")
            people.append({"meta": from_meta_ads(u), "slug": u.get("utm_campaign") or "", "active": bool(activated),
                           "checkout": "checkout" in m or plan, "plan": plan, "paid": cid in firsts,
                           # old enough to have been charged: trial over plus a few days for retries
                           "plan_mature": plan and (plan_at is None or plan_at < time.time() - (trial_days + 3) * 86400)})

        def stages_for(slug: Optional[str]) -> list[dict]:
            ids = [c for c, sl in slug_of.items() if slug is None or sl == slug]
            clicks = sum(meta[c]["link_clicks"] for c in ids if c in meta) if meta else None
            v = sum(n for camp, src, med, n in visits if med == "paid" and src in META_SOURCES and (slug is None or camp == slug))
            ppl = [p for p in people if p["meta"] and (slug is None or p["slug"] == slug)]
            src_meta, src_us, src_stripe = "Meta (link clicks)", "ProAds", "Stripe (money collected)"
            return [{"key": "clicks", "label": "Ad clicks", "count": int(clicks) if clicks is not None else None, "source": src_meta},
                    {"key": "visits", "label": "Landing visits", "count": v, "source": "ProAds landing page (page loads from Meta ads)"},
                    {"key": "signups", "label": "Sign-ups", "count": len(ppl), "source": src_us},
                    {"key": "active", "label": "Active (brought in data)", "count": sum(p["active"] for p in ppl), "source": src_us},
                    {"key": "checkout", "label": "Checkout started", "count": sum(p["checkout"] for p in ppl), "source": src_us},
                    {"key": "plan", "label": "Plan started (card added)", "count": sum(p["plan"] for p in ppl), "source": "Stripe"},
                    {"key": "paid", "label": "First payment collected", "count": sum(p["paid"] for p in ppl), "source": src_stripe,
                     "mature": (sum(p["plan_mature"] for p in ppl), sum(p["paid"] and p["plan_mature"] for p in ppl)),
                     "mature_note": f"judged only on plans started over {trial_days + 3} days ago (free trial plus card retries)"}]

        slugs = sorted({s for s in slug_of.values()})
        per = {s: stages_for(s) for s in slugs}
        chosen = pick if pick in per else "all"
        stages = per[chosen] if chosen != "all" else stages_for(None)
        peers = [[{**x, "campaign": names.get(s, s)} for x in st] for s, st in per.items() if s != chosen] if len(per) > 1 else []
        vals = [r["amount"] for r in firsts.values()]
        cur = (next(iter(firsts.values()))["currency"] if firsts else "AED")
        out = purchase_funnel.analyse(stages, "saas", peers, days, round(sum(vals) / len(vals), 2) if vals else None, cur,
                                      unit="paying subscribers")
        meta_lpv = sum(meta[c]["landing_views"] for c in slug_of if c in meta and (chosen == "all" or slug_of[c] == chosen)) if meta else None
        out.update(campaigns=[{"slug": s, "name": names.get(s, s)} for s in slugs], selected=chosen, counted_since=since,
                   ads_connected=bool(state.ads_settings(admin, True)), meta_landing_views=meta_lpv,
                   cohort_note="Sign-ups and every step after them follow people who signed up in this window, "
                               "even if they paid later. People who signed up recently may still pay.")
        return out

    def shop_funnel(client_id: str, period: int, pick: str) -> dict:
        """A client's funnel from Meta (clicks, page views, cart, checkout) ending in VERIFIED purchases when their sales
        data carries ad tracking; otherwise Meta's reported purchases, labelled as such."""
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=period - 1)
        with state.lock:
            launches = [l for l in state.launches.list(client_id) if (l.get("conversion") or "purchase") == "purchase"]
        slug_of = {l["campaign_id"]: l.get("utm_slug") or meta_ads._slug(l.get("name", ""), 30) for l in launches}
        names = {}
        for l in launches:
            names.setdefault(slug_of[l["campaign_id"]], l.get("name") or slug_of[l["campaign_id"]])
        meta = meta_rows(client_id, launches, start.isoformat(), today.isoformat())
        with state.lock:
            eng = state.engine(client_id)
            purchases = list(eng.store.purchases(client_id))
            tracked = any(from_meta_ads({"utm_medium": p.utm_medium, "utm_source": p.utm_source}) for p in purchases)
            verified = [(p.utm_campaign or "", p.amount - min(p.amount, eng.store.refunded_amount(client_id, p.external_id)))
                        for p in purchases if start <= p.occurred_at.date() <= today
                        and from_meta_ads({"utm_medium": p.utm_medium, "utm_source": p.utm_source})]
        verified = [(c, v) for c, v in verified if v > 0]

        def stages_for(slug: Optional[str]) -> list[dict]:
            ids = [c for c, sl in slug_of.items() if slug is None or sl == slug]
            g = lambda k: int(sum(meta[c][k] for c in ids if c in meta)) if meta else None
            if tracked:
                paid, src = sum(1 for c, _ in verified if slug is None or c == slug), "Your sales data (paid, after refunds)"
            else:
                paid, src = g("purchase"), "Meta (reported, not verified)"
            return [{"key": "clicks", "label": "Ad clicks", "count": g("link_clicks"), "source": "Meta"},
                    {"key": "visits", "label": "Landing page views", "count": g("landing_views"), "source": "Meta"},
                    {"key": "cart", "label": "Added to cart", "count": g("add_to_cart"), "source": "Meta"},
                    {"key": "checkout", "label": "Checkout started", "count": g("checkout"), "source": "Meta"},
                    {"key": "paid", "label": "Verified purchases" if tracked else "Purchases (Meta)", "count": paid, "source": src}]
        slugs = sorted(set(slug_of.values()))
        per = {s: stages_for(s) for s in slugs}
        chosen = pick if pick in per else "all"
        stages = per[chosen] if chosen != "all" else stages_for(None)
        peers = [[{**x, "campaign": names.get(s, s)} for x in st] for s, st in per.items() if s != chosen] if len(per) > 1 else []
        vals = [v for _, v in verified]
        out = purchase_funnel.analyse(stages, "shop", peers, period, round(sum(vals) / len(vals), 2) if vals else None,
                                      state.config(client_id).get("currency", "AED"), unit="purchases")
        out.update(campaigns=[{"slug": s, "name": names.get(s, s)} for s in slugs], selected=chosen,
                   counted_since=start.isoformat(), verified=tracked, ads_connected=bool(state.ads_settings(client_id, False)))
        return out

    @flask.get("/funnel/<client_id>")
    def funnel_view(client_id):
        state.require_key(client_id)
        try:
            period = int(request.args.get("period") or 30)
        except ValueError:
            period = 30
        period = period if period in (7, 30, 90) else 30
        pick = (request.args.get("campaign") or "all")[:80]
        return proads_funnel(period, pick) if is_admin(client_id) else shop_funnel(client_id, period, pick)
    state.funnel_for = lambda cid, period=30, pick="all": proads_funnel(period, pick) if is_admin(cid) else shop_funnel(cid, period, pick)

    @flask.get("/acquisition/<client_id>")
    def acquisition_endpoint(client_id):
        state.require_key(client_id)
        if not is_admin(client_id):
            abort(403, "only for the ProAds account")
        return acquisition_for()
    state.acquisition_for = acquisition_for

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

    def sync_spend(client_id: str, force: bool = False) -> Optional[dict]:
        """Copy new daily spend of every ProAds campaign (however it was switched on) into the ledger."""
        with state.lock:
            launches = state.launches.list(client_id)
        s = state.ads_settings(client_id, is_admin(client_id))
        if not launches or not s:
            return None
        out = ledger.sync(meta_ads.Publisher(state.ads_http, s), state.ledger, client_id, launches,
                          s.get("ad_account", ""), force=force, error_cls=Exception)
        if out["failed"]:
            log.warning("spend sync for %s: %d campaign(s) failed", client_id, len(out["failed"]))
        return out

    def sync_all_spend() -> None:
        with state.lock:
            clients = state.launches.clients()
        for cid in clients:
            try:
                sync_spend(cid)
            except Exception as e:
                log.warning("spend sync for %s failed: %s", cid, e)
    state.sync_all_spend = sync_all_spend

    def spend_so_far(client_id: str, start: int) -> tuple[float, str]:
        """Estimate for the running period (not billed from this; billing settles after the period ends)."""
        with state.lock:
            ids = [l["campaign_id"] for l in state.launches.list(client_id)]
            if not ids:
                return 0.0, ""
            return state.ledger.total(ids, ledger.day(start), ledger.day(int(time.time())))

    def default_payment_method(stripe, client_id: str) -> Optional[str]:
        """The card on their subscription (Checkout saves it there, not on the customer), even if it was cancelled."""
        with state.lock:
            sub = (state.billing.get(client_id) or {}).get("subscription_id")
        if not sub:
            return None
        try:
            return (stripe.get(f"subscriptions/{sub}") or {}).get("default_payment_method")
        except Exception as e:
            log.info("no card on subscription %s: %s", sub, e)
            return None

    def settle_due_periods() -> None:
        """Periods that ended SETTLE_HOURS ago: read the spend once more, then settle and lock them."""
        with state.lock:
            due = state.fees.due()
        for p in due:
            cid = p["client_id"]
            try:
                sync_spend(cid, force=True)
            except Exception as e:
                log.warning("spend sync before settling %s failed: %s", cid, e)
            with state.lock:
                launches = state.launches.list(cid)
                campaigns = [{**l, "created_ts": state.launches.created_ts(l["campaign_id"])} for l in launches]
                connected = state.ads_settings(cid, is_admin(cid)) is not None
            try:
                res = settlement.calculate(p, campaigns, state.ledger, billing.fee_percent(), billing.fx_rate, connected)
            except Exception as e:
                log.warning("settling FP-%s failed, will retry: %s", p["id"], e)
                continue
            with state.lock:
                if res["status"] == "pending":
                    state.fees.set_pending(p["id"], res["reason"])
                else:
                    state.fees.settle(p["id"], res["fee_minor"], res["completeness"], res["detail"])
                    log.info("settled FP-%s for %s: %s %s (%s)", p["id"], cid, res["fee_minor"] / 100,
                             p["invoice_currency"], res["completeness"])
    state.settle_due_periods = settle_due_periods

    def bill_period(stripe, p: dict, invoice_id: Optional[str]) -> Optional[dict]:
        """Put one settled period (or adjustment) on an invoice. Safe to repeat: Stripe is checked for the reference first."""
        ref = f"proads-fee-period-{p['id']}"
        item = billing.existing_item(stripe, p["customer"], ref)
        if not item:
            data = {"customer": p["customer"], "amount": str(p["fee_minor"]), "currency": p["invoice_currency"].lower(),
                    "description": settlement.describe(p), "metadata[kind]": "ad_spend_fee",
                    "metadata[proads_ref]": ref, "metadata[fee_period]": str(p["id"])}
            if invoice_id:
                data["invoice"] = invoice_id
            item = stripe.post("invoiceitems", data, idempotency_key=ref) or {}
        return item

    def on_invoice_created(inv: dict) -> None:
        """A renewal: the period that just ended starts settling. Its fee is billed on its own invoice once settled
        (the renewal invoice is created before the period can settle, so the fee never waits a whole extra month)."""
        with state.lock:
            cid = state.billing.by_customer(inv.get("customer") or "")
        if not cid or billing.fee_percent() <= 0 or inv.get("billing_reason") != "subscription_cycle":
            return
        with state.lock:
            state.fees.open(cid, inv["customer"], inv.get("currency") or "", inv.get("period_start"), inv.get("period_end"))

    def bill_settled_fees() -> None:
        """Every settled fee (and correction) goes out right away on one invoice per customer and currency.
        Crash-safe: each item carries a reference that is looked up in Stripe before anything is created,
        and an item already on an invoice is never invoiced again."""
        with state.lock:
            ready = state.fees.ready()
        groups: dict = {}
        for p in ready:
            groups.setdefault((p["customer"], p["invoice_currency"]), []).append(p)
        for (customer, currency), periods in groups.items():
            stripe = billing.Stripe(state.stripe_http)
            try:
                made = [(p, bill_period(stripe, p, None)) for p in periods]
                loose = [p for p, item in made if not item.get("invoice")]
                invoice_id = None
                if loose:
                    ids = "-".join(str(p["id"]) for p in sorted(loose, key=lambda x: x["id"]))
                    data = {"customer": customer, "auto_advance": "true", "collection_method": "charge_automatically",
                            "pending_invoice_items_behavior": "include", "currency": currency.lower(),
                            "description": "ProAds ad spend fee. Each line shows its FP reference; see Plan & billing for the breakdown.",
                            "metadata[kind]": "ad_spend_fee", "metadata[fee_periods]": ids}
                    pm = default_payment_method(stripe, periods[0]["client_id"])
                    if pm:
                        data["default_payment_method"] = pm
                    invoice_id = (stripe.post("invoices", data, idempotency_key=f"proads-fee-invoice-{ids}") or {}).get("id")
            except Exception as e:
                log.warning("fee invoice for %s failed, will retry: %s", customer, e)
                continue
            with state.lock:
                for p, item in made:
                    state.fees.billed(p["id"], item.get("invoice") or invoice_id, item.get("id"))
            log.info("billed %s for %s", [f"FP-{p['id']}" for p in periods], customer)
    state.bill_settled_fees = bill_settled_fees

    def recheck_billed_fees() -> None:
        """A week after billing, compare with Meta's latest numbers once; bill or credit a material difference separately."""
        with state.lock:
            todo = state.fees.to_recheck()
        for p in todo:
            cid = p["client_id"]
            try:
                sync_spend(cid, force=True)
            except Exception:
                pass
            with state.lock:
                campaigns = [{**l, "created_ts": state.launches.created_ts(l["campaign_id"])} for l in state.launches.list(cid)]
                connected = state.ads_settings(cid, is_admin(cid)) is not None
            if not connected:
                with state.lock:
                    state.fees.mark_rechecked(p["id"])
                continue
            # same exchange rates as when it was settled, so only Meta's spend changes count
            rates = {(f["from"], f["to"]): f["rate"] for f in (p["detail"] or {}).get("fx", [])}
            try:
                res = settlement.calculate(p, campaigns, state.ledger, billing.fee_percent(),
                                           lambda a, b: rates.get((a, b)) or billing.fx_rate(a, b), connected)
            except Exception as e:
                log.info("recheck of FP-%s skipped: %s", p["id"], e)
                continue
            if res["status"] != "settled":
                continue
            diff = res["fee_minor"] - (p["fee_minor"] or 0)
            with state.lock:
                if abs(diff) >= settlement.correction_threshold(p["fee_minor"] or 0):
                    state.fees.adjustment(p, diff, {**res["detail"], "previous_fee": (p["fee_minor"] or 0) / 100,
                                                    "correction": diff / 100})
                    log.info("FP-%s corrected by %s %s", p["id"], diff / 100, p["invoice_currency"])
                else:
                    state.fees.mark_rechecked(p["id"])
    state.recheck_billed_fees = recheck_billed_fees

    # ---------------- Purchase Event Reliability ----------------
    def retry_meta_events() -> None:
        """Re-send ProAds' own events that didn't reach Meta (same event_id, so Meta never counts one twice)."""
        with state.lock:
            due = state.delivery.due()
        for eid, ev in due:
            res = growth.send(ev, state.growth_http)
            with state.lock:
                status = state.delivery.result(eid, res)
            if state.growth_sent is not None:
                state.growth_sent.append((ev, res))
            log.info("retried Meta event %s: %s", eid, status)
        delivery_alert()
    state.retry_meta_events = retry_meta_events

    def delivery_alert(now: Optional[int] = None) -> Optional[str]:
        """Email once a day while ProAds' own events are failing for over 30 minutes."""
        now = int(now or time.time())
        with state.lock:
            h = state.delivery.health("proads", 7, now)
            oldest = state.store.db.execute("SELECT MIN(created_at) FROM meta_delivery WHERE client_id='proads'"
                                            " AND status IN ('retrying','sending','rejected','expired') AND created_at>=?",
                                            (now - 7 * 86400,)).fetchone()[0]
            last = state.store.db.execute("SELECT value FROM funnel_meta WHERE key='delivery_alert_at'").fetchone()
        if h["healthy"] or not oldest or now - oldest < 1800 or (last and now - int(last[0]) < 86400):
            return None
        to = os.environ.get("PROADS_ALERT_EMAIL") or os.environ.get("PROADS_CONTACT_EMAIL")
        text = ("ProAds' events are not reaching Meta.\n\n" + "\n".join(
            f"- {a['text']}" + (f"\n  Fix: {a['fix']}" if a.get("fix") else "") for a in h["alerts"]) +
            "\n\nDetails: Meta learning page in ProAds.")
        if to and mailer.available():
            threading.Thread(target=mailer.send, daemon=True, args=(to, "ProAds: events not reaching Meta", text)).start()
        log.warning("event delivery alert: %s", text.replace("\n", " "))
        with state.lock:
            state.store.db.execute("INSERT INTO funnel_meta (key, value) VALUES ('delivery_alert_at', ?)"
                                   " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
            state.store.db.commit()
        return text
    state.delivery_alert = delivery_alert

    def proads_delivery() -> dict:
        """ProAds' own events: health, plus every eligible sign-up and first payment checked against Meta's answer."""
        with state.lock:
            h = state.delivery.health("proads", 7)
            since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
            signups = state.store.db.execute("SELECT client_id, ads_ctx FROM accounts WHERE created_at>=?", (since,)).fetchall()
            firsts = [r for r in state.revenue.all() if r["kind"] == "first" and r["paid_at"] >= time.time() - 7 * 86400]
            started = state.store.db.execute("SELECT MIN(created_at) FROM meta_delivery").fetchone()[0]

            def check(event_id: str) -> str:
                d = state.delivery.status_of(event_id)
                if d:
                    return d["status"]
                return "untracked" if not started else "missing"
            rows = []
            for cid, ctx in signups:
                if is_admin(cid):
                    continue
                rows.append({"event": "CompleteRegistration", "who": cid,
                             "status": check(f"signup:{cid}") if ctx else "no_consent"})
            for r in firsts:
                acct = state.accounts.get(r["client_id"]) or {}
                rows.append({"event": "Purchase", "who": r["client_id"], "value": r["amount"], "currency": r["currency"],
                             "status": check(f"proads:first:{r['invoice_id']}") if acct.get("ads_ctx") else "no_consent"})
        eligible = [x for x in rows if x["status"] != "no_consent"]
        tally = {k: sum(1 for x in eligible if x["status"] == k) for k in ("accepted", "retrying", "sending", "rejected", "expired", "missing", "untracked")}
        return {"health": h, "reconciliation": {"eligible": len(eligible), "no_consent": len(rows) - len(eligible), **tally,
                                                "rows": rows[-50:]},
                "configured": bool(os.environ.get("PROADS_META_DATASET_ID") and os.environ.get("PROADS_META_TOKEN")),
                "dataset": os.environ.get("PROADS_META_DATASET_ID") or None,
                "test_mode": bool(os.environ.get("PROADS_META_TEST_CODE")),
                "note": "Eligible = the person accepted ad cookies. 'Missing' means ProAds never tried to send it; "
                        "'untracked' means it happened before delivery tracking started."}

    def client_delivery(client_id: str) -> dict:
        """A client's purchase events: what reached Meta, what's stuck, and why."""
        cfg = state.config(client_id)
        meta = cfg.get("meta", {})
        with state.lock:
            sig = state.signals.summary(client_id)
            stuck = [p for p in state.engine(client_id).store.unsent(client_id)
                     if p.occurred_at.timestamp() < time.time() - 2 * 3600 and p.occurred_at.timestamp() > time.time() - 7 * 86400]
            conn = state.meta_conns.get(client_id)
        alerts = []
        if conn and not meta_connect.public_view(conn)["ready"]:
            alerts.append({"level": "bad", "text": "Your Facebook connection has expired or was signed out by Facebook, so no "
                           "sales are reaching Meta.", "fix": "Reconnect Facebook in Connect & upload."})
        if meta.get("live") and stuck:
            alerts.append({"level": "bad", "text": f"{len(stuck)} paid order{'s' if len(stuck) != 1 else ''} older than 2 hours "
                           "haven't been sent to Meta yet.", "fix": (sig.get("last_error") or {}).get("message")})
        err = sig.get("last_error")
        if err and (not sig.get("last_accepted_at") or err["at"] > sig["last_accepted_at"]):
            alerts.append({"level": "bad", "text": "Meta refused the last delivery of your sales.", "fix": err["message"]})
        if not meta.get("live"):
            alerts.append({"level": "info", "text": "Sales are not being sent to Meta yet (practice mode or no Pixel chosen).",
                           "fix": "Choose your Pixel and switch sending on in Meta learning."})
        return {"signals": sig, "stuck": len(stuck), "alerts": alerts, "healthy": not [a for a in alerts if a["level"] == "bad"]}

    @flask.get("/delivery/<client_id>")
    def delivery_view(client_id):
        state.require_key(client_id)
        return proads_delivery() if is_admin(client_id) else client_delivery(client_id)
    state.proads_delivery = proads_delivery

    # ---------------- Budget Intelligence: human-approved scaling ----------------
    def read_budget(pub, campaign_id: str, currency: str) -> dict:
        """The campaign's daily budget (campaign-level or summed over running ad sets) and Meta's learning status."""
        d = pub._get(campaign_id, "status,effective_status,daily_budget,lifetime_budget,"
                     "adsets.limit(50){id,name,daily_budget,lifetime_budget,effective_status,learning_stage_info}",
                     f"reading the budget of campaign {campaign_id}")
        off = scaling.offset(currency)
        num = lambda v: float(v) / off if v not in (None, "", "0") else 0.0
        sets = [a for a in ((d.get("adsets") or {}).get("data") or []) if a.get("effective_status") == "ACTIVE"]
        lifetime = num(d.get("lifetime_budget")) > 0 or any(num(a.get("lifetime_budget")) > 0 for a in sets)
        adsets = [{"id": a["id"], "name": a.get("name", ""), "daily": num(a.get("daily_budget"))} for a in sets]
        if num(d.get("daily_budget")):
            budget = {"level": "campaign", "daily": num(d.get("daily_budget"))}
        else:
            budget = {"level": "adset", "daily": round(sum(a["daily"] for a in adsets), 2)}
        budget.update(campaign_id=campaign_id, adsets=adsets, lifetime=lifetime)
        learning = [((a.get("learning_stage_info") or {}).get("status") or "").upper() for a in sets]
        return {"status": (d.get("effective_status") or d.get("status") or "").upper(), "budget": budget, "learning": learning}

    def verified_sales(client_id: str, slug: str, since: datetime, settle_days: int) -> dict:
        """Verified paid sales that came from this campaign's own ad links, after refunds.
        settled = old enough for refunds to have arrived; recent = not yet; refund_rate from this business's history."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=settle_days)
        meta_link = lambda src, med: (med or "").lower() == "paid" and (src or "").lower() in META_SOURCES
        if is_admin(client_id):
            with state.lock:
                rows = [r for r in state.revenue.all() if r["kind"] == "first"]
            tracked = any(meta_link(r["utm"].get("utm_source"), r["utm"].get("utm_medium")) for r in rows)
            mine = [r for r in rows if r["utm"].get("utm_campaign") == slug and meta_link(r["utm"].get("utm_source"), r["utm"].get("utm_medium"))]
            kept = lambda r: r["amount"] - r["refunded"] - r["chargeback"] > 0.01
            when = lambda r: datetime.fromtimestamp(r["paid_at"], timezone.utc)
            old = [r for r in rows if when(r) < cutoff and when(r) > now - timedelta(days=180)]
            rate = (sum(1 for r in old if not kept(r)) / len(old)) if len(old) >= 10 else 0.0
            inwin = [r for r in mine if when(r) >= since and kept(r)]
            return {"tracked": tracked, "settled": sum(1 for r in inwin if when(r) < cutoff),
                    "recent": sum(1 for r in inwin if when(r) >= cutoff), "refund_rate": rate}
        with state.lock:
            eng = state.engine(client_id)
            ps = list(eng.store.purchases(client_id))
            refunded = {p.external_id: eng.store.refunded_amount(client_id, p.external_id) for p in ps}
        tracked = any(meta_link(p.utm_source, p.utm_medium) for p in ps)
        kept = lambda p: refunded.get(p.external_id, 0) < p.amount
        old = [p for p in ps if cutoff > p.occurred_at > now - timedelta(days=180)]
        rate = (sum(1 for p in old if not kept(p)) / len(old)) if len(old) >= 10 else 0.0
        inwin = [p for p in ps if p.occurred_at >= since and p.utm_campaign == slug and meta_link(p.utm_source, p.utm_medium) and kept(p)]
        return {"tracked": tracked, "settled": sum(1 for p in inwin if p.occurred_at < cutoff),
                "recent": sum(1 for p in inwin if p.occurred_at >= cutoff), "refund_rate": rate}

    def spend_in(client_id: str, campaign_id: str, since_day: str, to_cur: str) -> float:
        with state.lock:
            # no upper cut-off: ledger days are the ad account's local dates, which can be ahead of UTC
            amt, cur = state.ledger.total([campaign_id], since_day, "2100-01-01")
        if cur and to_cur and cur.upper() != to_cur.upper():
            amt *= billing.fx_rate(cur, to_cur)
        return amt

    def scaling_for(client_id: str, only: Optional[str] = None) -> dict:
        admin = is_admin(client_id)
        econ = economics_for(client_id)
        if econ:                                     # sales here are already net of real refunds: don't count them twice
            econ = engines.economics({**econ["inputs"], "refund_rate": 0})
        econ_cur = state.config(client_id).get("currency", "AED").upper()
        with state.lock:
            launches = [l for l in state.launches.list(client_id) if l.get("status") != "DELETED"]
            cap = state.scaling.cap(client_id)
        out = {"cap": cap, "economics": bool(econ), "recommendations": [], "currency": None}
        s = state.ads_settings(client_id, admin)
        if not launches:
            return out
        if s is None:
            return {**out, "error": "Connect Facebook & Instagram again so ProAds can read your budgets."}
        pub = meta_ads.Publisher(state.ads_http, s)
        settle = refund_delay_days(client_id) if not admin else 14
        since = datetime.now(timezone.utc) - timedelta(days=scaling.WINDOW_DAYS)
        reads = {}
        for l in launches:
            with state.lock:
                _, cur = state.ledger.total([l["campaign_id"]], "2000-01-01", "2100-01-01")
            currency = (cur or ads_currency(client_id)).upper()
            try:
                reads[l["campaign_id"]] = (l, currency, read_budget(pub, l["campaign_id"], currency))
            except meta_ads.PublishError as e:
                out["recommendations"].append({"campaign_id": l["campaign_id"], "name": l.get("name", ""), "action": "hold",
                                               "reasons": [str(e)[:200]], "blockers": [], "rec_id": None})
        total_now = sum(r["budget"]["daily"] for _, _, r in reads.values() if r["status"] == "ACTIVE" and not r["budget"]["lifetime"])
        for cid_, (l, currency, r) in reads.items():
            if only and cid_ != only:
                continue
            slug = l.get("utm_slug") or meta_ads._slug(l.get("name", ""), 30)
            sales = verified_sales(client_id, slug, since, settle)
            try:
                spend = spend_in(client_id, cid_, since.date().isoformat(), econ_cur)
            except Exception as e:
                out["recommendations"].append({"campaign_id": cid_, "name": l.get("name", ""), "action": "hold",
                                               "reasons": [f"No exchange rate for {currency} to {econ_cur}: {e}"], "blockers": [], "rec_id": None})
                continue
            with state.lock:
                last = state.scaling.last(client_id, cid_)
            after = None
            if last and last["action"] == "scale_up" and not last["undone_by"]:
                start = datetime.fromtimestamp(last["at"], timezone.utc)
                a_sales = verified_sales(client_id, slug, start, settle)
                after = {"spend": spend_in(client_id, cid_, start.date().isoformat(), econ_cur),
                         "sales": a_sales["settled"] + a_sales["recent"] * (1 - a_sales["refund_rate"])}
            c = {"campaign_id": cid_, "name": l.get("name", ""), "status": r["status"], "budget": r["budget"],
                 "learning": r["learning"], "spend": spend, "sales_settled": sales["settled"], "sales_recent": sales["recent"],
                 "refund_rate": sales["refund_rate"], "tracked": sales["tracked"], "currency": currency, "after_change": after}
            rec = scaling.recommend(c, econ, cap, total_now, last if last and not last["undone_by"] else None)
            rec["learning"] = r["learning"]
            rec["budget_level"] = r["budget"]["level"]
            out["recommendations"].append(rec)
            out["currency"] = out["currency"] or currency
        out["total_daily"] = round(total_now, 2)
        out["spend_currency"] = econ_cur
        return out

    @flask.get("/scaling/<client_id>")
    def scaling_view(client_id):
        state.require_key(client_id)
        out = scaling_for(client_id)
        with state.lock:
            out["history"] = state.scaling.history(client_id)
        return out

    @flask.post("/scaling/<client_id>/settings")
    def scaling_settings(client_id):
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        v = body.get("max_daily")
        try:
            v = None if v in (None, "") else float(v)
        except (TypeError, ValueError):
            abort(400, "Enter the maximum as a number.")
        if v is not None and (v <= 0 or v > 1e7):
            abort(400, "Enter a maximum above zero.")
        with state.lock:
            state.scaling.set_cap(client_id, v)
        return {"ok": True, "max_daily": v}

    def apply_budget(client_id: str, pub, budget: dict, new_daily: float, currency: str) -> tuple[dict, bool]:
        """Write the new daily budget(s) to Meta, then read them back to confirm."""
        targets = scaling.split_budget(budget, new_daily, currency)
        for obj, minor in targets:
            pub._post(obj, {"daily_budget": minor}, f"changing the daily budget of {obj}")
        check = read_budget(pub, budget["campaign_id"], currency)["budget"]
        ok = abs(check["daily"] - new_daily) <= max(0.02 * new_daily, 1.0)
        return {"targets": targets, "read_back": check["daily"]}, ok

    @flask.post("/scaling/<client_id>/apply")
    def scaling_apply(client_id):
        """Carry out ONE recommendation the owner approved. It's re-checked first; if it changed, nothing happens."""
        state.require_key(client_id)
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            abort(400, "Confirm the change first.")
        camp = str(body.get("campaign_id") or "")
        fresh = scaling_for(client_id, only=camp)
        rec = next((r for r in fresh["recommendations"] if r["campaign_id"] == camp), None)
        if not rec:
            abort(404, "That campaign isn't one ProAds manages.")
        if rec.get("rec_id") != body.get("rec_id") or rec["action"] == "hold":
            resp = jsonify({"error": "The numbers changed since you looked, so the recommendation changed. Review the new one.",
                            "recommendation": rec})
            resp.status_code = 409
            return resp
        s = state.ads_settings(client_id, is_admin(client_id))
        pub = meta_ads.Publisher(state.ads_http, s)
        currency = rec["currency"]
        before = read_budget(pub, camp, currency)
        try:
            if rec["action"] == "pause":
                pub._post(camp, {"status": "PAUSED"}, f"pausing campaign {camp}")
                ok = pub.status_of(camp) == "PAUSED"
                if ok:
                    with state.lock:
                        state.launches.set_status(client_id, camp, "PAUSED")
                detail = {"status": "PAUSED"}
            else:
                detail, ok = apply_budget(client_id, pub, before["budget"], rec["new_daily"], currency)
        except meta_ads.PublishError as e:
            abort(502, str(e))
        with state.lock:
            last = state.scaling.last(client_id, camp)
            change = state.scaling.record(client_id, camp, rec["action"], before["budget"], detail, before["budget"]["daily"],
                                          rec.get("new_daily"), "admin" if is_admin(client_id) else (state.accounts.get(client_id) or {}).get("email"),
                                          rec["reasons"], ok, undoes=last["id"] if rec["action"] == "undo" and last else None)
        state.ads_cache.pop(("engines", client_id), None)
        log.info("budget change %s for %s/%s: %s -> %s (verified %s)", rec["action"], client_id, camp,
                 before["budget"]["daily"], rec.get("new_daily"), ok)
        return {"ok": ok, "change_id": change, "action": rec["action"],
                "message": None if ok else "Meta accepted the request but the budget it reports back differs. Check Ads Manager."}

    @flask.post("/scaling/<client_id>/undo/<int:change_id>")
    def scaling_undo(client_id, change_id):
        """Put back the budgets from before a change ProAds made (within 14 days, if nothing changed since)."""
        state.require_key(client_id)
        if (request.get_json(silent=True) or {}).get("confirm") is not True:
            abort(400, "Confirm the change first.")
        with state.lock:
            hist = {h["id"]: h for h in state.scaling.history(client_id, 200)}
            ch = hist.get(change_id)
            last = state.scaling.last(client_id, ch["campaign_id"]) if ch else None
        if not ch or ch["undone_by"] or ch["action"] == "undo":
            abort(404, "Nothing to undo.")
        if not last or last["id"] != change_id:
            abort(409, "A newer change was made to this campaign since. Undo that one first.")
        if time.time() - ch["at"] > 14 * 86400:
            abort(409, "Too long ago to undo safely. Set the budget you want in Ads Manager.")
        s = state.ads_settings(client_id, is_admin(client_id))
        pub = meta_ads.Publisher(state.ads_http, s)
        before = last["before"]
        with state.lock:
            _, cur = state.ledger.total([ch["campaign_id"]], "2000-01-01", "2100-01-01")
        currency = (cur or ads_currency(client_id)).upper()
        now_b = read_budget(pub, ch["campaign_id"], currency)
        try:
            if ch["action"] == "pause":
                pub._post(ch["campaign_id"], {"status": "ACTIVE"}, f"switching campaign {ch['campaign_id']} back on")
                ok = pub.status_of(ch["campaign_id"]) == "ACTIVE"
                if ok:
                    with state.lock:
                        state.launches.set_status(client_id, ch["campaign_id"], "ACTIVE")
                detail = {"status": "ACTIVE"}
            else:
                off = scaling.offset(currency)
                targets = [(before["campaign_id"], int(round(before["daily"] * off)))] if before["level"] == "campaign" else \
                    [(a["id"], int(round(a["daily"] * off))) for a in before["adsets"] if a.get("daily")]
                for obj, minor in targets:
                    pub._post(obj, {"daily_budget": minor}, f"restoring the daily budget of {obj}")
                back = read_budget(pub, ch["campaign_id"], currency)["budget"]["daily"]
                ok = abs(back - before["daily"]) <= max(0.02 * before["daily"], 1.0)
                detail = {"targets": targets, "read_back": back}
        except meta_ads.PublishError as e:
            abort(502, str(e))
        with state.lock:
            cid2 = state.scaling.record(client_id, ch["campaign_id"], "undo", now_b["budget"], detail, now_b["budget"]["daily"],
                                        before.get("daily"), "admin" if is_admin(client_id) else (state.accounts.get(client_id) or {}).get("email"),
                                        [f"Undo of change #{change_id}"], ok, undoes=change_id)
        state.ads_cache.pop(("engines", client_id), None)
        return {"ok": ok, "change_id": cid2}

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
        force = bool((request.get_json(silent=True) or {}).get("force"))
        with state.lock:
            live = [l for l in state.launches.list(client_id) if l["status"] == "ACTIVE"]
        s = state.ads_settings(client_id, is_admin(client_id))
        not_paused = []
        for l in live:
            try:
                if not s:
                    raise RuntimeError("no ad account selected")
                pub = meta_ads.Publisher(state.ads_http, s)
                pub.set_status(l, "PAUSED")
                if pub.status_of(l["campaign_id"]) not in ("PAUSED", "DELETED", "ARCHIVED"):
                    raise RuntimeError("Meta still shows it running")
                with state.lock:
                    state.launches.set_status(client_id, l["campaign_id"], "PAUSED")
            except Exception as e:
                log.warning("could not pause %s before disconnecting %s: %s", l["campaign_id"], client_id, e)
                not_paused.append({"campaign_id": l["campaign_id"], "name": l.get("name", ""), "error": str(e)[:200]})
        if not_paused and not force:
            resp = jsonify({"error": "Some campaigns are still running and couldn't be paused. Pause them in Ads Manager "
                                     "first: Meta keeps spending from your ad account even after you disconnect.",
                            "not_paused": not_paused, "can_force": True})
            resp.status_code = 409
            abort(resp)
        try:
            sync_spend(client_id, force=True)                 # last reading before access goes away
        except Exception as e:
            log.warning("final spend sync for %s failed: %s", client_id, e)
        with state.lock:
            for l in state.launches.list(client_id):            # verified paused: no spend after this moment
                if l["status"] != "ACTIVE" and l["campaign_id"] not in {x["campaign_id"] for x in not_paused}:
                    state.ledger.mark(client_id, l["campaign_id"], closed=True)
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
        return {"connected": False, "paused": [l["campaign_id"] for l in live if l["campaign_id"] not in {x["campaign_id"] for x in not_paused}],
                "still_running": not_paused}

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

    @flask.errorhandler(Exception)
    def unexpected(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify({"error": e.description}), e.code
        ref = secrets.token_hex(3)
        log.exception("unexpected error %s on %s", ref, request.path)
        return jsonify({"error": f"Something broke on our side ({type(e).__name__}: {str(e)[:160]}). Ref {ref}"}), 500

    if start_worker if start_worker is not None else os.environ.get("PAYSIGNAL_WORKER", "1") == "1":
        state.instant_meta = True
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
            for job in ("sync_all_launch_status", "tiktok_push_all", "retry_meta_events", "stripe_sync_all", "sync_all_spend", "settle_due_periods", "bill_settled_fees", "recheck_billed_fees"):
                try:
                    if getattr(state, job, None):
                        getattr(state, job)()
                except Exception:
                    log.exception("%s failed", job)
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
