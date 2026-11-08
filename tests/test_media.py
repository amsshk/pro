import base64
import io
import json

import pytest
from PIL import Image

from paysignal import media, meta_ads
from test_billing import ACCT, app  # noqa: F401  (fixture)


def png(w, h, color=(200, 80, 60)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


HTML = b"""<!doctype html><html><head><title>Glow Clinic</title>
<meta property="og:image" content="/share.jpg">
<script type="application/ld+json">{"@type":"Product","name":"Vitamin Serum","image":"https://glow.ae/serum.jpg","offers":{"price":"149"}}</script>
</head><body>
<img src="/logo.png" width="120"><img src="/thumb.jpg" width="80">
<img src="/hero-small.jpg" srcset="/hero-640.jpg 640w, /hero-1600.jpg 1600w" alt="Clinic room">
<video poster="/poster.jpg"><source src="https://glow.ae/tour.mp4" type="video/mp4"></video>
</body></html>"""


class Resp:
    def __init__(self, body, status=200, ctype="text/html", headers=None):
        self.body, self.status_code = body, status
        self.headers = {"Content-Type": ctype, **(headers or {})}

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]


class Web:
    def __init__(self, pages):
        self.pages = pages
        self.seen = []

    def get(self, url, **kw):
        self.seen.append(url)
        if url in self.pages:
            v = self.pages[url]
            return v if isinstance(v, Resp) else Resp(v, ctype="image/jpeg" if not url.endswith((".json", "/")) else "application/json")
        return Resp(b"", 404)


PUBLIC = lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))]
PRIVATE = lambda host, port: [(2, 1, 6, "", ("10.0.0.5", 0))]


def site():
    return Web({"https://glow.ae/": Resp(HTML),
                "https://glow.ae/products.json?limit=50": Resp(json.dumps({"products": [
                    {"title": "Hydra Facial Kit", "variants": [{"price": "299.00"}], "images": [{"src": "https://cdn.shopify.com/kit.jpg"}]}]}).encode(), ctype="application/json"),
                "https://glow.ae/share.jpg": png(1200, 630), "https://glow.ae/serum.jpg": png(1400, 1400),
                "https://cdn.shopify.com/kit.jpg": png(500, 500), "https://glow.ae/hero-1600.jpg": png(1600, 1066),
                "https://glow.ae/poster.jpg": png(1280, 720)})


def test_discover_finds_products_first_and_skips_logos():
    found = media.discover(site(), "glow.ae", PUBLIC)
    urls = [f["url"] for f in found]
    assert urls[0] == "https://cdn.shopify.com/kit.jpg" and found[0]["title"] == "Hydra Facial Kit" and found[0]["price"] == "299.00"
    assert "https://glow.ae/serum.jpg" in urls and "https://glow.ae/share.jpg" in urls
    assert "https://glow.ae/hero-1600.jpg" in urls                         # biggest from srcset
    assert not any("logo" in u or "thumb" in u for u in urls)
    vid = next(f for f in found if f["kind"] == "video")
    assert vid["url"] == "https://glow.ae/tour.mp4" and vid["poster"] == "https://glow.ae/poster.jpg"


def test_never_reads_internal_addresses():
    with pytest.raises(media.MediaError, match="public website"):
        media.discover(site(), "https://glow.ae/", PRIVATE)
    w = Web({"https://glow.ae/": Resp(b"", 302, headers={"Location": "http://169.254.169.254/latest"})})
    calls = {"n": 0}

    def resolver(host, port):
        calls["n"] += 1
        return PUBLIC(host, port) if host == "glow.ae" else [(2, 1, 6, "", ("169.254.169.254", 0))]
    with pytest.raises(media.MediaError, match="public website"):
        media.safe_get(w, "https://glow.ae/", 1000, resolver)
    with pytest.raises(media.MediaError, match="http"):
        media.safe_get(w, "file:///etc/passwd", 1000, resolver)


def test_quality_and_enhance():
    assert media.quality(1080, 1350) == "good" and media.quality(700, 700) == "ok" and media.quality(400, 300) == "small"
    out = media.enhance(png(400, 300))
    assert min(media.measure(out)) >= 900                                  # up to 3x: 300 -> 900


def test_rank_matches_ad_to_product():
    items = [{"id": 1, "source": "website", "kind": "image", "quality": "good", "title": "Clinic room", "approved": 1, "origin": "page image"},
             {"id": 2, "source": "website", "kind": "image", "quality": "ok", "title": "Vitamin Serum", "approved": 1, "origin": "product"},
             {"id": 3, "source": "website", "kind": "image", "quality": "small", "title": "Vitamin Serum", "approved": 1, "origin": "product"}]
    assert [x["id"] for x in media.rank(items, "Glow with our vitamin serum")] == [2, 1]


@pytest.fixture
def client(app):
    state = app.extensions["paysignal"]
    state.media_http = site()
    state.media_resolver = PUBLIC
    c = app.test_client()
    tok = c.post("/auth/signup", json={**ACCT, "website": "glow.ae"}).json["token"]
    return state, c, {"X-API-Key": tok}


class FakeAI:
    available = True

    def describe_image(self, jpg, ctx=""):
        return "a pink box of hydra facial kit on white"

    def generate_image(self, prompt, size="1080x1080"):
        self.prompt = prompt
        return png(1080, 1080, (240, 200, 210))


def test_scan_enhance_generate_approve_suggest_and_publish_rules(client):
    state, c, H = client
    r = c.post("/media/glow-clinic/scan", headers=H).json
    items = {i["url"]: i for i in r["items"]}
    assert items["https://cdn.shopify.com/kit.jpg"]["quality"] == "small" and not items["https://cdn.shopify.com/kit.jpg"]["usable"]
    assert items["https://glow.ae/serum.jpg"]["quality"] == "good" and items["https://glow.ae/tour.mp4"]["kind"] == "video"
    small = items["https://cdn.shopify.com/kit.jpg"]["id"]
    img = c.get(f"/media/glow-clinic/{small}/image", headers=H)
    assert img.status_code == 200 and img.data[:3] == b"\xff\xd8\xff"
    # enhance: no approval needed, clearly labelled
    e = c.post(f"/media/glow-clinic/{small}/enhance", headers=H).json
    assert e["ai"] == "enhanced" and e["approved"] and e["usable"] and "nothing changed" in e["label"]
    # similar: AI-made, blocked until the owner confirms it's accurate
    state.ai = FakeAI()
    g = c.post(f"/media/glow-clinic/{small}/similar", headers=H).json
    assert g["ai"] == "generated" and g["needs_approval"] and "hydra facial kit" in state.ai.prompt
    plan = {"ad_sets": [{"ads": [{"headline": "Hydra facial kit at home"}, {"headline": "Vitamin serum glow"}]}]}
    sug = c.post("/media/glow-clinic/suggest", headers=H, json={"plan": plan}).json["suggestions"]
    assert g["id"] not in [v["id"] for v in sug.values()]
    assert sug["0-1"]["title"] == "Vitamin Serum"
    assert c.post(f"/media/glow-clinic/{g['id']}/approve", headers=H, json={}).status_code == 400
    assert c.post(f"/media/glow-clinic/{g['id']}/approve", headers=H, json={"accurate": True}).json["approved"]


def test_publish_resolves_media_and_refuses_unconfirmed_ai(client):
    state, c, H = client
    c.post("/media/glow-clinic/scan", headers=H)
    state.ai = FakeAI()
    items = c.get("/media/glow-clinic", headers=H).json["items"]
    serum = next(i for i in items if i["url"].endswith("serum.jpg"))["id"]
    g = c.post(f"/media/glow-clinic/{serum}/similar", headers=H).json
    got = {}
    state.ads_settings = lambda cid, admin: {"token": "t", "ad_account": "act_1", "page_id": "p", "pixel_id": "x",
                                             "instagram_id": "", "version": "v25.0"}

    def fake_publish(self, plan, images, landing, currency=""):
        got.update(images)
        return {"campaign_id": "c9", "status": "PAUSED"}
    import paysignal.meta_ads as ma
    orig = ma.Publisher.publish
    ma.Publisher.publish = fake_publish
    try:
        plan = {"ad_sets": [{"ads": [{"headline": "a"}, {"headline": "b"}]}]}
        r = c.post("/campaign/glow-clinic/publish", headers=H, json={"plan": plan, "landing_url": "https://glow.ae",
                                                                     "images": {"0-0": f"media:{serum}", "0-1": f"media:{g['id']}"}})
        assert r.status_code == 400 and "confirmed" in r.json["error"]
        vid = next(i for i in items if i["kind"] == "video")["id"]
        r = c.post("/campaign/glow-clinic/publish", headers=H, json={"plan": plan, "landing_url": "https://glow.ae",
                                                                     "images": {"0-0": f"media:{serum}", "0-1": f"media:{vid}"}})
        assert r.status_code == 200
        assert base64.b64decode(got["0-0"])[:3] == b"\xff\xd8\xff"
        assert got["0-1"]["video_url"] == "https://glow.ae/tour.mp4" and got["0-1"]["thumb"]
    finally:
        ma.Publisher.publish = orig


class MetaVideo:
    def __init__(self):
        self.posts, self.n = [], 0

    def post(self, url, data=None, timeout=None):
        self.posts.append((url.split("/")[-1], dict(data)))
        if url.endswith("advideos"):
            return Resp2({"id": "v1"})
        if url.endswith("adimages"):
            return Resp2({"images": {"x": {"hash": "h1"}}})
        return Resp2({"id": "x"})

    def get(self, url, params=None, timeout=None):
        self.n += 1
        return Resp2({"status": {"video_status": "ready" if self.n > 1 else "processing"}, "picture": "https://x/p.jpg"})


class Resp2:
    def __init__(self, b):
        self.b, self.status_code = b, 200

    def json(self):
        return self.b


def test_video_ad_waits_for_processing_and_uses_thumbnail():
    pub = meta_ads.Publisher(MetaVideo(), {"token": "t", "ad_account": "act_1", "page_id": "p", "pixel_id": "x",
                                          "instagram_id": "", "version": "v25.0"})
    pub.sleep = lambda s: None
    thumb = base64.b64encode(media.to_jpeg(png(800, 450))).decode()
    data = pub._video({"video_url": "https://glow.ae/tour.mp4", "thumb": thumb}, "ad 1", {"headline": "H", "primary_text": "T"},
                      {"type": "LEARN_MORE", "value": {"link": "https://glow.ae"}})
    assert data["video_id"] == "v1" and data["image_hash"] == "h1" and data["title"] == "H"
    with pytest.raises(meta_ads.PublishError, match="https"):
        pub._video({"video_url": "http://glow.ae/v.mp4"}, "ad 1", {}, {})


class MetaPub:
    def __init__(self):
        self.posts = []

    def post(self, url, data=None, timeout=None):
        self.posts.append((url.split("/")[-1], dict(data)))
        if url.endswith("adimages"):
            return Resp2({"images": {"x": {"hash": "h1"}}})
        return Resp2({"id": "id" + str(len(self.posts))})

    def get(self, url, params=None, timeout=None):
        if url.endswith("adspixels"):
            return Resp2({"data": [{"id": "x"}]})
        return Resp2({"account_status": 1, "currency": "AED"})


def _publish(mode, ig="17841"):
    http = MetaPub()
    pub = meta_ads.Publisher(http, {"token": "t", "ad_account": "act_1", "page_id": "p", "pixel_id": "x",
                                    "instagram_id": ig, "version": "v25.0"})
    plan = {"name": "Spring", "daily_budget": 100, "placement_mode": mode, "ad_sets": [{"name": "A", "ads": [{"headline": "H"}]}]}
    img = base64.b64encode(media.to_jpeg(png(800, 800))).decode()
    pub.publish(plan, {"0-0": img}, "https://glow.ae")
    adset = next(d for p, d in http.posts if p == "adsets")
    creative = next(d for p, d in http.posts if p == "adcreatives")
    return json.loads(adset["targeting"]) if isinstance(adset["targeting"], str) else adset["targeting"], creative


def test_instagram_only_placements():
    t, creative = _publish("instagram")
    assert t["publisher_platforms"] == ["instagram"] and "reels" in t["instagram_positions"]
    assert "17841" in json.dumps(creative)
    assert '"caption": "glow.ae"' in json.dumps(json.loads(creative["object_story_spec"]) if isinstance(creative["object_story_spec"], str) else creative["object_story_spec"])
    t, _ = _publish("automatic")
    assert "publisher_platforms" not in t
    with pytest.raises(meta_ads.PublishError, match="Instagram account"):
        _publish("instagram", ig="")
