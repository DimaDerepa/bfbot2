"""Sinsay Romania (sinsay.com/ro/ro/) - LPP group fast fashion; client-side Algolia search.

DISABLED BY DEFAULT: the result grid is rendered in the browser from Algolia (#algoliaContainer on Sinsay RO,
.algolia-products-module__algolia-products-container on Sinsay HU), the Algolia application id / search key / index
are unknown, and bohdan-tur/price-tracker forces a headless browser for sinsay.com. Treat it as hard from GitHub
Actions without a browser (BOT_BROWSER=sinsay enables the core Playwright fallback on block pages).
When enabled:
  1. GET https://www.sinsay.com/ro/ro/?query={quote(q)}   (hmlendea/nucisearch routing table; unverified)
  2. Algolia credentials are looked up in that HTML (10-char application id near "algolia"/"appId", 32-hex search
     key, an index name containing "ro_ro"); when all three are found:
       POST https://{APP_ID}-dsn.algolia.net/1/indexes/*/queries
            headers x-algolia-application-id / x-algolia-api-key
            body {"requests": [{"indexName": INDEX, "params": "query={q}&hitsPerPage=40"}]}
       results[0].hits[] - field names unknown, so common ones are tried: name/title, url/url_key/link,
       final_price/special_price/price (number, {"RON": ...} or text), regular/old price, sku/objectID, image.
  3. Otherwise generic extraction of the page (JSON-LD / embedded JSON / cards).
Product pages: JSON-LD Product offers.price, then meta itemprop price (generic extraction).
Prices: RON, visible "59,99 lei"; JSON-LD dot decimal. Images (Reserved): https://www.reserved.com/media/catalog/product/...
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from ..models import Offer
from ..net import BlockedError, FetchError
from .base import Store
from ._fashion import (
    NextPager, clean_url, dedupe, first, join_title, landed_on_home, norm, num, raise_if_challenge,
)

_APP_RE = re.compile(r"""(?:algolia[_-]?app(?:lication)?[_-]?id|applicationId|appId|app_id)["']?\s*[:=]\s*["']([A-Z0-9]{10})["']""", re.I)
_KEY_RE = re.compile(r"""(?:algolia[_-]?(?:search[_-]?)?(?:api[_-]?)?key|searchApiKey|searchKey|search_key|apiKey|api_key)["']?\s*[:=]\s*["']([a-f0-9]{32})["']""", re.I)
_INDEX_RE = re.compile(r"""(?:index[_-]?name|indexName|algoliaIndex(?:Name)?|index)["']?\s*[:=]\s*["']([\w.-]*ro_ro[\w.-]*)["']""", re.I)
_NAME_KEYS = ("name", "title", "product_name", "productName")
_URL_KEYS = ("url", "link", "product_url", "productUrl", "url_key", "urlKey", "slug")
_PRICE_KEYS = ("final_price", "finalPrice", "special_price", "specialPrice", "price_final", "sale_price", "price")
_OLD_KEYS = ("regular_price", "regularPrice", "old_price", "oldPrice", "original_price", "originalPrice", "base_price")
_ID_KEYS = ("sku", "objectID", "id", "product_id")
_IMG_KEYS = ("image", "image_url", "imageUrl", "thumbnail", "small_image", "photo")


def algolia_config(html: str) -> Optional[tuple[str, str, str]]:
    app, key, index = _APP_RE.search(html or ""), _KEY_RE.search(html or ""), _INDEX_RE.search(html or "")
    if app and key and index:
        return app.group(1), key.group(1), index.group(1)
    return None


def _money(v: Any) -> Optional[float]:
    if isinstance(v, dict):
        for k in ("RON", "ron", "value", "amount", "default", "price"):
            if k in v:
                return _money(v[k])
        return None
    return num(first(v))


class SinsayStore(NextPager, Store):
    key = "sinsay"
    name = "Sinsay"
    base_url = "https://www.sinsay.com"
    locale_path = "/ro/ro/"
    domains = ("sinsay.com",)
    categories = ("fashion",)
    enabled_by_default = False
    page_size = 40
    min_delay = 3.0
    extra_headers = {"Referer": "https://www.sinsay.com/ro/ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}{self.locale_path}?query={quote(query.strip(), safe='')}"

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            return []
        url = self.search_url(query)
        html, soup, final = self.get_html(url)
        if landed_on_home(url, final):
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect:home"
            return []
        cfg = algolia_config(html)
        if cfg:
            try:
                out = self.algolia(query, *cfg)
            except BlockedError:
                raise
            except (FetchError, ValueError):
                out = []
            if out:
                self.last_strategy = "algolia"
                return out
        raise_if_challenge(html, soup, final)
        out = self.fallback(html, final, soup)
        return [o for o in out if urlsplit(o.url).path.startswith(self.locale_path)] or out

    def algolia(self, query: str, app: str, key: str, index: str) -> list[Offer]:
        body = {"requests": [{"indexName": index, "params": f"query={quote(query.strip(), safe='')}&hitsPerPage={self.page_size}"}]}
        data = self.http.post_json(f"https://{app.lower()}-dsn.algolia.net/1/indexes/*/queries", body, headers={
            "x-algolia-application-id": app, "x-algolia-api-key": key, "Referer": self.base_url + self.locale_path,
        })
        try:
            self.last_raw = json.dumps(data, ensure_ascii=False)[:3_000_000]
        except (TypeError, ValueError):
            self.last_raw = None
        results = data.get("results") if isinstance(data, dict) else None
        hits = (results[0] or {}).get("hits") if isinstance(results, list) and results else None
        return dedupe(self._hit(h) for h in hits or [] if isinstance(h, dict))

    def _hit(self, h: dict) -> Optional[Offer]:
        name = next((norm(first(h.get(k))) for k in _NAME_KEYS if norm(first(h.get(k)))), "")
        brand = norm(first(h.get("brand")))
        title = join_title(brand, name)
        href = next((first(h.get(k)) for k in _URL_KEYS if isinstance(first(h.get(k)), str) and first(h.get(k))), "")
        if href and not href.startswith(("http", "/")):
            href = self.locale_path + href.lstrip("/")
        url = clean_url(href, self.base_url)
        price = next((p for p in (_money(h.get(k)) for k in _PRICE_KEYS) if p), None)
        if not title or not url or not price:
            return None
        old = next((p for p in (_money(h.get(k)) for k in _OLD_KEYS) if p), None)
        pid = next((str(first(h.get(k))) for k in _ID_KEYS if first(h.get(k)) not in (None, "")), None)
        img = next((first(h.get(k)) for k in _IMG_KEYS if isinstance(first(h.get(k)), str) and first(h.get(k))), None)
        stock = h.get("in_stock") if isinstance(h.get("in_stock"), bool) else (
            h.get("is_in_stock") if isinstance(h.get("is_in_stock"), bool) else None)
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=pid, image=clean_url(img, self.base_url) if img else None, source="algolia",
        )
