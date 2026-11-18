"""Client results shown on the public website (pro.ezdevsoft.net), managed by the ProAds admin.

A result shows only when it is marked published AND the admin confirms the client approved it. Each one carries
where its numbers come from and the period they cover, so a visitor (or an AI reading the page) can see what was
measured, by whom and when. Numbers are typed by the admin from the client's own ProAds account.
"""
from __future__ import annotations

import json
import time
from typing import Optional

COLS = ("id", "business", "industry", "country", "period", "summary", "metrics", "quote", "quote_by", "source",
        "approved", "published", "position", "updated_at")


class ResultStore:
    def __init__(self, db):
        self.db = db
        self.db.execute("""CREATE TABLE IF NOT EXISTS site_results (id INTEGER PRIMARY KEY AUTOINCREMENT, business TEXT NOT NULL,
            industry TEXT, country TEXT, period TEXT NOT NULL, summary TEXT, metrics TEXT NOT NULL, quote TEXT, quote_by TEXT,
            source TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)""")
        self.db.commit()

    def _row(self, r) -> dict:
        d = dict(zip(COLS, r))
        d["metrics"] = json.loads(d["metrics"] or "[]")
        d["approved"], d["published"] = bool(d["approved"]), bool(d["published"])
        return d

    def list(self, public_only: bool = False) -> list[dict]:
        q = "SELECT " + ",".join(COLS) + " FROM site_results"
        if public_only:
            q += " WHERE published=1 AND approved=1"
        return [self._row(r) for r in self.db.execute(q + " ORDER BY position, updated_at DESC")]

    def save(self, b: dict, rid: Optional[int] = None) -> dict:
        t = lambda k, n: str(b.get(k) or "").strip()[:n]
        business, period, source = t("business", 80), t("period", 60), t("source", 160)
        if not business:
            raise ValueError("Add the business name.")
        if not period:
            raise ValueError("Add the period the numbers cover, e.g. Jan–Oct 2026.")
        if not source:
            raise ValueError("Say where the numbers come from, e.g. 'Measured in ProAds from the clinic's Meta leads export'.")
        metrics = []
        for m in (b.get("metrics") or [])[:6]:
            label, value = str((m or {}).get("label") or "").strip()[:60], str((m or {}).get("value") or "").strip()[:24]
            if label and value:
                metrics.append({"label": label, "value": value})
        if not metrics:
            raise ValueError("Add at least one number, like 614 / buyers found.")
        published, approved = bool(b.get("published")), bool(b.get("approved"))
        if published and not approved:
            raise ValueError("Only publish a result the client has approved.")
        vals = (business, t("industry", 60), t("country", 40), period, t("summary", 400), json.dumps(metrics),
                t("quote", 300), t("quote_by", 80), source, int(approved), int(published), int(b.get("position") or 0), int(time.time()))
        if rid:
            self.db.execute("UPDATE site_results SET business=?, industry=?, country=?, period=?, summary=?, metrics=?, quote=?, "
                            "quote_by=?, source=?, approved=?, published=?, position=?, updated_at=? WHERE id=?", (*vals, rid))
        else:
            rid = self.db.execute("INSERT INTO site_results (business, industry, country, period, summary, metrics, quote, quote_by, "
                                  "source, approved, published, position, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", vals).lastrowid
        self.db.commit()
        return self._row(self.db.execute("SELECT " + ",".join(COLS) + " FROM site_results WHERE id=?", (rid,)).fetchone())

    def delete(self, rid: int) -> None:
        self.db.execute("DELETE FROM site_results WHERE id=?", (rid,))
        self.db.commit()


def html_section(items: list[dict], esc) -> str:
    """The Results section of the landing page (empty when there's nothing approved and published)."""
    if not items:
        return ""
    cards = []
    for r in items:
        nums = "".join(f'<div style="display:flex;flex-direction:column"><b style="font-size:26px">{esc(m["value"])}</b>'
                       f'<span class="small" style="color:var(--muted)">{esc(m["label"])}</span></div>' for m in r["metrics"])
        who = " · ".join(esc(x) for x in (r["industry"], r["country"]) if x)
        quote = (f'<blockquote style="margin:4px 0 0;font-style:italic">“{esc(r["quote"])}”'
                 + (f' <span class="small">— {esc(r["quote_by"])}</span>' if r["quote_by"] else "") + "</blockquote>") if r["quote"] else ""
        cards.append(
            f'<article class="card" style="display:flex;flex-direction:column;gap:10px">'
            f'<h3 style="margin:0">{esc(r["business"])}</h3>'
            + (f'<span class="small" style="color:var(--muted)">{who}</span>' if who else "")
            + (f'<p style="margin:0">{esc(r["summary"])}</p>' if r["summary"] else "")
            + f'<div style="display:flex;gap:22px;flex-wrap:wrap">{nums}</div>{quote}'
            f'<span class="small" style="color:var(--muted)">{esc(r["period"])} · Source: {esc(r["source"])}</span></article>')
    return ('\n    <section class="block" id="results" dir="ltr" lang="en" aria-labelledby="results-h">\n      <h2 id="results-h">Results from real businesses</h2>\n'
            '      <p class="sub">Shared with each business\'s permission. Every number says the period it covers and where it was measured.</p>\n'
            f'      <div class="grid3">{"".join(cards)}</div>\n    </section>\n')


def text_lines(items: list[dict]) -> list[str]:
    out = []
    for r in items:
        nums = "; ".join(f'{m["value"]} {m["label"]}' for m in r["metrics"])
        out.append(f"- {r['business']}" + (f" ({r['industry']})" if r["industry"] else "") + f", {r['period']}: {nums}. Source: {r['source']}.")
    return out
