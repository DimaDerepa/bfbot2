"""elefant.ro - books, toys, beauty, fashion, home (general marketplace); server-rendered HTML, Cloudflare.

Search: GET https://www.elefant.ro/search?SearchTerm={quote(q)}   spaces -> %20 (the search form's input is
        name="SearchTerm"; /catalogsearch/result/ is NOT valid). Page N > 1: only the pager's next link
        (pagination parameters are undocumented, so they are never built from a template).
Cards (pretzi RO store-configs 2026-03, CiprianSimionov elefant.py, ElMortimer elefant scraper):
  .product-tile | .product-item
    a.product-title[href*='.html']    title = text (or title attribute); href may be relative
    .product-price .current-price     current price (fallback .product-price .price), "49,99 lei" / "1.299,00 lei"
    old price: struck / .old-price / .regular-price inside .product-price (UNVERIFIED), else a higher amount there
    stock text: "în stoc" / "disponibil" vs "stoc epuizat" / "indisponibil" / "momentan indisponibil"
  URL shapes: /{...}/{slug}-{digits}.html and /{...}/{slug}_{hex}.html  -> offer_id = digits / hex
  (a data-product-id / data-sku attribute wins when present). Brand element, when present, is prefixed.
Product page: .product-price .current-price; legacy span[itemprop=price][content]; JSON-LD / og:price:amount.
Anti-bot: Cloudflare challenge (libergent 2026-09-28: 403 to a plain fetch) -> BlockedError; >= 4 s between requests.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ..extract import extract_product_page, make_soup
from ..models import Offer
from .base import Store
from ._fashion import (
    NextPager, clean_url, dedupe, first_el, higher_money_in, image_of, join_title, landed_on_home, longest, norm,
    next_page_url, old_price_in, outermost, pick_price, raise_if_challenge, stock_from_text, text_of,
)

_ID_RE = re.compile(r"(?:-(\d{4,})|_([a-f0-9]{6,}))\.html$", re.I)


def elefant_id(url: Optional[str]) -> Optional[str]:
    m = _ID_RE.search(urlsplit(url or "").path)
    return (m.group(1) or m.group(2)) if m else None


class ElefantStore(NextPager, Store):
    key = "elefant"
    name = "elefant.ro"
    base_url = "https://www.elefant.ro"
    domains = ("elefant.ro",)
    categories = ("general", "books", "toys", "beauty", "fashion", "home")
    page_size = 24
    min_delay = 4.0
    extra_headers = {"Referer": "https://www.elefant.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/search?SearchTerm={self.q_path(query)}"

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page == 1:
            self._pager_reset()
            url = self.search_url(query)
        else:
            url = self._pager_get(page)
            if not url:
                return []
        html, soup, final = self.get_html(url)
        if landed_on_home(url, final):
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect:home"
            return []
        cards = outermost(soup.select(".product-tile, .product-item"))
        if page == 1 and elefant_id(final) and not urlsplit(final).path.startswith("/search"):
            # the search redirected to one product: its "related products" tiles are other products
            # (a heuristic h1 + first price read is trusted only when the page has no product tiles at all)
            o = self.parse_product(soup, final, html)
            if o is not None and (o.source != "elefant-pdp" or not cards):
                self.last_strategy = "product-page"
                return [o]
        out = dedupe(self._card(c, final) for c in cards)
        if out:
            self.last_strategy = "cards"
            self._pager_set(page + 1, next_page_url(soup, final))
            return out
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ parsing
    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        a = c.select_one("a.product-title[href*='.html']") or c.select_one("a.product-title[href]") \
            or c.select_one("a[href*='.html']")
        if a is None:
            return None
        url = clean_url(a.get("href"), base)
        if not url:
            return None
        title = longest(a.get_text(" ", strip=True), a.get("title"))
        brand = text_of(first_el(c, ".product-brand", ".brand", "[class*='brand']"))
        if brand and len(brand) < 40:
            title = join_title(brand, title)
        if not title:
            img = c.select_one("img[alt]")
            title = norm(img.get("alt")) if img is not None else ""
        if not title:
            return None
        box = c.select_one(".product-price") or c
        price_el, price = pick_price(box, ".current-price", ".price", "[class*='price']")
        if not price:
            return None
        old = old_price_in(box, price, exclude=[price_el])
        if not old and box is not c:
            old = higher_money_in(box, price)
        pid = None
        for el in (c, a):
            for attr in ("data-product-id", "data-sku", "data-product-sku", "data-id", "data-productid"):
                v = norm(el.get(attr))
                if v and re.fullmatch(r"[\w-]{3,40}", v):
                    pid = v
                    break
            if pid:
                break
        stock_el = first_el(c, ".product-stock", ".stock", "[class*='stoc']", "[class*='stock']", "[class*='availab']")
        stock = stock_from_text(text_of(stock_el)) if stock_el is not None else None
        if stock is None and stock_from_text(text_of(c)) is False:
            stock = False
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=pid or elefant_id(url), image=image_of(c, base, "img"), source="elefant",
        )

    # ------------------------------------------------------------------ product page
    def parse_product(self, soup: BeautifulSoup, url: str, html: str = "") -> Optional[Offer]:
        o = extract_product_page(html or str(soup), url, self.key) if html or soup else None
        if o is None:
            box = soup.select_one(".product-price")
            el, price = pick_price(box or soup, ".current-price", "span[itemprop=price]", ".price")
            h1 = soup.find("h1")
            if not price or h1 is None:
                return None
            old = old_price_in(box, price, exclude=[el]) if box is not None else None
            o = Offer(store=self.key, title=text_of(h1), url=url, price=price, old_price=old,
                      in_stock=stock_from_text(text_of(first_el(soup, ".product-stock", ".stock", "[class*='stoc']"))),
                      source="elefant-pdp")
        o.url = clean_url(url, self.base_url) or url
        o.offer_id = o.offer_id or elefant_id(url)
        o.store_name = self.name
        return o

    def product(self, url: str) -> Optional[Offer]:
        resp = self.http.get(url)
        self.last_raw = resp.text
        soup = make_soup(resp.text)
        o = self.parse_product(soup, url, resp.text)
        if o is None:
            raise_if_challenge(resp.text, soup, url)
            return None
        o.url = url
        return o
