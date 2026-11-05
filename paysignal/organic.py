"""Organic intelligence: which posts bring people who PAY, and what to post next.

Measures every post by real money per 1,000 views (not likes), and finds sales
that normal tracking misses:

  direct     - buyer came through the post's tagged link or promo code
  returning  - a customer the post first brought in, buying again later without a link
  halo       - extra unlinked sales on the days after a post, above the normal level
               (an estimate, shown separately)

Then learns which formats, hooks, offers and posting times make money, and
recommends what to post next, balancing proven winners with things worth testing.
"""
from __future__ import annotations

import csv
import io
import itertools
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .attribution import resolve
from .engine import Engine
from .identity import customer_key

ATTRIBUTES = ("format", "hook", "offer")
PRIOR_KVIEWS = 5.0   # shrinkage strength: a pattern needs ~5k views before its own data dominates


@dataclass
class Post:
    post_id: str
    platform: str
    posted_at: datetime
    format: str = "unknown"      # reel, story, carousel, image, video, live
    hook: str = "unknown"        # e.g. before_after, testimonial, tutorial, offer, behind_scenes
    offer: str = "none"          # none, discount, free_gift, bundle
    topic: str = ""
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    link_clicks: int = 0
    caption: str = ""            # used by the labeller
    media_type: str = ""         # as exported by the platform, e.g. REELS, STORY, CAROUSEL_ALBUM
    label_source: str = "manual" # manual | rules | ai

    @property
    def engagement_rate(self) -> float:
        return (self.likes + self.comments + self.shares + self.saves) / self.views if self.views else 0.0


def load_posts_csv(text: str) -> tuple[list[Post], list[str]]:
    """Columns: post_id, platform, posted_at, views (required); format, hook, offer, topic,
    likes, comments, shares, saves, link_clicks, caption, media_type (optional).
    Export from Meta Business Suite / TikTok analytics, or fill in by hand.
    Leave format/hook/offer blank to have the labeller fill them in from the caption."""
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.strip().lower() for c in reader.fieldnames or []}
    missing = {"post_id", "platform", "posted_at", "views"} - cols
    if missing:
        return [], [f"missing columns: {', '.join(sorted(missing))}"]
    posts, errors = [], []
    for n, raw in enumerate(reader, start=2):
        r = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        try:
            dt = datetime.fromisoformat(r["posted_at"].replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            num = lambda k: int(float(r.get(k) or 0))
            posts.append(Post(r["post_id"], r["platform"].lower(), dt,
                              (r.get("format") or "unknown").lower(), (r.get("hook") or "unknown").lower(),
                              (r.get("offer") or "unknown").lower(), r.get("topic", ""),
                              num("views"), num("likes"), num("comments"), num("shares"),
                              num("saves"), num("link_clicks"),
                              caption=r.get("caption", ""), media_type=r.get("media_type", "")))
        except (ValueError, KeyError) as e:
            errors.append(f"row {n}: {e}")
    return posts, errors


@dataclass
class Credit:
    post_id: Optional[str]
    how: str              # direct | returning | unattributed
    amount: float
    refunded: float
    real_value: float
    occurred_at: datetime


@dataclass
class PostResult:
    post: Post
    direct_orders: int = 0
    returning_orders: int = 0
    gross: float = 0.0
    refunds: float = 0.0
    real_value: float = 0.0       # direct + returning (measured)
    halo_value: float = 0.0       # estimated extra unlinked value
    tags: list[str] = field(default_factory=list)

    @property
    def orders(self) -> int:
        return self.direct_orders + self.returning_orders

    @property
    def total_value(self) -> float:
        return self.real_value + self.halo_value

    @property
    def value_per_1k(self) -> float:
        return 1000 * self.total_value / self.post.views if self.post.views else 0.0

    @property
    def buyer_rate(self) -> float:
        return self.direct_orders / self.post.link_clicks if self.post.link_clicks else 0.0


class OrganicEngine:
    def __init__(self, engine: Engine, posts: list[Post], halo_window_days: int = 3,
                 baseline_days: int = 14, now: Optional[datetime] = None):
        self.engine = engine
        self.now = now or datetime.now(timezone.utc)
        self.posts = {p.post_id: p for p in posts}
        self.halo_window = halo_window_days
        self.baseline_days = baseline_days
        self._promo_map = engine.promo_map

    # ---------- 1. credit every purchase ----------
    def credits(self) -> list[Credit]:
        rows = sorted(self.engine.scored_purchases(), key=lambda r: r[0].occurred_at)
        first_post: dict[str, str] = {}   # customer -> post that first brought them in
        out = []
        for p, refunded, score in rows:
            ck = customer_key(p.email, p.phone, p.customer_id)
            t = resolve(p, self._promo_map)
            post_id, how = None, "unattributed"
            if t.content in self.posts:
                if ck and ck in first_post and first_post[ck] != t.content:
                    post_id, how = t.content, "direct"      # new post re-engaged them
                elif ck and ck in first_post:
                    post_id, how = t.content, "returning"
                else:
                    post_id, how = t.content, "direct"
                if ck:
                    first_post.setdefault(ck, t.content)
            elif ck and ck in first_post:
                post_id, how = first_post[ck], "returning"
            out.append(Credit(post_id, how, p.amount, refunded,
                              self._realized_value(p, refunded, score), p.occurred_at))
        return out

    def _realized_value(self, p, refunded: float, score) -> float:
        """Judge past posts by what actually happened, not by prediction:
        - orders past the return window: margin x share NOT refunded
        - recent orders: margin x predicted keep-chance
        No future-value bonus here: repeat orders are counted for real as 'returning'."""
        matured = self.now - p.occurred_at >= self.engine.scorer.return_window
        keep = (1 - min(refunded / p.amount, 1.0)) if (matured and p.amount) else (1 - score.return_risk)
        return round(score.margin * keep, 2)

    # ---------- 2. halo: unlinked sales lift after posts ----------
    def halo(self, credits: list[Credit]) -> dict[str, float]:
        """Excess unattributed value on each day above its trailing median, shared among
        posts live in that window, weighted by views and decaying day by day."""
        daily: dict[date, float] = defaultdict(float)
        for c in credits:
            if c.how == "unattributed":
                daily[c.occurred_at.date()] += c.real_value
        if not daily or not self.posts:
            return {}
        start = min(min(daily), min(p.posted_at.date() for p in self.posts.values()))
        end = max(daily)
        days = [start + timedelta(d) for d in range((end - start).days + 1)]
        series = [daily.get(d, 0.0) for d in days]

        out: dict[str, float] = defaultdict(float)
        for i, d in enumerate(days):
            if i < self.baseline_days:
                continue
            baseline = statistics.median(series[i - self.baseline_days:i])
            excess = series[i] - baseline
            if excess <= 0:
                continue
            weights = {}
            for p in self.posts.values():
                age = (d - p.posted_at.date()).days
                if 0 <= age < self.halo_window:
                    weights[p.post_id] = p.views * (0.5 ** age)
            total = sum(weights.values())
            for pid, w in weights.items():
                out[pid] += excess * w / total
        return dict(out)

    # ---------- 3. per-post scoreboard ----------
    def scoreboard(self) -> list[PostResult]:
        credits = self.credits()
        res = {pid: PostResult(p) for pid, p in self.posts.items()}
        for c in credits:
            if c.post_id in res:
                r = res[c.post_id]
                r.direct_orders += c.how == "direct"
                r.returning_orders += c.how == "returning"
                r.gross += c.amount
                r.refunds += c.refunded
                r.real_value += c.real_value
        for pid, v in self.halo(credits).items():
            if pid in res:
                res[pid].halo_value = v

        results = list(res.values())
        self._tag(results)
        return sorted(results, key=lambda r: r.value_per_1k, reverse=True)

    @staticmethod
    def _tag(results: list[PostResult]) -> None:
        viewed = [r for r in results if r.post.views >= 500]
        if len(viewed) < 4:
            return
        eng = sorted(r.post.engagement_rate for r in viewed)
        val = sorted(r.value_per_1k for r in viewed)
        hi_eng, lo_eng = eng[int(len(eng) * 0.7)], eng[int(len(eng) * 0.3)]
        hi_val, lo_val = val[int(len(val) * 0.7)], val[int(len(val) * 0.3)]
        for r in viewed:
            if r.value_per_1k >= hi_val:
                r.tags.append("PAYER POST")
            if r.post.engagement_rate >= hi_eng and r.value_per_1k <= lo_val:
                r.tags.append("WATCHERS ONLY")       # liked a lot, nobody pays
            if r.post.engagement_rate <= lo_eng and r.value_per_1k >= hi_val:
                r.tags.append("QUIET SELLER")        # few likes, real buyers
            if r.gross and r.refunds / r.gross > 0.3:
                r.tags.append("HIGH REFUNDS")

    # ---------- 4. what kind of content makes money ----------
    @staticmethod
    def _rate_table(results: list[PostResult], key) -> dict[str, dict]:
        total_v = sum(r.total_value for r in results)
        total_k = sum(r.post.views for r in results) / 1000
        g = total_v / total_k if total_k else 0.0
        groups: dict[str, list[PostResult]] = defaultdict(list)
        for r in results:
            groups[key(r)].append(r)
        # Comparing many options at once makes a lucky one likely, so the bar rises
        # with the number of options (10% chance of any false alarm per question).
        z_needed = statistics.NormalDist().inv_cdf(1 - 0.10 / (2 * max(len(groups), 1)))
        table = {}
        for k, rs in groups.items():
            v = sum(r.total_value for r in rs)
            kv = sum(r.post.views for r in rs) / 1000
            rate = (v + PRIOR_KVIEWS * g) / (kv + PRIOR_KVIEWS)   # shrunk toward the average
            per_post = [r.value_per_1k for r in rs]
            spread = statistics.pstdev(per_post) if len(per_post) > 1 else g
            # how sure: distance from average in units of standard error
            se = spread / (len(rs) ** 0.5) if spread else g
            z = (rate - g) / se if se else 0.0
            table[k] = dict(posts=len(rs), kviews=kv, rate=rate, lift=(rate / g - 1) if g else 0.0,
                            spread=spread, confident=abs(z) >= z_needed and len(rs) >= 5)
        return table

    def patterns(self, results: list[PostResult] | None = None) -> dict[str, dict[str, dict]]:
        results = results or self.scoreboard()
        return {a: self._rate_table(results, lambda r, a=a: getattr(r.post, a)) for a in ATTRIBUTES}

    def best_times(self, results: list[PostResult] | None = None) -> dict[str, dict[str, dict]]:
        results = results or self.scoreboard()

        def slot(r):
            h = r.post.posted_at.hour
            return "morning 6-12" if 6 <= h < 12 else "afternoon 12-17" if h < 17 else \
                   "evening 17-22" if h < 22 else "night 22-6"
        return {"day": self._rate_table(results, lambda r: r.post.posted_at.strftime("%A")),
                "time": self._rate_table(results, slot)}

    # ---------- 5. what to post next ----------
    def recommend(self, n: int = 3, draws: int = 4000, seed: int = 0) -> list[dict]:
        """Thompson sampling over format x hook x offer. Each draw samples every pattern's
        money-per-view from its uncertainty, combines them, and picks the best combo.
        Combos that win often are proven; rarely-tested combos can still win and get
        flagged as experiments."""
        results = self.scoreboard()
        pats = self.patterns(results)
        total_k = sum(r.post.views for r in results) / 1000
        g = sum(r.total_value for r in results) / total_k if total_k else 0.0
        if g <= 0:
            return []
        rng = random.Random(seed)
        options = {a: list(t.keys()) for a, t in pats.items()}
        combos = list(itertools.product(*(options[a] for a in ATTRIBUTES)))
        wins: dict[tuple, int] = defaultdict(int)
        for _ in range(draws):
            sample = {a: {k: max(rng.gauss(s["rate"], s["spread"] / (s["posts"] ** 0.5 + 1)), 0.01)
                          for k, s in pats[a].items()} for a in ATTRIBUTES}
            best = max(combos, key=lambda c: g * _prod(sample[a][v] / g for a, v in zip(ATTRIBUTES, c)))
            wins[best] += 1
        tried = {tuple(getattr(r.post, a) for a in ATTRIBUTES) for r in results}
        out = []
        for combo, w in sorted(wins.items(), key=lambda kv: kv[1], reverse=True)[:n]:
            expected = g * _prod(pats[a][v]["rate"] / g for a, v in zip(ATTRIBUTES, combo))
            out.append(dict(zip(ATTRIBUTES, combo), chance_best=w / draws,
                            expected_per_1k=expected, status="proven" if combo in tried else "experiment"))
        return out

    # ---------- 6. ready-made ads ----------
    def boost_candidates(self, results: list[PostResult] | None = None, min_views: int = 1500,
                         top: int = 3) -> list[PostResult]:
        """Posts that already turn views into paying customers: promote these first when ads start."""
        results = results or self.scoreboard()
        ok = [r for r in results if r.post.views >= min_views and r.orders >= 3
              and (not r.gross or r.refunds / r.gross <= 0.25)]
        return sorted(ok, key=lambda r: r.value_per_1k, reverse=True)[:top]

    # ---------- report ----------
    def report(self, currency: str = "AED", top: int = 8) -> str:
        results = self.scoreboard()
        credits = self.credits()
        L = []
        total = sum(c.real_value for c in credits)
        linked = sum(c.real_value for c in credits if c.how == "direct")
        returning = sum(c.real_value for c in credits if c.how == "returning")
        halo = sum(r.halo_value for r in results)
        L.append("WHERE THE MONEY CAME FROM (real value)")
        L.append(f"  through a post's link or code   {linked:>12,.0f} {currency}  ({linked / total:.0%})")
        L.append(f"  returning customers of a post   {returning:>12,.0f} {currency}  ({returning / total:.0%})")
        L.append(f"  extra sales after posts (est.)  {halo:>12,.0f} {currency}  ({halo / total:.0%})")
        L.append(f"  normal tracking would only see the first line.")

        L.append(f"\nTOP POSTS BY MONEY PER 1,000 VIEWS")
        L.append(f"  {'post':<16}{'type':<32}{'views':>8}{'eng%':>6}{'orders':>7}{'per 1k':>8}  tags")
        for r in results[:top]:
            p = r.post
            L.append(f"  {p.post_id:<16}{p.format + '/' + p.hook + '/' + p.offer:<32}{p.views:>8,}"
                     f"{p.engagement_rate:>6.1%}{r.orders:>7}{r.value_per_1k:>8,.0f}  {', '.join(r.tags)}")
        watchers = [r for r in results if "WATCHERS ONLY" in r.tags]
        if watchers:
            L.append(f"\nWATCHERS-ONLY POSTS (lots of likes, almost no payers)")
            for r in watchers[:5]:
                L.append(f"  {r.post.post_id:<16}{r.post.format + '/' + r.post.hook:<26}"
                         f"eng {r.post.engagement_rate:.1%}   {r.value_per_1k:,.0f} {currency} per 1k views")

        def fmt(table, limit=None):
            items = sorted(table.items(), key=lambda kv: -kv[1]["rate"])[:limit]
            return "  |  ".join(f"{k} {v['lift']:+.0%}{'' if v['confident'] else '?'}" for k, v in items)

        L.append("\nWHAT MAKES MONEY (vs your average post;  ? = not enough evidence yet)")
        for a, table in self.patterns(results).items():
            L.append(f"  {a:<10}{fmt(table)}")
        bt = self.best_times(results)
        for a in ("time", "day"):
            if any(v["confident"] for v in bt[a].values()):
                L.append(f"  {'best ' + a:<10}{fmt(bt[a], 3)}")
            else:
                L.append(f"  {'best ' + a:<10}no clear pattern yet")

        L.append("\nPOST NEXT")
        for i, rec in enumerate(self.recommend(), 1):
            L.append(f"  {i}. {rec['format']} + {rec['hook']} + offer: {rec['offer']}   "
                     f"~{rec['expected_per_1k']:,.0f} {currency}/1k views, "
                     f"best in {rec['chance_best']:.0%} of simulations  [{rec['status']}]")

        boost = self.boost_candidates(results)
        if boost:
            L.append("\nPROMOTE THESE FIRST WHEN ADS START")
            for r in boost:
                L.append(f"  {r.post.post_id:<16}{r.value_per_1k:,.0f} {currency} per 1k views, "
                         f"{r.orders} orders, {r.refunds / r.gross if r.gross else 0:.0%} refunds")
        return "\n".join(L)


def _prod(xs) -> float:
    out = 1.0
    for x in xs:
        out *= x
    return out
