"""ProAds News: dated articles on the public site, written so Google can show them in Top Stories, Discover and
its AI answers. Pages are rendered on the server (crawlers read them without running scripts) and carry
NewsArticle structured data, a cover image, a sitemap, a Google News sitemap and an RSS feed.

Bodies use a small, safe text format (everything is escaped first):
  ## Heading        - bullet        1. numbered        **bold**        [text](https://link)
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

FONT = os.path.join(os.path.dirname(__file__), "web", "fonts", "DejaVuSans-Bold.ttf")
LOGO = os.path.join(os.path.dirname(__file__), "web", "proads-logo.png")
PUBLISHER = "ProAds by EzDevSoft"


def google_source_on() -> bool:
    """Show "Add ProAds on Google" buttons only once Google lists the site as a source (set PROADS_GOOGLE_SOURCE=1)."""
    return os.environ.get("PROADS_GOOGLE_SOURCE", "").strip().lower() in ("1", "true", "yes", "on")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:80].strip("-") or f"post-{int(time.time())}"


def _inline(text: str) -> str:
    t = html.escape(text, quote=True)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)",
               lambda m: f'<a href="{m.group(2)}"' + ('' if m.group(2).startswith("/") else ' rel="noopener" target="_blank"') + f">{m.group(1)}</a>", t)
    return t


def render_body(body: str) -> str:
    out, para, lst = [], [], None

    def flush():
        nonlocal para, lst
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para = []
        if lst:
            tag, items = lst
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{tag}>")
            lst = None

    for raw in (body or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("## "):
            flush()
            out.append("<h2>" + _inline(line[3:]) + "</h2>")
            continue
        m = re.match(r"^(?:[-*]|(\d+)\.)\s+(.*)$", line)
        if m:
            if para:
                flush()
            tag = "ol" if m.group(1) else "ul"
            if not lst or lst[0] != tag:
                flush()
                lst = (tag, [])
            lst[1].append(m.group(2))
            continue
        if lst:
            flush()
        para.append(line)
    flush()
    return "\n".join(out)


def plain(body: str) -> str:
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body or "")
    return re.sub(r"\s+", " ", re.sub(r"(^|\n)(##|[-*]|\d+\.)\s+|\*\*", " ", t)).strip()


class NewsStore:
    def __init__(self, db):
        self.db = db
        self.db.execute("""CREATE TABLE IF NOT EXISTS news_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL, summary TEXT NOT NULL,
            body TEXT NOT NULL, author TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0,
            published_at INTEGER, updated_at INTEGER NOT NULL)""")
        self.db.commit()

    COLS = ("id", "slug", "title", "summary", "body", "author", "published", "published_at", "updated_at")

    def _row(self, r) -> Optional[dict]:
        return dict(zip(self.COLS, r)) if r else None

    def list(self, published_only: bool = True, limit: int = 200) -> list[dict]:
        q = "SELECT " + ",".join(self.COLS) + " FROM news_posts" + (" WHERE published=1" if published_only else "") + \
            " ORDER BY COALESCE(published_at, updated_at) DESC LIMIT ?"
        return [self._row(r) for r in self.db.execute(q, (limit,)).fetchall()]

    def get(self, slug: str, published_only: bool = True) -> Optional[dict]:
        q = "SELECT " + ",".join(self.COLS) + " FROM news_posts WHERE slug=?" + (" AND published=1" if published_only else "")
        return self._row(self.db.execute(q, (slug,)).fetchone())

    def by_id(self, pid: int) -> Optional[dict]:
        return self._row(self.db.execute("SELECT " + ",".join(self.COLS) + " FROM news_posts WHERE id=?", (pid,)).fetchone())

    def save(self, title: str, summary: str, body: str, author: str, published: bool, pid: Optional[int] = None,
             slug: Optional[str] = None, at: Optional[int] = None) -> dict:
        title, summary, body = (title or "").strip(), (summary or "").strip(), (body or "").strip()
        if not title or not body:
            raise ValueError("A title and the article text are needed.")
        if len(title) > 110:
            raise ValueError("Keep the title under 110 characters (Google cuts longer ones).")
        summary = summary or plain(body)[:180]
        now = at or int(time.time())
        if pid:
            old = self.by_id(pid)
            if not old:
                raise ValueError("No such article")
            pub_at = old["published_at"] or (now if published else None)
            self.db.execute("UPDATE news_posts SET title=?, summary=?, body=?, author=?, published=?, published_at=?, updated_at=?"
                            " WHERE id=?", (title, summary, body, author, int(published), pub_at, now, pid))
        else:
            base = slug or slugify(title)
            s, n = base, 2
            while self.db.execute("SELECT 1 FROM news_posts WHERE slug=?", (s,)).fetchone():
                s, n = f"{base}-{n}", n + 1
            cur = self.db.execute("INSERT INTO news_posts (slug, title, summary, body, author, published, published_at, updated_at)"
                                  " VALUES (?,?,?,?,?,?,?,?)", (s, title, summary, body, author, int(published), now if published else None, now))
            pid = cur.lastrowid
        self.db.commit()
        return self.by_id(pid)

    def delete(self, pid: int) -> None:
        self.db.execute("DELETE FROM news_posts WHERE id=?", (pid,))
        self.db.commit()

    OLD_ENDING = "## Try it with ProAds\nUse the **Add ProAds on Google** button at the top of this page, then you will see ProAds articles more often in your Google results."

    def seed(self) -> None:
        """The first articles, once, so the section is never empty."""
        for pid, body in self.db.execute("SELECT id, body FROM news_posts WHERE body LIKE '%## Try it with ProAds%'").fetchall():
            if self.OLD_ENDING in body:                 # the button it pointed to is hidden until Google lists the site
                self.db.execute("UPDATE news_posts SET body=? WHERE id=?", (body.replace(self.OLD_ENDING, SEED[2][2].split("## Eligibility takes time")[0].rstrip() and
                                "## Eligibility takes time" + SEED[2][2].split("## Eligibility takes time")[1]), pid))
                self.db.commit()
        if not self.db.execute("SELECT COUNT(*) FROM news_posts").fetchone()[0]:
            for i, (title, summary, body) in enumerate(SEED):
                self.save(title, summary, body, "ProAds team", True, at=int(time.time()) - (len(SEED) - i) * 3600)
        if not self.db.execute("SELECT 1 FROM news_posts WHERE title=?", (LAUNCH_2[0],)).fetchone():
            self.save(*LAUNCH_2, "ProAds team", False)   # a draft: the admin reviews and publishes it


# ---------------------------------------------------------------- pages
CSS = """
:root{color-scheme:light}
body{margin:0;background:#fff;color:#141414;font-family:Inter,system-ui,sans-serif;line-height:1.7;font-size:17px}
a{color:#4A7600}a:hover{color:#5E9400}
.top{max-width:1100px;margin:0 auto;padding:18px clamp(16px,4vw,40px);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{display:inline-flex;align-items:center;gap:10px;font-family:'Space Grotesk',Inter,sans-serif;font-weight:700;font-size:20px;color:#141414;text-decoration:none}
.nav{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.nav a.pill,.prefsrc{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;border:1px solid #E3E6DD;background:#fff;color:#1F1F1F;font-weight:600;font-size:14px;text-decoration:none}
.prefsrc{box-shadow:0 1px 3px rgba(0,0,0,.08)}.prefsrc svg{color:#5E9400}
.nav a.go{background:#5E9400;border-color:#5E9400;color:#0A0A0A}
main{max-width:760px;margin:0 auto;padding:12px clamp(16px,4vw,40px) 64px}
h1{font-family:'Space Grotesk',Inter,sans-serif;font-size:clamp(30px,5vw,42px);line-height:1.15;margin:18px 0 10px}
h2{font-family:'Space Grotesk',Inter,sans-serif;font-size:23px;margin:34px 0 8px}
.meta{color:#5C5C5C;font-size:14px;margin-bottom:20px}
.lead{font-size:19px;color:#333}
.cover{width:100%;height:auto;border-radius:14px;border:1px solid #E8E9E3;margin:8px 0 18px}
.card{display:block;border:1px solid #E8E9E3;border-radius:14px;padding:18px 20px;margin:14px 0;text-decoration:none;color:inherit}
.card:hover{border-color:#5E9400}
.card h2{margin:4px 0 6px;font-size:21px;color:#141414}.card p{margin:0;color:#444;font-size:15px}
.cta{margin-top:40px;padding:20px 22px;border-radius:14px;background:#F3F7EA;border:1px solid #DCE8C4}
footer{max-width:760px;margin:0 auto;padding:24px clamp(16px,4vw,40px) 40px;color:#5C5C5C;font-size:14px;border-top:1px solid #E8E9E3}
"""
LOGO_SVG = ('<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><rect width="28" height="28" rx="5" fill="#5E9400"/>'
            '<path d="M5 19l6-6 4 3 8-9" stroke="#0A0A0A" stroke-width="2.4" fill="none" stroke-linecap="square"/></svg>')
STAR = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
        '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/></svg>')


def _host(base: str) -> str:
    return re.sub(r"^https?://", "", base).split("/")[0]


def _shell(base: str, title: str, desc: str, canonical: str, body: str, head_extra: str = "", og_image: str = "",
           og_type: str = "website") -> str:
    e = lambda s: html.escape(s, quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title><meta name="description" content="{e(desc)}"><link rel="canonical" href="{e(canonical)}">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="{og_type}"><meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(canonical)}"><meta property="og:site_name" content="ProAds">{f'<meta property="og:image" content="{e(og_image)}"><meta name="twitter:card" content="summary_large_image">' if og_image else ''}
<link rel="alternate" type="application/rss+xml" title="ProAds News" href="{base}/news/rss.xml">
<link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="48x48" href="/favicon-48.png"><link rel="apple-touch-icon" href="/favicon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>{head_extra}</head><body>
<header class="top"><a class="brand" href="/">{LOGO_SVG}ProAds</a>
<nav class="nav" aria-label="Main"><a class="pill" href="/news">News</a>
{f'<a class="prefsrc" href="https://www.google.com/preferences/source?q={e(_host(base))}" target="_blank" rel="noopener">{STAR}Add ProAds on Google</a>' if google_source_on() else ''}
<a class="pill go" href="/#signup">Get your free audit</a></nav></header>
{body}
<footer>© {datetime.now(timezone.utc).year} ProAds · a product of <a href="https://ezdevsoft.net" rel="noopener">EzDevSoft</a>, UAE ·
<a href="/about">About</a> · <a href="/terms">Terms</a> · <a href="/privacy">Privacy</a> · <a href="/security">Security</a> · <a href="/news/rss.xml">RSS</a></footer>
</body></html>"""


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def index_page(base: str, posts: list[dict]) -> str:
    cards = "".join(f'<a class="card" href="/news/{p["slug"]}"><span class="meta">{_date(p["published_at"])}</span>'
                    f'<h2>{html.escape(p["title"])}</h2><p>{html.escape(p["summary"])}</p></a>' for p in posts) \
        or "<p>No articles yet.</p>"
    body = (f'<main><h1>ProAds News</h1><p class="lead">Ad platform changes, tracking and what actually brings paying customers, '
            f'for businesses advertising on TikTok, Instagram and Facebook.</p>{cards}</main>')
    ld = {"@context": "https://schema.org", "@type": "CollectionPage", "name": "ProAds News", "url": f"{base}/news",
          "publisher": {"@type": "Organization", "name": PUBLISHER, "logo": {"@type": "ImageObject", "url": f"{base}/proads-logo.png"}}}
    return _shell(base, "ProAds News: ads, tracking and real sales", "News and guides on TikTok, Meta and Google ads, tracking and real sales.",
                  f"{base}/news", body, f'<script type="application/ld+json">{_ld(ld)}</script>')


def _ld(obj) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def article_page(base: str, p: dict, more: list[dict]) -> str:
    url = f"{base}/news/{p['slug']}"
    cover = f"{base}/news/{p['slug']}/cover.png"
    ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": p["title"], "description": p["summary"],
          "image": [cover], "datePublished": _iso(p["published_at"]), "dateModified": _iso(max(p["updated_at"], p["published_at"])),
          "author": [{"@type": "Organization", "name": p["author"], "url": f"{base}/about"}],
          "publisher": {"@type": "Organization", "name": PUBLISHER, "logo": {"@type": "ImageObject", "url": f"{base}/proads-logo.png"}},
          "mainEntityOfPage": url}
    others = "".join(f'<a class="card" href="/news/{o["slug"]}"><h2>{html.escape(o["title"])}</h2><p>{html.escape(o["summary"])}</p></a>'
                     for o in more if o["slug"] != p["slug"])[:6000]
    updated = f' · updated {_date(p["updated_at"])}' if p["updated_at"] - p["published_at"] > 86400 else ""
    body = (f'<main><article><h1>{html.escape(p["title"])}</h1>'
            f'<div class="meta">By {html.escape(p["author"])} · <time datetime="{_iso(p["published_at"])}">{_date(p["published_at"])}</time>{updated}</div>'
            f'<img class="cover" src="/news/{p["slug"]}/cover.png" width="1200" height="675" alt="">'
            f'<p class="lead">{html.escape(p["summary"])}</p>{render_body(p["body"])}</article>'
            f'<div class="cta"><b>See which posts and ads bring people who pay.</b> ProAds reads your real sales and teaches '
            f'Meta and TikTok who your buyers are. <a href="/#signup">Get your free audit</a></div>'
            + (f"<h2>More from ProAds News</h2>{others}" if others else "") + "</main>")
    return _shell(base, p["title"] + " · ProAds News", p["summary"], url, body,
                  f'<script type="application/ld+json">{_ld(ld)}</script>'
                  f'<meta property="article:published_time" content="{_iso(p["published_at"])}">', og_image=cover, og_type="article")


def cover_png(title: str) -> bytes:
    """A 1200x675 cover: ProAds logo, green bar and the headline (Google wants large images for Top Stories)."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1200, 675
    im = Image.new("RGB", (W, H), (250, 250, 247))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 18, H], fill=(94, 148, 0))
    if os.path.exists(LOGO):
        lg = Image.open(LOGO).convert("RGBA").resize((88, 88))
        im.paste(lg, (80, 70), lg)
    try:
        big, small = ImageFont.truetype(FONT, 64), ImageFont.truetype(FONT, 34)
    except Exception:
        big = small = ImageFont.load_default()
    d.text((190, 92), "ProAds News", font=small, fill=(20, 20, 20))
    words, lines, cur = title.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textlength(test, font=big) <= W - 160:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    lines = lines[:5]
    y = 230 + (5 - len(lines)) * 30
    for ln in lines:
        d.text((80, y), ln, font=big, fill=(10, 10, 10))
        y += 80
    d.text((80, H - 70), "pro.ezdevsoft.net", font=ImageFont.truetype(FONT, 26) if os.path.exists(FONT) else small, fill=(74, 118, 0))
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def rss(base: str, posts: list[dict]) -> str:
    e = lambda s: html.escape(s, quote=False)
    items = "".join(f"<item><title>{e(p['title'])}</title><link>{base}/news/{p['slug']}</link><guid>{base}/news/{p['slug']}</guid>"
                    f"<pubDate>{datetime.fromtimestamp(p['published_at'], timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
                    f"<description>{e(p['summary'])}</description></item>" for p in posts[:30])
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>ProAds News</title><link>{base}/news</link>'
            f"<description>Ads, tracking and real sales.</description><language>en</language>{items}</channel></rss>")


def sitemap(base: str, posts: list[dict]) -> str:
    pages = [("/", None), ("/news", posts[0]["updated_at"] if posts else None), ("/fees", None), ("/about", None), ("/terms", None),
             ("/privacy", None), ("/security", None)] + [(f"/news/{p['slug']}", max(p["updated_at"], p["published_at"])) for p in posts]
    urls = "".join(f"<url><loc>{base}{path}</loc>" + (f"<lastmod>{_iso(ts)}</lastmod>" if ts else "") + "</url>" for path, ts in pages)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def news_sitemap(base: str, posts: list[dict]) -> str:
    """Google News sitemap: articles from the last 2 days only (Google's rule)."""
    since = time.time() - 2 * 86400
    urls = "".join(f"<url><loc>{base}/news/{p['slug']}</loc><news:news><news:publication><news:name>ProAds News</news:name>"
                   f"<news:language>en</news:language></news:publication><news:publication_date>{_iso(p['published_at'])}</news:publication_date>"
                   f"<news:title>{html.escape(p['title'], quote=False)}</news:title></news:news></url>" for p in posts if p["published_at"] >= since)
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            f'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">{urls}</urlset>')


# ---------------------------------------------------------------- first articles (facts only, with sources)
LAUNCH_2 = (
    "ProAds 2.0: see the difference in percentages, and win back the sales you almost lost",
    "ProAds 2.0 adds ProAds Impact, Follow-ups, auto buyer audiences in Meta and a morning summary email. Every number says where it comes from.",
    """Most ad tools show you what the ad platforms claim. ProAds starts from what your shop was actually paid, and 2.0 is about showing the difference that makes, honestly.

## ProAds Impact: the difference, in percentages
A new card on Home, in three parts, each labelled with its source:
- **What Meta says.** Meta's own Dataset Quality figures for your Purchase event: Additional Conversions Reported (the share of purchases Meta measures because of server events), Event Match Quality and event coverage.
- **What ProAds sent.** Of your real paid orders, how many reached Meta and TikTok, how many carried an email or phone for matching, and how many were paid outside your website checkout, where no browser pixel can see them.
- **What changed.** Money really paid and orders per day in the same number of days before and after ProAds started sending your sales, with a likely range. It's a before-and-after comparison, not a controlled test, and the card says so.

ProAds never counts a sale that a platform claims but your shop didn't get paid for.

## Follow-ups (Premium)
The cheapest sales come from people who almost bought or already bought. Follow-ups lists:
- **Checkouts started but not paid**, from Shopify, with the saved cart link.
- **Past buyers due to buy again**, after the number of days you choose, with an optional code.
- **Happy customers to ask for a review**, 5 to 30 days after they paid.

One tap opens WhatsApp on your own phone with the message ready. ProAds counts a sale as brought back only when the same customer pays within 7 days of a checkout message or 14 days of a win-back message, and it shows separately the people who paid without a message.

## Auto buyer audiences (Premium)
Your real buyers, kept up to date in your Meta ad account every day: new buyers join, fully refunded ones drop out, plus a 1% lookalike once you have 100 buyers. Use it to stop paying to reach people who already bought, and to find more people like them.

## Morning summary
One email around 8:00: yesterday's money really paid against the same day last week, the last 7 days, ad spend and money back per 1 spent, what's waiting in Follow-ups and one thing to fix.

## Also new for chat-based businesses
Upload the leads export from Meta Business Suite (English or Thai) to see which ads bring buyers in Messenger, who closed the sales, and whether those buyers are reaching Meta at all.

ProAds 2.0 is live for every account. Impact and the morning summary are on every plan; Follow-ups and auto buyer audiences are part of Premium.""",
)

SEED = [
    ("TikTok's learning phase: the daily budget a UAE online shop needs",
     "TikTok needs enough purchases to learn who buys. Here is how to size a daily budget from your cost per sale, and what to do when the budget is small.",
     """When you run a TikTok campaign that optimises for purchases, TikTok first goes through a learning phase. It is testing who is likely to buy, and it needs real purchases to do that.

## What TikTok needs
- Guides on TikTok's learning phase put the target at about **50 conversions per ad group**, usually within the first week.
- An ad group with fewer than about 20 conversions in its first 10 days often shows **Learning limited**.
- TikTok's own budgeting advice is a daily budget of about **10 times your target cost per conversion**. See [TikTok's bidding and budget guide](https://ads.tiktok.com/help/article?aid=10010472).

## A worked example
Say a perfume sells for 300 AED and a sale costs about 150 AED in ads while TikTok is learning.
- TikTok's rule of thumb says about 1,500 AED a day.
- At 200 AED a day you get about 9 sales a week. That is enough to learn from, just slower, and you may see Learning limited for a while.

## When the budget is small
1. **Keep one ad group.** Splitting a small budget across ad groups means none of them learns.
2. **Lead with your cheapest product.** A lower first price gets more purchases, and more purchases mean faster learning.
3. **Leave it alone for 7 days.** Big edits and pauses of more than a day restart learning.
4. **Raise the budget 20 to 30% at a time** while each sale costs less than it earns you.
5. **If you get fewer than about 5 purchases a week**, optimise for Checkout for two weeks, then switch back to Purchase.

## The part most shops miss
Learning limited is not the real test. Profit is. If each sale costs less in ads than it leaves you after product costs, the campaign works, learning badge or not. That is why ProAds measures cost per **paid** order, after refunds, instead of cost per checkout."""),
    ("Why ad platforms should learn from paid orders, not checkouts",
     "A checkout is not a sale. Sending only verified, paid and non-refunded orders to Meta and TikTok teaches them to find real buyers.",
     """Meta and TikTok learn from the purchase events you send them. Most shops send an event when someone reaches the thank-you page, which can include failed payments, test orders and orders that are later refunded.

## Why it matters
The ad platform copies what you reward. If it is told that a refunded order was a great purchase, it looks for more people like that buyer.

## What ProAds sends instead
- **Only paid orders**, read from the payment system (Stripe, Shopify and others), not from the browser.
- **Refunded orders are never sent to TikTok**, and partly refunded ones are sent at the amount the shop kept. TikTok has no refund event, so this is the only way to keep its data clean.
- **Meta gets refunds as a separate event**, so its audiences stay accurate.
- Customer details are hashed (scrambled) before sending, as both platforms require.

## Limits worth knowing
- Meta only accepts purchase events from the last 7 days through its Conversions API, so older history cannot be sent as purchases.
- Older paying customers can still help as a customer list (for example, to exclude existing buyers or, with enough people, to find similar ones).

Clean signals do not make a bad ad good. They make sure the platform's learning budget is spent on the right people."""),
    ("Google Preferred Sources: what it is and how to add your site",
     "Google now lets people pick the sites they want to see more of in Top Stories and AI answers. Here is what it does, what it does not do, and how to add the button.",
     """Google's Preferred Sources lets a person choose sites they want to see more often in Search.

## Where it shows up
- **Top Stories**, for searches that bring up news.
- **AI Overviews and AI Mode**, since May 2026, according to [coverage of the change](https://origami.sa/en/blog/google-preferred-sources-button/).

## What it does not do
It is not a general ranking boost. It applies to the people who chose your site, and Google says the choice does not override relevance. See [Search Engine Journal's summary](https://www.searchenginejournal.com/googles-preferred-sources-feature-is-now-a-global-seo-signal/573591/).

## How to add the button
The simplest version is a link to Google's preferences page with your domain:

**https://www.google.com/preferences/source?q=yourdomain.com**

Google also offers a script that draws an official button. Either way, the site needs to publish fresh content regularly to be eligible. A site with only product pages has little for Top Stories to show.

## Eligibility takes time
A new site, or a site that has just started publishing, may not be listed yet. Search your domain on the preferences page: if it shows "No results", keep publishing regularly and check again in a few weeks."""),
]
