"""Sephora Romania (sephora.ro) - beauty; Salesforce Commerce Cloud (SFCC) storefront, Akamai Bot Manager.

Search: GET https://www.sephora.ro/on/demandware.store/Sites-Sephora_RO-Site/ro_RO/Search-Show?q={quote_plus(q)}&sz=48&start={offset}
        (SFCC pipeline URL; site id Sephora_RO / locale ro_RO from alltheplaces' sephora_europe spider; it may 301
        to an SEO search URL - redirects are followed). sz = page size (confirmed up to 96 on FR/TR), start = offset:
        page N -> start = (N-1)*48. /catalogsearch/result answered 404 (not Magento).
Tiles (CityBaddies sephora.fr 2026, quickScrapperFreelance sephora_fr, pretzi RO):
  .product-tile[data-tcproduct] - HTML-entity-encoded JSON (BeautifulSoup unescapes it):
    product_pid_name (name), product_brand | product_trademark, product_url_page (absolute URL),
    product_price_ati (current price incl. VAT, dot decimal "430.00"), product_old_price_ati (old price; equals the
    current one when there is no promo), product_pid / product_sku, product_sku_name (volume, e.g. "50 ml"),
    product_url_picture (often empty -> .product-first-img src / data-src / srcset).
    product_discount_ati is unreliable and is never used (discount = old vs current).
  Fallback (pretzi RO): div.product-tile, a.product-tile-link[href*="/p/"], .product-min-price, .price-sales-standard;
  unit prices "lei / 100 ml" are never the price.
  URL shape https://www.sephora.ro/p/{slug}-{id}.html (id may carry a "P": /p/le-parfum---eau-de-parfum-P569004.html).
Product page: meta[itemprop=price][content] in .price-block-right, .price-sales-standard (visible),
  old .product-price.st-price, meta[itemprop=sku], meta[itemprop=availability][content*=InStock|OutOfStock],
  h1[itemprop=name]; JSON-LD / meta fallback.
Anti-bot: Akamai Bot Manager. Desktop User-Agents get 403 "Access Denied" while mobile UAs get the full HTML
  (CityBaddies, sephora.fr 2026) -> Android Chrome UA + mobile client hints (same Chrome major as the curl_cffi
  TLS profile), >= 8 s between requests; a block page -> BlockedError.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ..extract import availability_flag, extract_product_page, make_soup
from ..models import Offer
from .base import Store
from ._fashion import (
    NextPager, clean_url, dedupe, first_el, image_of, join_title, landed_on_home, norm, num, old_price_in,
    outermost, pick_price, raise_if_challenge, stock_from_text, text_of,
)

_PID_RE = re.compile(r"/p/.*-(P?\d+)\.html$", re.I)
_VOLUME_RE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:ml|cl|l|g|gr|kg|buc|oz)\s*$", re.I)


def sephora_pid(url: Optional[str]) -> Optional[str]:
    m = _PID_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def _mobile_headers() -> dict:
    """Android Chrome UA with the same Chrome major as curl_cffi's default "chrome" TLS profile."""
    ver = "131"
    try:
        from curl_cffi.requests.impersonate import DEFAULT_CHROME  # type: ignore

        m = re.search(r"(\d{2,3})", str(DEFAULT_CHROME))
        if m:
            ver = m.group(1)
    except Exception:  # pragma: no cover - older curl_cffi
        pass
    return {
        "User-Agent": f"Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
                      f"Chrome/{ver}.0.0.0 Mobile Safari/537.36",
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "Referer": "https://www.sephora.ro/",
        "Upgrade-Insecure-Requests": "1",
    }


class SephoraStore(NextPager, Store):
    key = "sephora"
    name = "Sephora"
    base_url = "https://www.sephora.ro"
    domains = ("sephora.ro",)
    categories = ("beauty",)
    page_size = 48
    min_delay = 8.0
    extra_headers = _mobile_headers()
    search_path = "/on/demandware.store/Sites-Sephora_RO-Site/ro_RO/Search-Show"

    def search_url(self, query: str, page: int = 1) -> str:
        start = (max(1, page) - 1) * self.page_size
        return f"{self.base_url}{self.search_path}?q={self.q_plus(query)}&sz={self.page_size}&start={start}"

    def search_page(self, query: str, page: int) -> list[Offer]:
        url = self.search_url(query, page)
        html, soup, final = self.get_html(url)
        if landed_on_home(url, final):
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect:home"
            return []
        tiles = outermost(soup.select(".product-tile[data-tcproduct]"))
        if tiles:
            self.last_strategy = "tcproduct"
        else:
            tiles = outermost(soup.select("div.product-tile, .product-tile"))
            self.last_strategy = "tiles"
        out = dedupe(self._tile(t, final) for t in tiles)
        if out:
            return out
        if page == 1 and sephora_pid(final):
            o = self.parse_product(soup, final)
            if o is not None:
                o.store_name = self.name
                self.last_strategy = "product-page"
                return [o]
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ tiles
    @staticmethod
    def _tc(tile: Tag) -> dict:
        raw = tile.get("data-tcproduct")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}
        return data if isinstance(data, dict) else {}

    def _tile(self, t: Tag, base: str) -> Optional[Offer]:
        d = self._tc(t)
        link = t.select_one('a.product-tile-link[href*="/p/"]') or t.select_one('a[href*="/p/"]')
        url = clean_url(_s(d.get("product_url_page")), base) or (clean_url(link.get("href"), base) if link else "")
        if not url:
            return None
        brand = _s(d.get("product_brand")) or _s(d.get("product_trademark")) or text_of(
            first_el(t, ".product-brand", ".brand", "[class*='brand']"))
        name = _s(d.get("product_pid_name")) or text_of(first_el(t, ".product-title", ".product-name", "h3", "h2")) \
            or (norm(link.get("title")) if link is not None else "")
        title = join_title(brand, name)
        sku_name = _s(d.get("product_sku_name"))
        if title and sku_name and _VOLUME_RE.match(sku_name) and sku_name.replace(" ", "").lower() not in \
                title.replace(" ", "").lower():
            title = f"{title} {sku_name}"
        if not title:
            return None
        price = num(d.get("product_price_ati"))
        price_el = None
        if not price:
            price_el, price = pick_price(t, ".product-min-price", ".price-sales-standard", ".product-price",
                                         "[class*='price']")
        if not price:
            return None
        old = num(d.get("product_old_price_ati"))
        if not old or old <= price:
            old = old_price_in(t, price, exclude=[price_el])
        pid = _s(d.get("product_pid")) or sephora_pid(url) or _s(d.get("product_sku")) or None
        image = _s(d.get("product_url_picture"))
        if image.startswith("//"):
            image = "https:" + image
        stock = False if stock_from_text(text_of(t)) is False else None
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=pid, image=image or image_of(t, base, "img.product-first-img, img"),
            source="sephora",
        )

    # ------------------------------------------------------------------ product page
    def parse_product(self, soup: BeautifulSoup, url: str) -> Optional[Offer]:
        el = soup.select_one('.price-block-right meta[itemprop="price"][content]') or \
            soup.select_one('meta[itemprop="price"][content]')
        price = num(el.get("content")) if el is not None else None
        if not price:
            _, price = pick_price(soup, ".price-block-right .price-sales-standard", ".price-sales-standard")
        h1 = first_el(soup, "h1[itemprop=name]", "h1.product-name", "h1")
        if not price or h1 is None:
            return None
        brand_el = first_el(soup, "[itemprop=brand] [itemprop=name]", "[itemprop=brand]", ".brand-name")
        brand = norm(brand_el.get("content")) or text_of(brand_el) if brand_el is not None else ""
        title = join_title(brand, text_of(h1))
        old = None
        for o_el in soup.select(".product-price.st-price"):
            v = pick_price(o_el, ".price-sales-standard", "span", "*")[1] or num(text_of(o_el))
            if v and v > price and (old is None or v > old):
                old = v
        av = soup.select_one('meta[itemprop="availability"][content], link[itemprop="availability"][href]')
        stock = availability_flag(av.get("content") or av.get("href")) if av is not None else None
        sku = soup.select_one('meta[itemprop="sku"][content]')
        return Offer(store=self.key, title=title, url=url, price=price, old_price=old, in_stock=stock,
                     offer_id=sephora_pid(url) or (norm(sku.get("content")) if sku is not None else None),
                     source="sephora-pdp")

    def product(self, url: str) -> Optional[Offer]:
        resp = self.http.get(url)
        self.last_raw = resp.text
        soup = make_soup(resp.text)
        o = self.parse_product(soup, url)
        if o is None:
            raise_if_challenge(resp.text, soup, url)
            o = extract_product_page(resp.text, url, self.key)
            if o is not None:
                o.offer_id = sephora_pid(url) or o.offer_id
        if o is not None:
            o.url = url
            o.store_name = self.name
        return o


def _s(v: Any) -> str:
    if isinstance(v, list):
        v = v[0] if v else ""
    return norm(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""
