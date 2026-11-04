"""evoMAG (evomag.ro) - custom shop (legacy "nice_product_*" markup).

Search (two routes, tried in this order, first one with product cards wins):
  1. GET https://www.evomag.ro/?sn.q={quote_plus(q)}      e.g. /?sn.q=rtx+4070  (literal dot in "sn.q")
     Search URL of a 2019 crawler and a Nov 2024 Serenity test - both ran a real browser, so the results
     may be rendered client-side; the raw HTML is checked for cards.
  2. GET https://www.evomag.ro/produse/filtru/cautare:{quote(q)}     (%20 for spaces)
     Server-rendered handler used by evoMAG's own page script (libergent, 2026-09); results live in
     `.produse_liste_filter`. A response that is the home page (`.index-category-menu`, no results
     section) carries only navigation promotions and is never parsed as search results.
  /?sn.q= is the home-page path, so outside `.produse_liste_filter` its cards count only when the page echoes
  the query (search box / title / heading); a redirect to the bare home page is never parsed. The route that
  produced cards is remembered for the process and tried first for the next query.
  Page N > 1: only the pager's next link (category listings use /filtru/pagina:N).
Cards: .nice_product_item (Mar 2026) / .nice_product_container (2019, inside div.product_grid)
    .npi_name a[href]             name + URL (often relative), /<cat>/<slug>-<id>.html
    .npi_price .real_price        "2.199,99 Lei" (decimals possibly in <sup>); "-12%" badges and
                                  "sau 125 Lei / luna" instalment lines in the price box are never the price
    old price: struck / "old" element or a second, higher price inside .npi_price (unverified)
    stock: listing markup undocumented -> text ("in stoc", "stoc magazin", "stoc epuizat", "indisponibil")
Product page (body.htmlbody_product): h1.product_name, [itemprop=price][content] (plain decimal) ->
  div.pret_rons / div.price_ajax (<sup class="price_sup">), old price div.price_ajax > div:first-child,
  instalments in .product-right-credit are ignored; stock .stock_instocmagazin / .stock_epuizat.
"""
from __future__ import annotations

import re
from typing import ClassVar, Optional

from bs4 import BeautifulSoup, Tag

from ..extract import make_soup
from ..models import Offer
from ..net import BlockedError, FetchError
from .base import Store
from ._itshops import (
    ShopMixin, clean_url, dedupe, enrich_pdp, image_of, landed_on_home, longest, next_page_url, old_price_in,
    other_higher_price, outermost, pdp_offer, pick_price, raise_if_challenge, stock_from, stock_of_el, text_of,
)

_ID_RE = re.compile(r"-(\d+)\.html$")
_NO_RESULTS_RE = re.compile(
    r"nu\s+(?:am|au|s-au)\s+(?:fost\s+)?g[aă]sit|nu\s+a\s+(?:fost\s+g[aă]sit|returnat)|niciun\s+(?:produs|rezultat)|"
    r"0\s+(?:produse|rezultate)|nu\s+exist[aă]\s+produse",
    re.I,
)


def _url_id(url: str) -> Optional[str]:
    m = _ID_RE.search(url.split("?", 1)[0])
    return m.group(1) if m else None


class EvomagStore(ShopMixin, Store):
    key = "evomag"
    name = "evoMAG"
    base_url = "https://www.evomag.ro"
    domains = ("evomag.ro",)
    categories = ("electronics", "pc", "appliances", "home")
    page_size = 24
    min_delay = 3.0
    extra_headers = {"Referer": "https://www.evomag.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/?sn.q={self.q_plus(query)}"

    def filter_search_url(self, query: str) -> str:
        return f"{self.base_url}/produse/filtru/cautare:{self.q_path(query)}"

    # route that last produced cards in this process: tried first next time, so a route that only answers with
    # the home page does not cost an extra request (and min_delay) on every query
    _preferred: ClassVar[Optional[str]] = None

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            url = self._pager_get(page)
            if not url:
                return []
            html, soup, final = self.get_html(url)
            if landed_on_home(url, final):
                return []
            offers, _ = self._parse(soup, final)
            if offers:
                self._pager_set(page + 1, next_page_url(soup, final, page))
            return offers
        self._pager_reset()
        fallback_src: Optional[tuple[str, BeautifulSoup, str, Tag]] = None
        routes = [("sn.q", self.search_url(query)), ("filtru", self.filter_search_url(query))]
        if type(self)._preferred == "filtru":
            routes.reverse()
        missing: Optional[FetchError] = None
        answered = 0
        for name, url in routes:
            try:
                html, soup, final = self.get_html(url)
            except BlockedError:
                raise
            except FetchError as e:
                if e.status in (404, 410):
                    missing = e
                    continue
                raise
            answered += 1
            if landed_on_home(url, final):
                raise_if_challenge(html, soup, final)
                continue                          # redirected to the bare home page: promotions only
            # /?sn.q= is the home-page path: without the results section its cards count only when the page
            # is recognisably the search page for this query (else they are home-page promotions)
            offers, kind = self._parse(soup, final, query=query if name == "sn.q" else None)
            if offers:
                type(self)._preferred = name
                self.last_strategy = f"cards:{name}"
                self._pager_set(2, next_page_url(soup, final, 1))
                return offers
            raise_if_challenge(html, soup, final)
            if kind == "no-results":
                self.last_strategy = f"no-results:{name}"
                return []
            if kind != "homepage" and fallback_src is None:
                fallback_src = (html, soup, final, self._section(soup) or soup)
        if not answered and missing is not None:
            raise missing
        if fallback_src is None:
            self.last_strategy = "homepage"   # both routes answered with the home page (JS-only search?)
            return []
        html, soup, final, section = fallback_src
        if section is soup:
            return self.fallback(html, final, soup)
        return self.fallback(str(section), final, make_soup(str(section)))

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _section(soup: BeautifulSoup) -> Optional[Tag]:
        return soup.select_one(".produse_liste_filter") or soup.select_one("div.product_grid")

    @staticmethod
    def _echoes_query(soup: BeautifulSoup, query: str) -> bool:
        q = " ".join(query.lower().split())
        if not q:
            return False
        for inp in soup.find_all("input", attrs={"name": "sn.q"}):
            if " ".join(str(inp.get("value") or "").lower().split()) == q:
                return True
        heads = [text_of(soup.title)] + [text_of(h) for h in soup.find_all(["h1", "h2"], limit=3)]
        return any(q in h.lower() for h in heads)

    def _parse(self, soup: BeautifulSoup, base: str, query: Optional[str] = None) -> tuple[list[Offer], str]:
        """`query` set = strict mode (home-page path): outside `.produse_liste_filter` cards are accepted only
        when the page echoes the query in its search box / title / heading."""
        section = soup.select_one(".produse_liste_filter")
        if section is None:
            if soup.select_one(".index-category-menu") is not None:
                return [], "homepage"
            if query is not None and not self._echoes_query(soup, query):
                return [], "homepage"
            section = soup.select_one("div.product_grid") or soup
        cards = outermost(section.select(".nice_product_item, .nice_product_container"))
        offers = dedupe(self._card(c, base) for c in cards)
        if offers:
            return offers, "cards"
        if section is not soup and _NO_RESULTS_RE.search(text_of(section)):
            return [], "no-results"
        return [], "none"

    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        name_box = c.select_one(".npi_name")
        a = (name_box.select_one("a[href]") if name_box is not None else None) or c.select_one("a[href$='.html']")
        if a is None:
            return None
        url = clean_url(a.get("href"), base)
        img = c.select_one("img[alt]")
        title = longest(a.get_text(" ", strip=True), a.get("title")) or (img.get("alt") if img is not None else "")
        if not url or not title:
            return None
        box = c.select_one(".npi_price") or c
        price_el, price = pick_price(box, ".real_price")
        if not price:
            price_el, price = pick_price(c, ".real_price", "[class*='price']")
        if not price:
            return None
        old = old_price_in(box, price, exclude=[price_el]) or other_higher_price(box, price_el, price)
        stock = stock_of_el(c.select_one("[class*='stock_'], [class*='stoc_'], .npi_stock, [class*='availability']"))
        if stock is None:
            stock = stock_from((), text_of(c))
        pid = _url_id(url)
        if not pid:
            el = c.select_one("[data-product-id], [data-pid], .nice_add_to_cart [data-id]")
            if el is not None:
                pid = el.get("data-product-id") or el.get("data-pid") or el.get("data-id")
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=str(pid) if pid else None, image=image_of(c, base), source="evomag",
        )

    # ------------------------------------------------------------------ product page
    _PDP_OLD = ("div.price_ajax > div:first-child", ".price_ajax del", ".pret_vechi", ".old_price")
    _PDP_STOCK = (".stock_instocmagazin", ".stock_epuizat", "[class*='stock_']")
    _PDP_TITLE = ("h1.product_name", "h1")

    def product(self, url: str) -> Optional[Offer]:
        o = super().product(url)
        raw = self.last_raw or ""
        soup = make_soup(raw) if raw else None
        if soup is not None:
            for el in soup.select(".product-right-credit"):
                el.extract()        # instalment amounts
        if o is not None:
            o.offer_id = _url_id(url) or o.offer_id
            return enrich_pdp(o, soup, title_sels=self._PDP_TITLE, stock_sels=self._PDP_STOCK, old_sels=self._PDP_OLD)
        if soup is None:
            return None
        raise_if_challenge(raw, soup, url)
        return pdp_offer(self, soup, url,
                         price_sels=('[itemprop="price"][content]', "div.pret_rons", "div.price_ajax"),
                         old_sels=self._PDP_OLD, stock_sels=self._PDP_STOCK, title_sels=self._PDP_TITLE,
                         offer_id=_url_id(url), source="evomag-pdp")
