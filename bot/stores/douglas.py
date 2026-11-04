"""Douglas Romania (douglas.ro) - perfume & cosmetics; platform UNVERIFIED (Shopware 6 in Jan 2024).

Search route is unverified, so two candidates are probed once per process and the one that answers is kept:
  1. https://www.douglas.ro/search?search={quote_plus(q)}   Shopware 6 default storefront route. douglas.ro ran
     Shopware 6 in Jan 2024 (Testproxpert/Douglas.ro: /account/login, loginMail, personalFirstName ids).
  2. https://www.douglas.ro/ro/search?q={quote_plus(q)}     route of the Douglas group platform (douglas.de style),
     in case douglas.ro moved there.
  A 404 / 410 or a redirect to the home page moves on to the next candidate; /catalogsearch/result/?q= is known to
  answer 404 (libergent 2026-09) and is not used. Page N > 1: only the pager's next link.
Listing markup, whichever the server returns (model knowledge - verify with a captured fixture):
  A) Shopware 6: .cms-listing-col .card.product-box
       a.product-name[href][title]            title (+ .product-manufacturer brand when shown)
       .product-price-info .product-price     current price "229,00 Lei*" (Shopware appends '*'); its
       .list-price / .list-price-price child = old price (the element gets class with-list-price)
       .product-price-unit "(458,00 Lei* / 100 ml)" unit price - never the price
       input[name^=lineItems][name$='[id]']   product id (32-hex)
  B) Douglas group: [data-testid=product-tile] | .product-tile, a[href*='/p/'] (/ro/p/{code}?variant={id}: variant
     kept), .top-brand / .brand-line / .name / .category texts, [class*=price] with struck / "original" old price.
  Otherwise JSON-LD / microdata / embedded JSON (generic).
Product page: Shopware microdata (meta itemprop=price / priceCurrency / availability / sku) or JSON-LD (generic).
Anti-bot: unknown; no blocking reported (libergent's plain fetch got real 404s) -> 3 s between requests.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup, Tag

from ..models import Offer
from ..net import BlockedError, FetchError
from .base import Store
from ._fashion import (
    NextPager, clean_url, dedupe, first_el, image_of, join_title, landed_on_home, longest, next_page_url, norm,
    old_price_in, outermost, pick_price, raise_if_challenge, stock_from_text, text_of,
)

ROUTES = ("shopware", "douglas-group")
_ROUTE_CACHE: dict[str, str] = {}
_CODE_RE = re.compile(r"/p/(\d{4,})(?:/|$)")


def douglas_id(url: Optional[str]) -> Optional[str]:
    p = urlsplit(url or "")
    m = _CODE_RE.search(p.path)
    if not m:
        return None
    variant = dict(parse_qsl(p.query)).get("variant")
    return f"{m.group(1)}-{variant}" if variant else m.group(1)


class DouglasStore(NextPager, Store):
    key = "douglas"
    name = "Douglas"
    base_url = "https://www.douglas.ro"
    domains = ("douglas.ro",)
    categories = ("beauty",)
    page_size = 24
    min_delay = 3.0
    extra_headers = {"Referer": "https://www.douglas.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str, route: str = "shopware") -> str:
        if route == "douglas-group":
            return f"{self.base_url}/ro/search?q={self.q_plus(query)}"
        return f"{self.base_url}/search?search={self.q_plus(query)}"

    def _routes(self) -> list[str]:
        known = _ROUTE_CACHE.get(self.key)
        return [known] + [r for r in ROUTES if r != known] if known else list(ROUTES)

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            url = self._pager_get(page)
            if not url:
                return []
            html, soup, final = self.get_html(url)
            out = self.parse_listing(soup, final)
            self._pager_set(page + 1, next_page_url(soup, final) if out else None)
            return out
        self._pager_reset()
        last_err: Optional[FetchError] = None
        for route in self._routes():
            url = self.search_url(query, route)
            try:
                html, soup, final = self.get_html(url)
            except BlockedError:
                raise
            except FetchError as e:
                if e.status in (404, 410):
                    last_err = e
                    continue
                raise
            if landed_on_home(url, final):
                raise_if_challenge(html, soup, final)
                continue
            _ROUTE_CACHE[self.key] = route
            out = self.parse_listing(soup, final)
            if out:
                self._pager_set(2, next_page_url(soup, final))
                return out
            raise_if_challenge(html, soup, final)
            return self.fallback(html, final, soup)
        if last_err is not None:
            raise last_err
        self.last_strategy = "redirect:home"
        return []

    # ------------------------------------------------------------------ listing
    def parse_listing(self, soup: BeautifulSoup, base: str) -> list[Offer]:
        boxes = outermost(soup.select(".product-box"))
        if boxes:
            out = dedupe(self._shopware(b, base) for b in boxes)
            if out:
                self.last_strategy = "shopware-cards"
                return out
        tiles = outermost(soup.select("[data-testid='product-tile'], .product-tile"))
        out = dedupe(self._tile(t, base) for t in tiles)
        if out:
            self.last_strategy = "douglas-tiles"
        return out

    def _shopware(self, b: Tag, base: str) -> Optional[Offer]:
        a = b.select_one("a.product-name[href]") or b.select_one("a.product-image-link[href]") or b.select_one("a[href]")
        if a is None:
            return None
        url = clean_url(a.get("href"), base)
        name = longest(a.get("title"), a.get_text(" ", strip=True))
        brand = text_of(first_el(b, ".product-manufacturer", ".product-brand", "[class*='manufacturer']"))
        title = join_title(brand, name) if brand and len(brand) < 40 else name
        if not url or not title:
            return None
        box = b.select_one(".product-price-info") or b
        price_el, price = pick_price(box, ".product-price", ".price")
        if not price:
            return None
        old = old_price_in(box, price, exclude=[price_el])
        pid = None
        inp = b.select_one("input[name^='lineItems'][name$='[id]']")
        if inp is not None and norm(inp.get("value")):
            pid = norm(inp.get("value"))
        if not pid:
            el = b.select_one("[data-product-id]")
            pid = norm(el.get("data-product-id")) if el is not None else None
        stock = False if stock_from_text(text_of(b)) is False else None
        return Offer(store=self.key, title=title, url=url, price=price, old_price=old, in_stock=stock,
                     offer_id=pid or None, image=image_of(b, base, "img"), source="shopware")

    def _tile(self, t: Tag, base: str) -> Optional[Offer]:
        a = t.select_one("a[href*='/p/']") or t.select_one("a[href]")
        if a is None:
            return None
        url = clean_url(a.get("href"), base, keep=("variant",))
        parts = [text_of(x) for x in t.select(".top-brand, .brand-line, .name, .category")]
        title = join_title(*parts) or longest(a.get("title"), a.get_text(" ", strip=True))
        if not url or not title:
            return None
        price_el, price = pick_price(t, "[data-testid*='price']", ".product-price__price", ".product-price",
                                     "[class*='price']")
        if not price:
            return None
        old = old_price_in(t, price, exclude=[price_el])
        stock = False if stock_from_text(text_of(t)) is False else None
        return Offer(store=self.key, title=title, url=url, price=price, old_price=old, in_stock=stock,
                     offer_id=douglas_id(url), image=image_of(t, base, "img"), source="douglas")


def _reset_route_cache() -> None:  # tests
    _ROUTE_CACHE.clear()
