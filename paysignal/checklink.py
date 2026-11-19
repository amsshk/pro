"""Check links: a private, read-only page of one business's tracking totals that the admin can hand to someone who
can't sign in (a support helper, an AI assistant reading the page, an accountant).

Totals only: paid orders and money per day, what was sent to Meta and how many Meta accepted, what TikTok accepted,
the last error. No names, emails, phones, addresses, order numbers or tokens ever appear. A link works for 24 hours by
default, can be cancelled, and only its SHA-256 is stored, so a copy of the database can't be used to open one.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

HOURS = 24
DAYS = 7


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class CheckLinkStore:
    def __init__(self, db):
        self.db = db
        self.db.execute("CREATE TABLE IF NOT EXISTS check_links (token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,"
                        " created_by TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, opened INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS shopify_hook_log (client_id TEXT NOT NULL, at INTEGER NOT NULL, topic TEXT,"
                        " ok INTEGER NOT NULL, note TEXT)")
        self.db.commit()

    def create(self, client_id: str, created_by: str, hours: int = HOURS) -> tuple[str, int]:
        token = secrets.token_urlsafe(24)
        now = int(time.time())
        exp = now + max(1, min(int(hours), 72)) * 3600
        self.db.execute("INSERT INTO check_links VALUES (?,?,?,?,?,0)", (_h(token), client_id, created_by, now, exp))
        self.db.commit()
        return token, exp

    def resolve(self, token: str) -> Optional[str]:
        if not token or len(token) > 100:
            return None
        r = self.db.execute("SELECT client_id, expires_at FROM check_links WHERE token_hash=?", (_h(token),)).fetchone()
        if not r or r[1] < time.time():
            return None
        self.db.execute("UPDATE check_links SET opened=opened+1 WHERE token_hash=?", (_h(token),))
        self.db.commit()
        return r[0]

    def hook(self, client_id: str, topic: str, ok: bool, note: str = "") -> None:
        """One line per Shopify webhook that arrives (topic and whether its signature matched), kept 14 days."""
        now = int(time.time())
        self.db.execute("INSERT INTO shopify_hook_log VALUES (?,?,?,?,?)", (client_id, now, topic, int(ok), note[:120]))
        self.db.execute("DELETE FROM shopify_hook_log WHERE at<?", (now - 14 * 86400,))
        self.db.commit()

    def hooks(self, client_id: str) -> dict:
        since = int(time.time()) - DAYS * 86400
        rows = self.db.execute("SELECT at, topic, ok, note FROM shopify_hook_log WHERE client_id=? AND at>=? ORDER BY at",
                               (client_id, since)).fetchall()
        topics: dict = {}
        for _, t, ok, _ in rows:
            topics.setdefault(t or "?", [0, 0])[0 if ok else 1] += 1
        last_ok = max((r[0] for r in rows if r[2]), default=None)
        bad = [r for r in rows if not r[2]]
        return {"hooks_ok": sum(1 for r in rows if r[2]), "hooks_rejected": len(bad), "hook_topics": topics,
                "last_hook_ok": last_ok, "last_rejected": {"at": bad[-1][0], "why": bad[-1][3]} if bad else None}

    def cancel_all(self, client_id: str) -> int:
        n = self.db.execute("DELETE FROM check_links WHERE client_id=?", (client_id,)).rowcount
        self.db.commit()
        return n


def build(db, client_id: str, purchases, refunded: Callable, tz: str = "Asia/Dubai", now: Optional[float] = None) -> dict:
    """Per day (business time zone) for the last DAYS days: paid orders, money kept, purchases sent to Meta live and
    accepted, purchases TikTok accepted."""
    try:
        z = ZoneInfo(tz or "Asia/Dubai")
    except Exception:
        z = ZoneInfo("Asia/Dubai")
    now = now or time.time()
    today = datetime.fromtimestamp(now, z).date()
    days = [today - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    start = datetime(days[0].year, days[0].month, days[0].day, tzinfo=z)
    since = int(start.timestamp())
    row = {d: {"day": d.isoformat(), "orders": 0, "money": 0.0, "meta_sent": 0, "meta_accepted": 0, "tiktok_accepted": 0,
               "practice": 0} for d in days}
    day_of = lambda ts: datetime.fromtimestamp(ts, z).date()
    for p in purchases:
        kept = p.amount - refunded(p)
        d = p.occurred_at.astimezone(z).date()
        if d in row and kept > 0.005:
            row[d]["orders"] += 1
            row[d]["money"] += kept
    meta_err = None
    for at, mode, purch, received, err in db.execute(
            "SELECT at, mode, purchases, received, error FROM capi_log WHERE client_id=? AND at>=? ORDER BY at", (client_id, since)):
        d = day_of(at)
        if d not in row:
            continue
        if mode != "live":
            row[d]["practice"] += purch or 0
            continue
        row[d]["meta_sent"] += purch or 0
        if not err and received:
            row[d]["meta_accepted"] += purch or 0
        if err:
            meta_err = {"at": at, "message": str(err)[:200]}
        elif received:
            meta_err = None                      # a later batch went through, so the earlier error is over
    tt_err = None
    for sent_at, status, reason in db.execute(
            "SELECT sent_at, status, reason FROM tiktok_delivered WHERE client_id=? AND sent_at>=? AND event_id NOT LIKE '%refund%'",
            (client_id, since)):
        d = day_of(sent_at)
        if d in row and status == "accepted":
            row[d]["tiktok_accepted"] += 1
        elif status != "accepted" and (not tt_err or sent_at > tt_err["at"]):
            tt_err = {"at": sent_at, "message": str(reason or status)[:200]}
    last = lambda sql: (db.execute(sql, (client_id,)).fetchone() or [None])[0]
    last_tt = last("SELECT MAX(sent_at) FROM tiktok_delivered WHERE client_id=? AND status='accepted'")
    if tt_err and last_tt and last_tt >= tt_err["at"]:
        tt_err = None
    out = [dict(r, money=round(r["money"], 2)) for r in row.values()]
    tot = {k: sum(r[k] for r in out) for k in ("orders", "meta_sent", "meta_accepted", "tiktok_accepted", "practice")}
    tot["money"] = round(sum(r["money"] for r in out), 2)
    return {"days": out, "totals": tot, "as_of": int(now), "tz": str(z),
            "meta_connected": bool(last("SELECT 1 FROM capi_log WHERE client_id=? AND mode='live' LIMIT 1")),
            "tiktok_connected": bool(last("SELECT 1 FROM tiktok_pixels WHERE client_id=? AND active=1 LIMIT 1")),
            "last_meta_accepted": last("SELECT MAX(at) FROM capi_log WHERE client_id=? AND mode='live' AND error IS NULL AND COALESCE(received,0)>0"),
            "last_tiktok_accepted": last_tt,
            "last_sale": max((int(p.occurred_at.timestamp()) for p in purchases), default=None),
            "meta_error": meta_err, "tiktok_error": tt_err}


def verdict(d: dict) -> str:
    t = d["totals"]
    if not t["orders"]:
        return ("ProAds received no paid orders in the last 7 days. If the shop did sell, the sales aren't reaching ProAds: "
                "check the Shopify connection below.")
    parts = []
    if d["meta_connected"]:
        share = t["meta_accepted"] / t["orders"] if t["orders"] else 0
        parts.append(f"Meta accepted {t['meta_accepted']} of {t['orders']} paid orders ({round(min(share, 1) * 100)}%).")
    else:
        parts.append("Meta isn't sending live sales.")
    if d["tiktok_connected"]:
        share = t["tiktok_accepted"] / t["orders"] if t["orders"] else 0
        parts.append(f"TikTok accepted {t['tiktok_accepted']} of {t['orders']} ({round(min(share, 1) * 100)}%).")
    if t["practice"]:
        parts.append(f"{t['practice']} were sent in practice mode only (Meta Test events), not to the ads.")
    return " ".join(parts)


def render(name: str, cur: str, d: dict, expires_at: Optional[int], esc) -> str:
    z = ZoneInfo(d["tz"])
    when = lambda ts: datetime.fromtimestamp(ts, z).strftime("%d %b %H:%M") if ts else "never"
    rows = "".join(
        f"<tr><td>{datetime.fromisoformat(r['day']).strftime('%a %d %b')}</td><td>{r['orders']}</td><td>{r['money']:,.0f}</td>"
        f"<td>{r['meta_sent']}</td><td>{r['meta_accepted']}</td><td>{r['tiktok_accepted']}</td><td>{r['practice']}</td></tr>"
        for r in d["days"])
    t = d["totals"]
    errs = "".join(f"<p class='err'>{esc(k)} last error ({when(e['at'])}): {esc(e['message'])}</p>"
                   for k, e in (("Meta", d["meta_error"]), ("TikTok", d["tiktok_error"])) if e)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Tracking check: {esc(name)}</title>
<style>body{{font-family:Arial,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#141414;background:#fff}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:right}}
th:first-child,td:first-child{{text-align:left}}.v{{font-size:18px;font-weight:700}}.err{{color:#A30000}}.s{{color:#5C5C5C;font-size:13px}}</style></head><body>
<p class="s">ProAds tracking check · totals only, no customer details</p>
<h1>{esc(name)}</h1>
<p class="v">{esc(verdict(d))}</p>
<p>Last 7 days ({esc(d['tz'])}): {t['orders']} paid orders, {t['money']:,.0f} {esc(cur)} really paid (refunds taken off).</p>
<p>Last sale: {when(d['last_sale'])} · Last accepted by Meta: {when(d['last_meta_accepted'])} · Last accepted by TikTok: {when(d['last_tiktok_accepted'])}</p>
{errs}
{shopify_html(d.get("shopify"), when, esc)}
<table><thead><tr><th>Day</th><th>Paid orders</th><th>Paid ({esc(cur)})</th><th>Sent to Meta</th><th>Meta accepted</th><th>TikTok accepted</th><th>Practice only</th></tr></thead>
<tbody>{rows}<tr><th>Total</th><th>{t['orders']}</th><th>{t['money']:,.0f}</th><th>{t['meta_sent']}</th><th>{t['meta_accepted']}</th><th>{t['tiktok_accepted']}</th><th>{t['practice']}</th></tr></tbody></table>
<p class="s">A sale is sent after it's paid, usually within minutes, so today's last orders may not be sent yet. Meta counts are
purchases in batches Meta confirmed it received. Checked {when(d['as_of'])}{(' · this link stops working ' + when(expires_at)) if expires_at else ''}.</p>
</body></html>"""


def shopify_html(sh: Optional[dict], when, esc) -> str:
    if not sh:
        return ""
    lines = [f"Shopify app: {'connected since ' + esc(sh['app_connected_at'] or '') + ', last sync ' + esc(sh['app_last_sync'] or 'never') + ' (' + esc(sh['app_sync'] or '') + ')' if sh['app'] else 'not connected'}",
             f"Webhook signing key saved: {'yes' if sh['signing_key'] else 'no'}",
             f"Shopify webhooks in the last 7 days: {sh['hooks_ok']} accepted, {sh['hooks_rejected']} rejected"
             + (f" · last accepted {when(sh['last_hook_ok'])}" if sh["last_hook_ok"] else "")]
    if sh["hook_topics"]:
        lines.append("By topic: " + ", ".join(f"{esc(k)} {v[0]} ok" + (f" / {v[1]} rejected" if v[1] else "") for k, v in sorted(sh["hook_topics"].items())))
    if sh["last_rejected"]:
        lines.append(f"<span class='err'>Last rejected {when(sh['last_rejected']['at'])}: {esc(sh['last_rejected']['why'])}</span>")
    return "<h2>Shopify connection</h2>" + "".join(f"<p>{l}</p>" for l in lines) + \
        "<p class='s'>Webhook counting started 20 Nov 2026, so earlier days show none.</p>"
