"""Emails about the free ad test: a day before it ends, the day it ends, and one follow-up 3 days later.

Only for businesses that started the ad test and have no paid plan. Each email goes once per test end date, so if the
admin gives more ad-test days, the reminders come again for the new end. Nothing is sent once a plan is paid.
"""
from __future__ import annotations

import html
import time
from typing import Optional

DAY = 86400
# kind, (from, to) seconds relative to the test end
WINDOWS = (("soon", -DAY, 0), ("ended", 0, 2 * DAY), ("followup", 3 * DAY, 6 * DAY))


class TrialMailStore:
    def __init__(self, db):
        self.db = db
        self.db.execute("CREATE TABLE IF NOT EXISTS trial_mails (client_id TEXT NOT NULL, kind TEXT NOT NULL, trial_end INTEGER NOT NULL,"
                        " sent_at INTEGER NOT NULL, PRIMARY KEY (client_id, kind, trial_end))")
        self.db.commit()

    def sent(self, client_id: str, kind: str, trial_end: int) -> bool:
        return bool(self.db.execute("SELECT 1 FROM trial_mails WHERE client_id=? AND kind=? AND trial_end=?",
                                    (client_id, kind, trial_end)).fetchone())

    def mark(self, client_id: str, kind: str, trial_end: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO trial_mails VALUES (?,?,?,?)", (client_id, kind, trial_end, int(time.time())))
        self.db.commit()


def due(access: dict, now: Optional[float] = None) -> Optional[str]:
    """Which email (if any) is due for this ad-test state."""
    if access.get("plan") or not access.get("trial_ends"):
        return None
    rel = (now or time.time()) - access["trial_ends"]
    for kind, lo, hi in WINDOWS:
        if lo <= rel < hi:
            return kind
    return None


def render(kind: str, name: str, prices: dict, fee: str, app_url: str, ends: str = "") -> tuple[str, str, str]:
    """Subject, text, HTML."""
    billing = app_url.rstrip("/") + "/app#/billing"
    plan_line = f"Pro is {prices['pro']} a month + {fee} of the ad spend on campaigns made through ProAds. Premium is {prices['premium']} a month + {fee}."
    if kind == "extended":
        subject = f"{name}: your free ad test was extended"
        lead = "Good news: your free ProAds ad test was extended."
        body = [f"You can publish and run campaigns until {ends or 'the new end date'}.",
                "Your sales tracking and sending real purchases to Meta and TikTok keep working as before."]
    elif kind == "left3":
        subject = f"{name}: 3 days left in your free ad test"
        lead = "3 days left in your free ProAds ad test."
        body = [f"Your ad test ends {('on ' + ends) if ends else 'in 3 days'}. Add a plan to keep your campaigns running without a break.",
                "Your sales tracking and sending real purchases to Meta and TikTok keep working for free."]
    elif kind == "soon":
        subject = f"{name}: your free ad test ends tomorrow"
        lead = "Your free ProAds ad test ends in less than a day." if not ends else f"Your free ProAds ad test ends on {ends}."
        body = ["After that, new campaigns can't be published and campaigns made through ProAds are paused until a plan is added.",
                "Your sales tracking, sending real purchases to Meta and TikTok, and the organic tools keep working for free."]
    elif kind == "ended":
        subject = f"{name}: your free ad test has ended"
        lead = "Your free ProAds ad test has ended."
        body = ["Campaigns made through ProAds are paused and new ones can't be published until a plan is added. Add a card and they can run again.",
                "Your sales tracking, sending real purchases to Meta and TikTok, and the organic tools keep working for free."]
    else:
        subject = f"{name}: pick up your ads where you left off"
        lead = "Your ProAds campaigns are still waiting."
        body = ["Your buyers, campaign history and settings are all kept. Add a plan and you can publish again in a minute.",
                "If something didn't work for you during the test, just reply to this email and tell us."]
    body.append(plan_line)
    text = f"Hello,\n\n{lead}\n\n" + "\n\n".join(body) + f"\n\nAdd your plan: {billing}\n\nProAds"
    esc = html.escape
    paras = "".join(f'<p style="margin:0 0 12px">{esc(b)}</p>' for b in body)
    page = (f'<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#141414">'
            f'<p style="font-size:13px;color:#5C5C5C;margin:0">ProAds · {esc(name)}</p>'
            f'<p style="font-size:22px;font-weight:700;margin:8px 0 14px">{esc(lead)}</p>{paras}'
            f'<p><a href="{esc(billing)}" style="background:#4A7600;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:700">Add your plan</a></p>'
            f'<p style="font-size:12px;color:#5C5C5C">You get this because your business started the free ad test on ProAds.</p></div>')
    return subject, text, page
