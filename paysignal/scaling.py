"""Budget Intelligence with human-approved scaling.

ProAds RECOMMENDS budget changes from verified profit; nothing changes in Meta until the owner approves that exact change.

Rules (all must hold for a scale-up):
  * judged on VERIFIED sales from the campaign's own ad links (refunds out, and recent sales discounted by this
    business's own refund rate), never on Meta's reported purchases
  * confidently profitable: cost per verified sale under the target, and under break-even with >= 90% confidence
  * not in Meta's learning phase (a budget change restarts it); LEARNING_LIMITED is fine, more budget can help
  * at least 7 days since the last budget change ProAds made (each change needs time to show its effect)
  * one step of at most +20% (larger jumps tend to restart Meta's learning)
  * never above the owner's maximum total daily budget; with no maximum set, scale-ups are blocked
  * lifetime budgets are never touched
Losing campaigns get "reduce 20%" (probably losing) or "pause" (confidently losing). A scale-up that made things
worse gets "undo". Bids, audiences and ads are never changed here.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

from . import engines

STEP_UP = 0.20
STEP_DOWN = 0.20
COOLDOWN_DAYS = 7
WINDOW_DAYS = 14
MIN_CONF = 0.9
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "PYG", "ISK", "HUF", "TWD", "COP", "IDR"}   # Meta budget offset 1


def offset(currency: str) -> int:
    return 1 if (currency or "").upper() in ZERO_DECIMAL else 100


def rec_id(campaign_id: str, action: str, current: Optional[float], new: Optional[float]) -> str:
    return hashlib.sha256(f"{campaign_id}|{action}|{current}|{new}".encode()).hexdigest()[:16]


def recommend(c: dict, econ: Optional[dict], cap_total: Optional[float], total_now: float,
              last_change: Optional[dict], now: Optional[int] = None) -> dict:
    """c: {campaign_id, name, status, budget: {level, daily, lifetime, adsets:[...]}, learning: [statuses],
           spend, sales_settled, sales_recent, refund_rate, tracked, currency}
    econ: engines.economics() (break_even_cpa, target_cpa) in the same currency as spend."""
    now = int(now or time.time())
    out = {"campaign_id": c["campaign_id"], "name": c.get("name", ""), "action": "hold", "reasons": [], "blockers": [],
           "current_daily": c["budget"].get("daily"), "new_daily": None, "currency": c.get("currency", "")}
    b = c["budget"]
    if c.get("status") not in ("ACTIVE", "IN_PROCESS", "WITH_ISSUES"):
        out["reasons"].append("The campaign isn't running.")
        return _seal(out)
    if b.get("lifetime"):
        out["reasons"].append("It uses a lifetime budget. ProAds only adjusts daily budgets.")
        return _seal(out)
    if not b.get("daily"):
        out["reasons"].append("No daily budget found on the campaign or its ad sets.")
        return _seal(out)
    if not econ:
        out["blockers"].append("Fill in the Profit engine (price, costs, desired profit) so ProAds knows what a sale is worth.")
        return _seal(out)
    if not c.get("tracked"):
        out["blockers"].append("Your sales data doesn't show which ad each sale came from yet, so profit can't be verified. "
                               "ProAds won't move budgets on Meta's reported numbers.")
        return _seal(out)
    if c["spend"] <= 0:
        out["blockers"].append(f"ProAds has no recorded spend for this campaign in the last {WINDOW_DAYS} days, so it can't "
                               "judge it. Spend syncs from Meta every few minutes; if this stays, reconnect Facebook.")
        return _seal(out)
    if c["spend"] < econ["break_even_cpa"]:
        out["reasons"].append("Too little spend so far to judge (less than the cost of one break-even sale).")
        return _seal(out)
    sales = c["sales_settled"] + c["sales_recent"] * (1 - min(0.9, max(0.0, c.get("refund_rate") or 0)))
    v = engines.verdict(c["spend"], sales, econ["target_cpa"], econ["break_even_cpa"])
    out["verdict"] = {**v, "spend": round(c["spend"], 2), "verified_sales": round(sales, 1), "window_days": WINDOW_DAYS,
                      "settled_sales": c["sales_settled"], "recent_sales": c["sales_recent"],
                      "refund_rate_used": round(c.get("refund_rate") or 0, 3)}
    since_change = (now - last_change["at"]) / 86400 if last_change else None
    cooling = since_change is not None and since_change < COOLDOWN_DAYS

    # did our last scale-up hurt? (compare cost per verified sale before vs after)
    if last_change and last_change.get("action") == "scale_up" and not cooling and c.get("after_change"):
        a = c["after_change"]
        va = engines.verdict(a["spend"], a["sales"], econ["target_cpa"], econ["break_even_cpa"])
        if va["verdict"] == "losing":
            out.update(action="undo", new_daily=last_change["before_daily"])
            out["reasons"].append(f"Since the budget went up on {time.strftime('%d %b', time.gmtime(last_change['at']))}, "
                                  "the campaign is confidently losing money. Go back to the previous budget.")
            return _seal(out)

    if v["verdict"] == "losing" and (v.get("confidence") or 0) >= MIN_CONF:
        out.update(action="pause")
        out["reasons"].append(f"Confidently losing money ({sure(v['confidence'])} sure) on verified sales over "
                              f"{WINDOW_DAYS} days. Pausing stops the loss; budget can go to a profitable campaign.")
        return _seal(out)
    if v["verdict"] == "likely_losing":
        if cooling:
            out["reasons"].append(f"Probably losing money, but the budget changed {days_ago(since_change)}. Wait for the change to settle.")
            return _seal(out)
        out.update(action="scale_down", new_daily=_round(b["daily"] * (1 - STEP_DOWN), c.get("currency")))
        out["reasons"].append("Probably losing money on verified sales. Reduce the budget 20% while you test a fix.")
        return _seal(out)
    if v["verdict"] != "profitable":
        out["reasons"].append({"too_early": "Not enough verified sales yet to judge.",
                               "break_even": "Around break-even: more budget would add sales but not profit.",
                               "no_room": "Every sale from ads loses money at the current price and costs."}
                              .get(v["verdict"], v.get("label", "")))
        return _seal(out)
    if (v.get("confidence") or 0) < MIN_CONF:
        out["reasons"].append(f"Making money, but only {int((v.get('confidence') or 0) * 100)}% sure. ProAds waits for 90%.")
        return _seal(out)
    # profitable with confidence: can it grow?
    if any(s == "LEARNING" for s in c.get("learning") or []):
        out["reasons"].append("Profitable, but Meta is still in its learning phase. A budget change now would restart it.")
        return _seal(out)
    if cooling:
        out["reasons"].append(f"Profitable. The budget was last changed {days_ago(since_change)}; ProAds waits "
                              f"{COOLDOWN_DAYS} days between changes so each one can show its effect.")
        return _seal(out)
    new = _round(b["daily"] * (1 + STEP_UP), c.get("currency"))
    if cap_total is None:
        out["blockers"].append("Set your maximum total daily budget first. ProAds never raises budgets without a limit you chose.")
        out["would"] = {"action": "scale_up", "new_daily": new}
        return _seal(out)
    room = cap_total - total_now
    if room <= 0:
        out["reasons"].append("Profitable, but your campaigns are already at the maximum daily budget you set.")
        return _seal(out)
    new = min(new, _round(b["daily"] + room, c.get("currency")))
    if new <= b["daily"]:
        out["reasons"].append("Profitable, but there's no room under your maximum daily budget.")
        return _seal(out)
    out.update(action="scale_up", new_daily=new)
    out["reasons"].append(f"Confidently profitable ({sure(v['confidence'])} sure) on verified sales: cost per sale "
                          f"{v['cpa']:,.2f} against your target {econ['target_cpa']:,.2f}. Raise the budget one step "
                          f"(+{(new / b['daily'] - 1):.0%}).")
    out["reasons"].append("After approval ProAds waits 7 days, then checks the cost per sale held. If it didn't, it suggests going back.")
    return _seal(out)


def days_ago(d: float) -> str:
    return "today" if d < 1 else ("yesterday" if d < 2 else f"{int(d)} days ago")


def sure(conf: float) -> str:
    return "over 99%" if conf >= 0.995 else f"{int(conf * 100)}%"


def _round(x: float, currency: Optional[str]) -> float:
    return float(round(x)) if offset(currency or "") == 1 else round(x, 2)


def _seal(out: dict) -> dict:
    out["rec_id"] = rec_id(out["campaign_id"], out["action"], out["current_daily"], out["new_daily"])
    return out


def split_budget(budget: dict, new_daily: float, currency: str) -> list[tuple[str, int]]:
    """(object id, new daily budget in Meta's minor units). Campaign budget if the campaign has one, otherwise each
    ad set scaled by the same factor."""
    off = offset(currency)
    if budget["level"] == "campaign":
        return [(budget["campaign_id"], int(round(new_daily * off)))]
    f = new_daily / budget["daily"]
    return [(a["id"], max(100 if off == 100 else 1, int(round(a["daily"] * f * off)))) for a in budget["adsets"] if a.get("daily")]


class ScalingStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS budget_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
        campaign_id TEXT NOT NULL, action TEXT NOT NULL, at INTEGER NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL,
        before_daily REAL, after_daily REAL, approved_by TEXT, reasons TEXT, verified INTEGER NOT NULL DEFAULT 0,
        undone_by INTEGER);
    CREATE TABLE IF NOT EXISTS scaling_settings (client_id TEXT PRIMARY KEY, max_daily REAL);
    """

    def __init__(self, db):
        self.db = db
        self.db.executescript(self.SCHEMA)
        self.db.commit()

    def cap(self, client_id: str) -> Optional[float]:
        r = self.db.execute("SELECT max_daily FROM scaling_settings WHERE client_id=?", (client_id,)).fetchone()
        return r[0] if r else None

    def set_cap(self, client_id: str, value: Optional[float]) -> None:
        self.db.execute("INSERT INTO scaling_settings (client_id, max_daily) VALUES (?,?) ON CONFLICT(client_id)"
                        " DO UPDATE SET max_daily=excluded.max_daily", (client_id, value))
        self.db.commit()

    def record(self, client_id: str, campaign_id: str, action: str, before: dict, after: dict, before_daily, after_daily,
               approved_by: str, reasons: list, verified: bool, at: Optional[int] = None, undoes: Optional[int] = None) -> int:
        cur = self.db.execute(
            "INSERT INTO budget_changes (client_id, campaign_id, action, at, before, after, before_daily, after_daily,"
            " approved_by, reasons, verified) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (client_id, campaign_id, action, int(at or time.time()), json.dumps(before), json.dumps(after), before_daily,
             after_daily, approved_by, json.dumps(reasons), int(verified)))
        if undoes:
            self.db.execute("UPDATE budget_changes SET undone_by=? WHERE id=?", (cur.lastrowid, undoes))
        self.db.commit()
        return cur.lastrowid

    def last(self, client_id: str, campaign_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT id, action, at, before, before_daily, after_daily, undone_by FROM budget_changes"
                            " WHERE client_id=? AND campaign_id=? ORDER BY at DESC, id DESC LIMIT 1",
                            (client_id, campaign_id)).fetchone()
        if not r:
            return None
        return {"id": r[0], "action": r[1], "at": r[2], "before": json.loads(r[3]), "before_daily": r[4],
                "after_daily": r[5], "undone_by": r[6]}

    def history(self, client_id: str, limit: int = 30) -> list[dict]:
        rows = self.db.execute("SELECT id, campaign_id, action, at, before_daily, after_daily, approved_by, reasons, verified,"
                               " undone_by FROM budget_changes WHERE client_id=? ORDER BY at DESC, id DESC LIMIT ?",
                               (client_id, limit)).fetchall()
        keys = ("id", "campaign_id", "action", "at", "before_daily", "after_daily", "approved_by", "reasons", "verified", "undone_by")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["reasons"] = json.loads(d["reasons"] or "[]")
            out.append(d)
        return out
