"""Automatic emails to clients: new paid orders, weekly ads report, campaign live / paused, plan renewal,
subscription (started, payment failed, ended) and TikTok alerts (low balance, learning on / off / done).

Each email is built here as (subject, text, html). Sending, timing and the "already sent" check live in server.py.
Orders and reports only ever show amounts and order numbers, never customer names, emails or phones.
"""
from __future__ import annotations

import html
import time

KINDS = ("orders", "weekly", "live", "paused", "renews", "plan_started", "payment_failed", "plan_ended", "receipt", "welcome", "connected", "news",
         "tt_balance", "tt_learning_on", "tt_learning_off", "tt_learning_done")


class ClientMailStore:
    def __init__(self, db):
        self.db = db
        self.db.execute("CREATE TABLE IF NOT EXISTS client_mails (client_id TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,"
                        " sent_at INTEGER NOT NULL, PRIMARY KEY (client_id, kind, ref))")
        self.db.commit()

    def sent(self, client_id: str, kind: str, ref: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM client_mails WHERE client_id=? AND kind=? AND ref=?",
                                    (client_id, kind, str(ref))).fetchone())

    def mark(self, client_id: str, kind: str, ref: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO client_mails VALUES (?,?,?,?)", (client_id, kind, str(ref), int(time.time())))
        self.db.commit()

    def installed_at(self, tag: str = "1") -> int:
        """When these emails were switched on. Orders from before that are never emailed (no flood on first deploy)."""
        row = self.db.execute("SELECT sent_at FROM client_mails WHERE client_id='*' AND kind='installed' AND ref=?", (tag,)).fetchone()
        if row:
            return int(row[0])
        now = int(time.time())
        self.mark("*", "installed", tag)
        return now


def money(amount: float, currency: str) -> str:
    return f"{currency} {amount:,.2f}"


def _page(name: str, lead: str, body: list[str], app_url: str, button: str = "Open ProAds", why: str = "", path: str = "home", link: str = "") -> tuple[str, str]:
    link = link or app_url.rstrip("/") + "/app#/" + path
    esc = html.escape
    paras = "".join(f'<p style="margin:0 0 12px">{esc(b)}</p>' for b in body)
    page = (f'<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#141414">'
            f'<p style="font-size:13px;color:#5C5C5C;margin:0">ProAds · {esc(name)}</p>'
            f'<p style="font-size:22px;font-weight:700;margin:8px 0 14px">{esc(lead)}</p>{paras}'
            f'<p><a href="{esc(link)}" style="background:#4A7600;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:700">{esc(button)}</a></p>'
            + (f'<p style="font-size:12px;color:#5C5C5C">{esc(why)}</p>' if why else "") + '</div>')
    text = f"Hello,\n\n{lead}\n\n" + "\n\n".join(body) + f"\n\n{button}: {link}\n\nProAds"
    return text, page


def render(kind: str, name: str, d: dict, app_url: str) -> tuple[str, str, str]:
    """Subject, text, HTML. d holds the numbers for that kind (see sample())."""
    cur = d.get("currency") or "AED"
    why = "You get this because your business uses ProAds."
    button = "Open ProAds"
    if kind == "orders":
        orders = d.get("orders") or []
        n = len(orders)
        total = sum(o["amount"] for o in orders)
        subject = f"{name}: {n} new paid order{'s' if n != 1 else ''}"
        lead = f"You got {n} new paid order{'s' if n != 1 else ''}."
        body = [f"Total paid: {money(total, cur)} (after refunds)."]
        lines = [f"#{o['order']} · {money(o['amount'], cur)} · {o['from']}" for o in orders[:10]]
        if lines:
            body.append("  |  ".join(lines))
        if n > 10:
            body.append(f"and {n - 10} more.")
    elif kind == "weekly":
        subject = f"{name}: your ads report for last week"
        lead = "Your week with ProAds."
        body = [f"Paid orders: {d['orders']}. Money kept after refunds: {money(d['kept'], cur)}."]
        if d.get("refunds"):
            body.append(f"Refunded: {money(d['refunds'], cur)}.")
        if d.get("spend") is not None:
            line = f"Ad spend: {money(d['spend'], d.get('spend_currency') or cur)}."
            if d.get("cost_per_order"):
                line += f" Cost per paid order: {money(d['cost_per_order'], cur)}."
            body.append(line)
        if not d.get("orders") and d.get("spend"):
            body.append("Ads spent money but no paid orders came in this week. Open ProAds to see what to change.")
        body.append(f"Period: {d.get('from', '')} to {d.get('to', '')}.")
    elif kind in ("live", "paused"):
        camp, plat = d.get("campaign") or "your campaign", d.get("platform") or "your ad account"
        if kind == "live":
            subject = f"{name}: your campaign is live"
            lead = "Your campaign is now live."
            body = [f"{camp} is running on {plat}."]
        else:
            subject = f"{name}: your campaign is paused"
            lead = "Your campaign is paused."
            body = [f"{camp} on {plat} is paused."]
            if d.get("reason") == "billing":
                body.append("Your free ad test ended, so ProAds paused it. Add a card on Plan & billing and it runs again.")
        body.append("Spending only happens while a campaign is live.")
    elif kind == "renews":
        days = d.get("days_left", 3)
        if d.get("cancels"):
            subject = f"{name}: your ProAds plan ends in {days} days"
            lead = f"Your {d.get('plan', 'ProAds')} plan ends on {d.get('ends', '')}."
            body = ["You cancelled it, so it won't renew. After that, new campaigns can't be published and ProAds campaigns pause.",
                    "Changed your mind? Open Plan & billing and keep it."]
        else:
            subject = f"{name}: your ProAds plan renews in {days} days"
            lead = f"Your {d.get('plan', 'ProAds')} plan renews on {d.get('ends', '')}."
            body = ["Your card is charged automatically, plus the ad spend fee for the campaigns ProAds ran.",
                    "To change or cancel, open Plan & billing."]
    elif kind == "receipt":
        num = f" {d['number']}" if d.get("number") else ""
        subject = f"Your ProAds receipt{num}: {money(d.get('amount') or 0, cur)}"
        lead = f"Thank you, we received your payment of {money(d.get('amount') or 0, cur)}."
        body = [f"Invoice{num or ''}: {d.get('what') or 'ProAds plan'}, paid on {d.get('date') or 'today'}."]
        if d.get("pdf"):
            body.append(f"Download the PDF invoice: {d['pdf']}")
        button = "View invoice"
        why = "You get this because you paid for ProAds."
    elif kind == "welcome":
        subject = f"Welcome to ProAds, {name}"
        lead = "Welcome to ProAds."
        body = ["Next steps: connect your store (Shopify), connect your ad account (TikTok or Facebook), then build your first campaign.",
                "ProAds sends your real paid sales to Meta and TikTok so your ads learn who buys. Reply to this email any time for help."]
    elif kind == "connected":
        plat = d.get("platform") or "Your account"
        subject = f"{name}: {plat} is connected"
        lead = f"{plat} is connected to ProAds."
        body = {"Shopify": ["Your paid orders are being imported and new ones arrive by themselves."],
                "TikTok": ["Your TikTok ad account is connected. Real paid sales can now go to TikTok, and you can build campaigns."],
                "Facebook": ["Your Facebook ad account is connected. Real paid sales can now go to Meta, and you can build campaigns."]}.get(
            plat, ["Nothing else to do: ProAds keeps it in sync."])
    elif kind == "news":
        subject = f"ProAds News: {d.get('title') or 'new article'}"
        lead = d.get("title") or "New article on ProAds News"
        body = [d.get("summary") or "A new article is up on ProAds News."]
        button = "Read the article"
    elif kind == "plan_started":
        subject = f"{name}: your ProAds {d.get('plan', 'Pro')} plan is active"
        lead = f"Welcome to ProAds {d.get('plan', 'Pro')}."
        body = ["Your plan is active. You can publish campaigns and they keep running.",
                "Sales tracking and sending real purchases to Meta and TikTok keep working as before."]
    elif kind == "payment_failed":
        amt = f" of {money(d['amount'], d.get('currency') or 'AED')}" if d.get("amount") else ""
        subject = f"{name}: your ProAds payment didn't go through"
        lead = f"Your ProAds payment{amt} didn't go through."
        body = ["Please update your card on Plan & billing. We'll try the payment again automatically.",
                "If it keeps failing, campaigns made through ProAds will be paused."]
        button = "Update card"
    elif kind == "plan_ended":
        subject = f"{name}: your ProAds plan has ended"
        lead = "Your ProAds plan has ended."
        body = ["Campaigns made through ProAds are paused and new ones can't be published.",
                "Sales tracking and sending real purchases to Meta and TikTok keep working for free. Add a plan any time to run ads again."]
        button = "Add a plan"
    elif kind == "tt_balance":
        subject = f"{name}: TikTok ads stopped, balance is too low"
        lead = "Your TikTok ads stopped: not enough balance."
        body = [f"TikTok says the ad account doesn't have enough balance{(' for ' + d['campaign']) if d.get('campaign') else ''}.",
                "Add funds in TikTok Ads Manager (Payment / Add balance). Ads start again by themselves once the balance is topped up."]
    elif kind == "tt_learning_on":
        subject = f"{name}: TikTok is learning who buys"
        lead = "TikTok started learning for your ads."
        body = [f"{d.get('adgroup') or 'Your ad group'} is in the learning phase. TikTok tests who buys, using your real paid orders from ProAds.",
                "For the next 7 days: don't change budget, targeting or videos. Edits restart learning.",
                "Learning finishes at about 50 purchases in 7 days."]
    elif kind == "tt_learning_off":
        subject = f"{name}: TikTok learning is turned off"
        lead = "Learning is off on one of your TikTok ad groups."
        body = [f"{d.get('adgroup') or 'An ad group'} was created with Skip Learning Phase on, so TikTok doesn't learn who buys.",
                "Fix: in TikTok Ads Manager turn Skip Learning Phase off, or duplicate the ad group with it off and pause the old one."]
    elif kind == "tt_learning_done":
        subject = f"{name}: TikTok has enough purchases to finish learning"
        lead = "TikTok has enough purchases to finish learning."
        body = [f"TikTok accepted {d.get('purchases', 50)} purchases from ProAds in the last 7 days, about what it needs to finish learning.",
                "Now you can raise the budget slowly: about 20% every 2 to 3 days."]
    else:
        raise ValueError(kind)
    path = "billing" if kind in ("renews", "plan_started", "payment_failed", "plan_ended") else "home"
    text, page = _page(name, lead, body, app_url, button=button, why=why, path=path,
                       link=d.get("url", "") if kind in ("receipt", "news") else "")
    return subject, text, page


def sample(kind: str) -> dict:
    """Made-up numbers for the admin's test email."""
    return {
        "orders": {"currency": "AED", "orders": [{"order": "10024", "amount": 280.0, "from": "TikTok ad"},
                                                   {"order": "10025", "amount": 150.0, "from": "Direct / unknown"}]},
        "weekly": {"currency": "AED", "orders": 6, "kept": 1480.0, "refunds": 150.0, "spend": 620.0, "cost_per_order": 103.33,
                   "from": "16 Nov", "to": "22 Nov"},
        "live": {"campaign": "BYNAWI GCC Purchase Conversions", "platform": "TikTok"},
        "paused": {"campaign": "BYNAWI GCC Purchase Conversions", "platform": "TikTok", "reason": "billing"},
        "renews": {"plan": "Pro", "days_left": 3, "ends": "26 Nov 2026"},
        "plan_started": {"plan": "Pro"},
        "payment_failed": {"amount": 299.0, "currency": "AED"},
        "plan_ended": {},
        "welcome": {}, "connected": {"platform": "Shopify"},
        "news": {"title": "How to read your TikTok results", "summary": "Three numbers that matter.", "url": "https://pro.ezdevsoft.net/news"},
        "receipt": {"amount": 299.0, "currency": "AED", "number": "PROADS-0001", "what": "ProAds Pro, monthly",
                    "date": "28 Nov 2026", "url": "https://invoice.stripe.com/i/sample", "pdf": ""},
        "tt_balance": {"campaign": "BYNAWI GCC Purchase Conversions"},
        "tt_learning_on": {"adgroup": "GCC Broad - Verified Sales"},
        "tt_learning_off": {"adgroup": "GCC Broad - Verified Sales"},
        "tt_learning_done": {"purchases": 52},
    }[kind]
