"""Morning summary: one email a day at about 8:00 in the business's time zone.

Yesterday's money really paid and orders (refunds taken off) against the same weekday a week before, the last 7 days
against the 7 before, ad spend and money back per 1 spent when the ad accounts are connected, what's waiting in
Follow-ups, and the single biggest money leak to fix. On by default for accounts with sales; one switch turns it off.
"""
from __future__ import annotations

import html
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

SEND_HOUR = 8


class DigestStore:
    def __init__(self, db):
        self.db = db
        self.db.execute("CREATE TABLE IF NOT EXISTS digest_settings (client_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,"
                        " last_sent TEXT)")
        self.db.commit()

    def get(self, client_id: str) -> dict:
        r = self.db.execute("SELECT enabled, last_sent FROM digest_settings WHERE client_id=?", (client_id,)).fetchone()
        return {"enabled": bool(r[0]) if r else True, "last_sent": r[1] if r else None}

    def set(self, client_id: str, enabled: Optional[bool] = None, last_sent: Optional[str] = None) -> dict:
        cur = self.get(client_id)
        e = cur["enabled"] if enabled is None else enabled
        l = cur["last_sent"] if last_sent is None else last_sent
        self.db.execute("INSERT OR REPLACE INTO digest_settings VALUES (?,?,?)", (client_id, int(e), l))
        self.db.commit()
        return self.get(client_id)


def local_day(tz: str, now: Optional[float] = None) -> tuple[date, int]:
    try:
        z = ZoneInfo(tz or "Asia/Dubai")
    except Exception:
        z = ZoneInfo("Asia/Dubai")
    t = datetime.fromtimestamp(now or time.time(), z)
    return t.date(), t.hour


def _pct(a: float, b: float) -> Optional[float]:
    return None if not b else round(a / b - 1, 4)


def build(purchases, refunded: Callable, today: date, tz: str = "Asia/Dubai") -> dict:
    """Numbers for the summary of the day before `today` (business time zone)."""
    try:
        z = ZoneInfo(tz or "Asia/Dubai")
    except Exception:
        z = ZoneInfo("Asia/Dubai")
    y = today - timedelta(days=1)
    by_day: dict[date, list] = {}
    for p in purchases:
        kept = p.amount - refunded(p)
        if kept <= 0.005:
            continue
        d = p.occurred_at.astimezone(z).date()
        if y - timedelta(days=14) < d <= y:
            row = by_day.setdefault(d, [0, 0.0])
            row[0] += 1
            row[1] += kept
    get = lambda d: by_day.get(d, [0, 0.0])
    week = [get(y - timedelta(days=i)) for i in range(7)]
    prev = [get(y - timedelta(days=i)) for i in range(7, 14)]
    yo, ym = get(y)
    lo, lm = get(y - timedelta(days=7))
    return {"day": y.isoformat(), "orders": yo, "money": round(ym, 2), "vs_last_week": _pct(ym, lm), "last_week_money": round(lm, 2),
            "week_money": round(sum(m for _, m in week), 2), "week_orders": sum(o for o, _ in week),
            "week_change": _pct(sum(m for _, m in week), sum(m for _, m in prev))}


def _fmt(v: float, cur: str) -> str:
    return f"{v:,.0f} {cur}"


def _chg(x: Optional[float]) -> str:
    if x is None:
        return ""
    return f" ({'+' if x >= 0 else ''}{round(x * 100)}%)"


def render(name: str, cur: str, d: dict, spend: Optional[float], waiting: Optional[dict], leak: Optional[dict], app_url: str) -> tuple[str, str, str]:
    """Subject, plain text and HTML."""
    day = datetime.fromisoformat(d["day"]).strftime("%a %d %b")
    subject = f"{name}: {_fmt(d['money'], cur)} really paid yesterday{_chg(d['vs_last_week'])}"
    lines = [f"{day}: {_fmt(d['money'], cur)} really paid from {d['orders']} order{'s' if d['orders'] != 1 else ''}"
             f"{_chg(d['vs_last_week'])} vs the same day last week.",
             f"Last 7 days: {_fmt(d['week_money'], cur)} from {d['week_orders']} orders{_chg(d['week_change'])} vs the 7 before."]
    if spend:
        back = d["money"] / spend if spend else 0
        lines.append(f"Ad spend yesterday: {_fmt(spend, cur)}, so {back:.2f} back per 1 spent.")
    if waiting and any(waiting.values()):
        parts = [f"{waiting['checkout']} unpaid checkouts" if waiting.get("checkout") else None,
                 f"{waiting['winback']} buyers due to buy again" if waiting.get("winback") else None,
                 f"{waiting['review']} customers to ask for a review" if waiting.get("review") else None]
        lines.append("Waiting in Follow-ups: " + ", ".join(p for p in parts if p) + ".")
    if leak:
        lines.append(f"One thing to fix: {leak['title']}.")
    text = "Good morning,\n\n" + "\n".join("- " + l for l in lines) + \
        f"\n\nOpen ProAds: {app_url}\n\nSales count only when paid, refunds taken off. Turn this email off in ProAds: Connect."
    esc = html.escape
    lis = "".join(f'<li style="margin:0 0 8px">{esc(l)}</li>' for l in lines)
    body = (f'<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#141414">'
            f'<p style="font-size:13px;color:#5C5C5C;margin:0">ProAds · morning summary · {esc(name)}</p>'
            f'<p style="font-size:30px;font-weight:700;margin:8px 0 2px">{esc(_fmt(d["money"], cur))}</p>'
            f'<p style="margin:0 0 16px;color:#333">really paid on {esc(day)}{esc(_chg(d["vs_last_week"]))}</p>'
            f'<ul style="padding-left:18px;font-size:15px;line-height:1.5">{lis}</ul>'
            f'<p><a href="{esc(app_url)}" style="background:#5E9400;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:700">Open ProAds</a></p>'
            f'<p style="font-size:12px;color:#5C5C5C">Sales count only when paid, refunds taken off. Turn this email off in ProAds: Connect.</p></div>')
    return subject, text, body
