"""Answear Romania (answear.ro) - fashion; on-site search runs on Luigi's Box (third-party SaaS search API).

The HTML search route is broken for bots: https://answear.ro/k?search={q} redirects to the home page (libergent
2026-09-28) and /s?q= is unverified, so the shop's own search backend is used instead:
  GET https://live.luigisbox.com/search?tracker_id={TRACKER_ID}&q={q}&f[]=type:product&size=24&page={N}
      (form-encoded: spaces -> '+'; Answear's own open-source client answear/luigis-box-bundle and the Apify
      Answear actor use this API). TRACKER_ID ("111111-222222" shape) is specific to answear.ro and NOT known: it is
      discovered once per process from the home page HTML (tracker id / Luigi's Box LBX-<n>.js script, which is
      then read) or set with the env var ANSWEAR_LUIGISBOX_TRACKER_ID. No tracker id -> [] (diagnostics show it).
Response: results.hits[] {url (relative "/p/{slug}-{id}.html"), type "product", attributes{title, brand[0],
  price_amount (float RON) | price (number or "199,90 lei"), price_old_amount | price_old (when discounted),
  availability 0/1, id[0], image_link, web_url[0]}} - attribute values are often one-element lists.
  results.total_hits; field naming varies per index, so several names are tried.
Product page: JSON-LD Product offers.price (generic extraction), div[data-test=regularPrice] fallback.
Anti-bot: the Luigi's Box endpoint has no store anti-bot; answear.ro itself showed no Cloudflare/Akamai.
"""
from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlencode, urlsplit

from ..extract import extract_product_page, make_soup
from ..models import Offer
from ..net import BlockedError, FetchError
from .base import Store
from ._fashion import clean_url, dedupe, first, join_title, norm, num, raise_if_challenge, text_of

LUIGIS_SEARCH = "https://live.luigisbox.com/search"
TRACKER_ENV = "ANSWEAR_LUIGISBOX_TRACKER_ID"
_TRACKER_RE = re.compile(r"\b(\d{3,8}-\d{3,8})\b")
_TRACKER_CTX_RE = re.compile(r"""tracker[_-]?id["']?\s*[:=]\s*["'](\d{3,8}-\d{3,8})["']""", re.I)
_LBX_RE = re.compile(r"""(https?:)?//scripts\.luigisbox\.(?:com|tech)/(LBX-\d+)\.js""", re.I)
_ID_RE = re.compile(r"-(\d{4,})\.html$")

_TRACKER_CACHE: dict[str, Optional[str]] = {}


def answear_id(url: Optional[str]) -> Optional[str]:
    m = _ID_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def find_tracker_id(text: str) -> Optional[str]:
    """Luigi's Box tracker id from page HTML / script text."""
    m = _TRACKER_CTX_RE.search(text or "")
    if m:
        return m.group(1)
    low = (text or "").lower()
    for m in _TRACKER_RE.finditer(text or ""):
        ctx = low[max(0, m.start() - 160):m.end() + 40]
        if "luigi" in ctx or "tracker" in ctx or "lbx" in ctx:
            return m.group(1)
    return None


class AnswearStore(Store):
    key = "answear"
    name = "Answear"
    base_url = "https://answear.ro"
    domains = ("answear.ro",)
    categories = ("fashion",)
    page_size = 24
    min_delay = 2.0
    extra_headers = {"Referer": "https://answear.ro/", "Origin": "https://answear.ro"}

    # ------------------------------------------------------------------ tracker id
    def tracker_id(self) -> Optional[str]:
        env = norm(os.environ.get(TRACKER_ENV))
        if env:
            return env
        if "answear" in _TRACKER_CACHE:
            return _TRACKER_CACHE["answear"]
        tid: Optional[str] = None
        try:
            resp = self.http.get(self.base_url + "/")
            html = resp.text or ""
            tid = find_tracker_id(html)
            if not tid:
                m = _LBX_RE.search(html)
                if m:
                    js = self.http.get(f"https://scripts.luigisbox.com/{m.group(2)}.js",
                                       headers={"Accept": "*/*"}).text or ""
                    tid = find_tracker_id(js) or (_TRACKER_RE.search(js).group(1) if _TRACKER_RE.search(js) else None)
        except BlockedError:
            raise
        except FetchError:
            tid = None
        _TRACKER_CACHE["answear"] = tid
        return tid

    # ------------------------------------------------------------------ search
    def search_url(self, query: str, tracker: str, page: int = 1) -> str:
        return LUIGIS_SEARCH + "?" + urlencode([
            ("tracker_id", tracker), ("q", query.strip()), ("f[]", "type:product"),
            ("size", str(self.page_size)), ("page", str(page)),
        ])

    def search_page(self, query: str, page: int) -> list[Offer]:
        tracker = self.tracker_id()
        if not tracker:
            self.last_strategy = "luigisbox:no-tracker-id"
            return []
        data = self.get_json(self.search_url(query, tracker, page),
                             headers={"Accept": "application/json", "Referer": "https://answear.ro/"})
        results = (data or {}).get("results") if isinstance(data, dict) else None
        hits = (results or {}).get("hits") if isinstance(results, dict) else None
        out = dedupe(self._hit(h) for h in hits or [] if isinstance(h, dict))
        self.last_strategy = "luigisbox"
        try:
            total = int((results or {}).get("total_hits") or 0)
        except (TypeError, ValueError):
            total = 0
        if total and page * self.page_size >= total:
            self._last_page = True
        return out

    def search(self, query: str) -> list[Offer]:
        self._last_page = False
        out: list[Offer] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            offers = self.search_page(query, page)
            for o in offers:
                if o.key not in seen:
                    seen.add(o.key)
                    o.store_name = self.name
                    out.append(o)
            if self._last_page or len(offers) < self.page_size // 2:
                break
        return out

    def _hit(self, h: dict) -> Optional[Offer]:
        if (h.get("type") or "product") != "product":
            return None
        attrs = h.get("attributes") if isinstance(h.get("attributes"), dict) else {}
        href = first(attrs.get("web_url")) or h.get("url") or first(attrs.get("original_url"))
        url = clean_url(href if isinstance(href, str) else "", self.base_url)
        if not url or not urlsplit(url).netloc.endswith("answear.ro"):
            return None
        brand = norm(first(attrs.get("brand")))
        title = join_title(brand, first(attrs.get("title")))
        price = None
        for k in ("price_amount", "price_sale_amount", "price"):
            price = num(first(attrs.get(k)))
            if price:
                break
        if not title or not price:
            return None
        old = None
        for k in ("price_old_amount", "price_old", "price_regular_amount", "price_original"):
            old = num(first(attrs.get(k)))
            if old:
                break
        av = first(attrs.get("availability"))
        stock: Optional[bool] = None
        if isinstance(av, bool):
            stock = av
        elif isinstance(av, (int, float)) or (isinstance(av, str) and av.strip().isdigit()):
            stock = int(av) > 0
        pid = first(attrs.get("id"))
        image = first(attrs.get("image_link")) or first(attrs.get("image"))
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=str(pid) if pid not in (None, "") else answear_id(url),
            image=image if isinstance(image, str) and image.startswith("http") else None, source="luigisbox",
        )

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        resp = self.http.get(url)
        self.last_raw = resp.text
        o = extract_product_page(resp.text, url, self.key)
        soup = None
        if o is None:
            soup = make_soup(resp.text)
            el = soup.select_one("div[data-test='regularPrice'], [data-test='regularPrice']")
            h1 = soup.find("h1")
            price = num(text_of(el)) if el is not None else None
            if price and h1 is not None:
                o = Offer(store=self.key, title=text_of(h1), url=url, price=price, source="answear-pdp")
            else:
                raise_if_challenge(resp.text, soup, url)
        if o is not None:
            o.url = url
            o.offer_id = answear_id(url) or o.offer_id
            o.store_name = self.name
        return o


def _reset_tracker_cache() -> None:  # tests
    _TRACKER_CACHE.clear()


