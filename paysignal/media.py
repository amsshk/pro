"""Media for ads: the client's own photos and videos, ready for the campaign builder.

Sources, in order of trust:
  website    product photos (Shopify / WooCommerce product lists, JSON-LD), share images, large page images, videos
  instagram  / facebook  the client's own posts (needs Meta permission for client accounts)
  enhanced   a real photo made bigger and sharper with ordinary image processing (no AI, nothing invented)
  generated  an AI image made to look like a small original; ALWAYS needs the owner to confirm it shows their
             product or service accurately before it can go into an ad

Fetching is safe: only http(s), never private or internal addresses (checked on every redirect), size limits,
time limits. Only media referenced by the client's own site or accounts is collected.
"""
from __future__ import annotations

import io
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

MAX_HTML = 3_000_000
MAX_IMAGE = 12_000_000
MAX_CANDIDATES = 40
MIN_SIDE_OK = 600          # Meta's feed minimum is about 600 px; 1080 is ideal
GOOD_SIDE = 1080
TINY = 200
SKIP_WORDS = re.compile(r"(logo|icon|favicon|sprite|pixel|spacer|badge|payment|avatar|placeholder|loader|arrow)", re.I)
UA = "ProAdsMediaBot/1.0 (+https://pro.ezdevsoft.net)"


class MediaError(Exception):
    pass


def _public_ip(host: str, resolver: Callable = socket.getaddrinfo) -> bool:
    try:
        infos = resolver(host, None)
    except Exception:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(infos)


def safe_get(http, url: str, max_bytes: int, resolver: Callable = socket.getaddrinfo) -> tuple[bytes, str, str]:
    """(body, content type, final url). Refuses internal addresses on every hop and stops at max_bytes."""
    for _ in range(4):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise MediaError("Only http(s) web addresses can be read.")
        if not _public_ip(u.hostname, resolver):
            raise MediaError(f"{u.hostname} isn't a public website.")
        try:
            r = http.get(url, timeout=12, stream=True, allow_redirects=False, headers={"User-Agent": UA})
        except Exception as e:
            raise MediaError(f"Couldn't open {u.hostname}: {e}") from e
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            continue
        if r.status_code != 200:
            raise MediaError(f"{u.hostname} answered {r.status_code}.")
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            size += len(chunk)
            if size > max_bytes:
                raise MediaError("That file is too large.")
            chunks.append(chunk)
        return b"".join(chunks), (r.headers.get("Content-Type") or "").split(";")[0].strip().lower(), url
    raise MediaError("Too many redirects.")


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metas, self.imgs, self.videos, self.ld, self._ld = [], [], [], [], None
        self.title = ""
        self._t = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and (a.get("property") or a.get("name") or "").lower() in ("og:image", "og:image:url",
                                                                                     "og:image:secure_url", "twitter:image"):
            self.metas.append(a.get("content") or "")
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-original") or ""
            best = _srcset_best(a.get("srcset") or a.get("data-srcset") or "")
            w = _int(a.get("width"))
            self.imgs.append({"url": best or src, "alt": (a.get("alt") or "")[:120], "w": w})
        elif tag == "video":
            if a.get("src"):
                self.videos.append({"url": a["src"], "poster": a.get("poster") or ""})
            self._video_poster = a.get("poster") or ""
        elif tag == "source" and (a.get("type") or "").startswith("video") and a.get("src"):
            self.videos.append({"url": a["src"], "poster": getattr(self, "_video_poster", "")})
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._ld = []
        elif tag == "title":
            self._t = True

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.ld.append("".join(self._ld))
            self._ld = None
        elif tag == "title":
            self._t = False

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
        elif self._t:
            self.title += data


def _int(v) -> int:
    try:
        return int(str(v).strip().rstrip("px"))
    except (TypeError, ValueError):
        return 0


def _srcset_best(srcset: str) -> str:
    best, bw = "", 0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = _int(bits[1][:-1]) if len(bits) > 1 and bits[1].endswith("w") else 1
        if w >= bw:
            best, bw = bits[0], w
    return best


def _ld_products(blobs: list[str]) -> list[dict]:
    out = []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            t = x.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                imgs = x.get("image")
                imgs = imgs if isinstance(imgs, list) else [imgs]
                offer = x.get("offers") or {}
                offer = offer[0] if isinstance(offer, list) and offer else offer
                for im in imgs:
                    url = im.get("url") if isinstance(im, dict) else im
                    if url:
                        out.append({"url": url, "title": str(x.get("name") or "")[:120],
                                    "price": str((offer or {}).get("price") or "")})
            for v in x.values():
                if isinstance(v, (list, dict)):
                    walk(v)
    for b in blobs:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return out


def discover(http, site: str, resolver: Callable = socket.getaddrinfo) -> list[dict]:
    """Candidate media from a website: [{url, kind, origin, title, price, poster}] best-first."""
    if not re.match(r"^https?://", site or ""):
        site = "https://" + (site or "").strip().lstrip("/")
    if not urlparse(site).path:
        site += "/"
    html, ctype, final = safe_get(http, site, MAX_HTML, resolver)
    if "html" not in ctype and not html.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        raise MediaError("That address isn't a web page.")
    root = "{0.scheme}://{0.netloc}".format(urlparse(final))
    page = _Page()
    page.feed(html.decode("utf-8", "replace"))
    found: list[dict] = []

    def add(url, origin, kind="image", **kw):
        if not url or url.startswith("data:"):
            return
        url = urljoin(final, url.strip())
        if url.startswith("//"):
            url = "https:" + url
        path = urlparse(url).path.lower()
        if kind == "image" and (path.endswith((".svg", ".gif", ".ico")) or SKIP_WORDS.search(path)):
            return
        if any(f["url"] == url for f in found):
            return
        found.append({"url": url, "kind": kind, "origin": origin, "title": kw.get("title", ""),
                      "price": kw.get("price", ""), "poster": urljoin(final, kw["poster"]) if kw.get("poster") else ""})

    # online shops publish their product list: the most useful photos of all
    for path, parse in (("/products.json?limit=50", _shopify), ("/wp-json/wc/store/v1/products?per_page=50", _woo)):
        try:
            body, ct, _ = safe_get(http, root + path, MAX_HTML, resolver)
            for p in parse(json.loads(body)):
                add(p["url"], "product", title=p["title"], price=p["price"])
        except (MediaError, ValueError, KeyError, TypeError):
            continue
    for p in _ld_products(page.ld):
        add(p["url"], "product", title=p["title"], price=p["price"])
    for m in page.metas:
        add(m, "share image", title=page.title.strip()[:120])
    for v in page.videos:
        add(v["url"], "page video", kind="video", poster=v.get("poster"))
    for im in page.imgs:
        if im["w"] and im["w"] < TINY:
            continue
        add(im["url"], "page image", title=im["alt"])
    return found[:MAX_CANDIDATES]


def _shopify(d: dict) -> list[dict]:
    out = []
    for p in d.get("products") or []:
        price = ((p.get("variants") or [{}])[0] or {}).get("price") or ""
        for im in (p.get("images") or [])[:3]:
            out.append({"url": im.get("src"), "title": str(p.get("title") or "")[:120], "price": str(price)})
    return out


def _woo(d) -> list[dict]:
    out = []
    for p in d if isinstance(d, list) else []:
        pr = p.get("prices") or {}
        price = ""
        if pr.get("price"):
            unit = int(pr.get("currency_minor_unit") or 2)
            price = f"{int(pr['price']) / 10 ** unit:g}"
        for im in (p.get("images") or [])[:3]:
            out.append({"url": im.get("src"), "title": str(p.get("name") or "")[:120], "price": price})
    return out


# ---------- image work (Pillow): measure, prepare for ads, enhance without inventing anything ----------
def measure(raw: bytes) -> tuple[int, int]:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as im:
            return im.size
    except Exception as e:
        raise MediaError("That file isn't a picture ProAds can read.") from e


def quality(w: int, h: int) -> str:
    side = min(w, h)
    return "good" if side >= GOOD_SIDE else "ok" if side >= MIN_SIDE_OK else "small" if side >= TINY else "tiny"


def to_jpeg(raw: bytes, max_side: int = 1600) -> bytes:
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=90, optimize=True)
        return out.getvalue()


def enhance(raw: bytes, target: int = GOOD_SIDE) -> bytes:
    """Bigger and sharper, same picture: high-quality resize up to 3x, gentle sharpening and contrast."""
    from PIL import Image, ImageFilter, ImageOps
    with Image.open(io.BytesIO(to_jpeg(raw, 4000))) as im:
        w, h = im.size
        scale = min(3.0, max(1.0, target / max(1, min(w, h))))
        if scale > 1:
            im = im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
        im = ImageOps.autocontrast(im, cutoff=0.5)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue()


class MediaStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS media_items (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
        source TEXT NOT NULL, origin TEXT, url TEXT, kind TEXT NOT NULL, width INTEGER, height INTEGER, quality TEXT,
        title TEXT, price TEXT, poster TEXT, ai TEXT NOT NULL DEFAULT 'none', parent_id INTEGER,
        approved INTEGER NOT NULL DEFAULT 1, hidden INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
        prompt TEXT, UNIQUE (client_id, source, url));
    CREATE TABLE IF NOT EXISTS media_blobs (item_id INTEGER PRIMARY KEY, data BLOB NOT NULL);
    """
    KEYS = ("id", "source", "origin", "url", "kind", "width", "height", "quality", "title", "price", "poster", "ai",
            "parent_id", "approved", "created_at")

    def __init__(self, db):
        self.db = db
        self.db.executescript(self.SCHEMA)
        self.db.commit()

    def add(self, client_id: str, item: dict, blob: Optional[bytes]) -> Optional[int]:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO media_items (client_id, source, origin, url, kind, width, height, quality, title, price,"
            " poster, ai, parent_id, approved, created_at, prompt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (client_id, item["source"], item.get("origin"), item.get("url"), item["kind"], item.get("width"),
             item.get("height"), item.get("quality"), item.get("title"), item.get("price"), item.get("poster"),
             item.get("ai", "none"), item.get("parent_id"), int(item.get("approved", 1)), int(time.time()),
             item.get("prompt")))
        if cur.rowcount != 1:
            return None
        if blob is not None:
            self.db.execute("INSERT INTO media_blobs VALUES (?,?)", (cur.lastrowid, blob))
        self.db.commit()
        return cur.lastrowid

    def list(self, client_id: str) -> list[dict]:
        rows = self.db.execute("SELECT " + ", ".join(self.KEYS) + " FROM media_items WHERE client_id=? AND hidden=0"
                               " ORDER BY id", (client_id,)).fetchall()
        return [dict(zip(self.KEYS, r)) for r in rows]

    def get(self, client_id: str, item_id: int) -> Optional[dict]:
        r = self.db.execute("SELECT " + ", ".join(self.KEYS) + " FROM media_items WHERE client_id=? AND id=?",
                            (client_id, item_id)).fetchone()
        return dict(zip(self.KEYS, r)) if r else None

    def blob(self, client_id: str, item_id: int) -> Optional[bytes]:
        r = self.db.execute("SELECT b.data FROM media_blobs b JOIN media_items m ON m.id=b.item_id"
                            " WHERE m.client_id=? AND m.id=?", (client_id, item_id)).fetchone()
        return bytes(r[0]) if r else None

    def approve(self, client_id: str, item_id: int) -> None:
        self.db.execute("UPDATE media_items SET approved=1 WHERE client_id=? AND id=?", (client_id, item_id))
        self.db.commit()

    def hide(self, client_id: str, item_id: int) -> None:
        self.db.execute("UPDATE media_items SET hidden=1 WHERE client_id=? AND id=?", (client_id, item_id))
        self.db.commit()


def rank(items: list[dict], ad_text: str) -> list[dict]:
    """Best media for one ad: words shared with a product's name first, then source and picture quality."""
    words = {w for w in re.findall(r"[a-z0-9؀-ۿ]{3,}", (ad_text or "").lower())}
    src = {"upload": 30, "instagram": 3, "facebook": 3, "website": 2, "enhanced": 2, "generated": 1}   # what the owner uploaded wins
    qual = {"good": 3, "ok": 2, "small": 0, "tiny": -5}

    def score(it):
        title = {w for w in re.findall(r"[a-z0-9؀-ۿ]{3,}", (it.get("title") or "").lower())}
        match = len(words & title) * 4
        return (match + src.get(it["source"], 0) + qual.get(it.get("quality") or "", 0)
                + (2 if it.get("origin") == "product" else 0) - (10 if not it.get("approved") else 0)
                + (it.get("id") or 0) / max(1, newest) * 0.5)          # ties: the most recent first
    newest = max((it.get("id") or 0 for it in items), default=0)
    usable = [it for it in items if it["kind"] in ("image", "video") and (it.get("quality") in ("good", "ok") or it["kind"] == "video")]
    return sorted(usable, key=score, reverse=True)
