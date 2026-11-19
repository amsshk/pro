import json
import re

import pytest

from paysignal import news
from paysignal.server import create_app

ADMIN = {"X-API-Key": "admin-key-0123456789abc"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("PROADS_PUBLIC_URL", "https://pro.example.net")
    return create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "x.db"), start_worker=False).test_client()


def test_public_news_pages(c, monkeypatch):
    r = c.get("/news")
    assert r.status_code == 200 and b"ProAds News" in r.data and b'href="/news/' in r.data
    slug = re.search(rb'href="/news/([a-z0-9-]+)"', r.data).group(1).decode()
    a = c.get(f"/news/{slug}")
    assert a.status_code == 200 and b"<article>" in a.data
    ld = json.loads(re.search(rb'<script type="application/ld\+json">(.*?)</script>', a.data, re.S).group(1))
    assert ld["@type"] == "NewsArticle" and ld["image"][0].endswith("/cover.png") and ld["datePublished"]
    assert b'class="prefsrc"' not in a.data                        # hidden until Google lists the site
    monkeypatch.setenv("PROADS_GOOGLE_SOURCE", "1")
    assert b"google.com/preferences/source?q=pro.example.net" in c.get(f"/news/{slug}").data
    img = c.get(f"/news/{slug}/cover.png")
    assert img.status_code == 200 and img.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert b"<rss" in c.get("/news/rss.xml").data
    sm = c.get("/sitemap.xml").data
    assert b"https://pro.example.net/news/" + slug.encode() in sm
    assert b"news:news" in c.get("/news-sitemap.xml").data
    rb = c.get("/robots.txt").data
    assert b"Sitemap: https://pro.example.net/sitemap.xml" in rb and b"Disallow: /app" in rb
    assert c.get("/news/nope").status_code == 404


def test_admin_writes_and_drafts_stay_hidden(c):
    assert c.post("/admin/pro/news", json={"title": "x", "body": "y"}).status_code == 401
    r = c.post("/admin/pro/news", json={"title": "Draft <b>one</b>", "body": "## Hi\n- a\n- b\n\n**bold** <script>x</script> [l](https://e.com)"}, headers=ADMIN).json
    slug = r["post"]["slug"]
    assert c.get(f"/news/{slug}").status_code == 404                      # drafts aren't public
    c.post("/admin/pro/news", json={"id": r["post"]["id"], "title": "Draft <b>one</b>", "body": r["post"]["body"], "published": True}, headers=ADMIN)
    page = c.get(f"/news/{slug}").data.decode()
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page and "<h2>Hi</h2>" in page and "<li>a</li>" in page
    assert '<a href="https://e.com" rel="noopener" target="_blank">l</a>' in page and "<strong>bold</strong>" in page
    assert c.post(f"/admin/pro/news/{r['post']['id']}/delete", headers=ADMIN).json["deleted"]
    assert c.get(f"/news/{slug}").status_code == 404


def test_render_body_lists_and_paragraphs():
    out = news.render_body("Para one\nstill one\n\n1. a\n2. b\n\n- c\nafter")
    assert out == "<p>Para one still one</p>\n<ol><li>a</li><li>b</li></ol>\n<ul><li>c</li></ul>\n<p>after</p>"


def test_news_sitemap_is_never_empty():
    import time as _t
    from paysignal import news as N
    old = [{"slug": "a", "title": "Old one", "published_at": _t.time() - 10 * 86400, "updated_at": 0},
           {"slug": "b", "title": "Older", "published_at": _t.time() - 20 * 86400, "updated_at": 0}]
    x = N.news_sitemap("https://x", old)
    assert x.count("<url>") == 1 and "/news/a</loc>" in x
    new = old + [{"slug": "c", "title": "New", "published_at": _t.time() - 3600, "updated_at": 0}]
    y = N.news_sitemap("https://x", new)
    assert y.count("<url>") == 1 and "/news/c</loc>" in y
    assert "<url>" not in N.news_sitemap("https://x", [])
