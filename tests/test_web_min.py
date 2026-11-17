"""The browser gets compact pages (tools/minify_web.mjs), never stale ones."""
import hashlib
import json
import os

import pytest

from paysignal import server
from paysignal.server import create_app

WEB = server.WEB_DIR
PAGES = sorted(n for n in os.listdir(WEB) if n.endswith(".html"))


def test_every_page_has_an_up_to_date_compact_build():
    manifest = json.load(open(os.path.join(server.MIN_DIR, "manifest.json")))
    for name in PAGES:
        src = open(os.path.join(WEB, name), "rb").read()
        assert manifest.get(name) == hashlib.sha256(src).hexdigest(), \
            f"{name} changed: run `node tools/minify_web.mjs` and commit paysignal/web/min"
        assert server.web_page_dir(name) == server.MIN_DIR


def test_compact_pages_have_no_comments_and_keep_their_text():
    for name in PAGES:
        mini = open(os.path.join(server.MIN_DIR, name), encoding="utf-8").read()
        assert "<!--" not in mini and "<head>" in mini and "</html>" in mini
        assert "/* " not in mini.split("<script", 1)[-1]
    assert "Ownership of ProAds" in open(os.path.join(server.MIN_DIR, "terms.html"), encoding="utf-8").read()


def test_app_serves_the_compact_page(tmp_path):
    c = create_app({}, str(tmp_path / "w.db"), start_worker=False).test_client()
    body = c.get("/app").data
    assert body == open(os.path.join(server.MIN_DIR, "index.html"), "rb").read()


def test_stale_compact_page_falls_back_to_the_source(tmp_path, monkeypatch):
    d = tmp_path / "web"
    (d / "min").mkdir(parents=True)
    (d / "terms.html").write_text("<html><head></head><body>new terms</body></html>")
    (d / "min" / "terms.html").write_text("<html><head></head><body>old terms</body></html>")
    (d / "min" / "manifest.json").write_text(json.dumps({"terms.html": "0" * 64}))
    monkeypatch.setattr(server, "WEB_DIR", str(d))
    monkeypatch.setattr(server, "MIN_DIR", str(d / "min"))
    assert server.web_page_dir("terms.html") == str(d)
    assert server.web_page_dir("missing.html") == str(d)
