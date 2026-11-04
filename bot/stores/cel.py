"""CEL.ro (cel.ro) - custom shop, server-rendered listings.

Search: GET https://www.cel.ro/cauta/{quote(q)}/     path segment, %20 for spaces, e.g. /cauta/rtx%204070/
        (2019 crawler: 'cauta/' + urllib.parse.quote(q); redirects are followed).
        If that answers 404 or a page without product cards, the slug form libergent used successfully on
        2026-09-28 is tried once: /cauta/{slug}/ (lowercase, no diacritics, non-alphanumerics -> '-'),
        e.g. /cauta/ssd-samsung-1tb/ (13 and 12 accepted offers in libergent's direct check).
        The form that produced cards is remembered for the process and tried first for the next query.
        A redirect to the bare home page is never parsed (its promotion cards are not results).
        Page N > 1: only the pager's next link - the next page NUMBER first, `a.last` (2024 scraper; may be
        the "last page" >> arrow) only as a last resort.
Cards (libergent 2026-09, Pretzi 2026-03, 2024 Selenium scraper):
  div.product_data.productListing-tot[data-pid_prod]     (in #listProducts)
    .productListing-poza > a > img
    h2.productTitle a[href] > span      name; also a.product_link
    .pret_n span.price[content]         "1016" + <span class="moneda">lei</span> (usually whole lei)
    old price: .pret_n .price_initial / .pret_vechi / del.price
    .stoc_list.infoStocElem strong.info_stoc.in_stoc | .out_stoc | .no_stoc
  URL https://www.cel.ro/<cat>/<slug>-p<CODE>-l/ -> id <CODE> (>= 6 chars with upper-case letters, so a slug
  ending like "-macbook-pro/" is not a code; fallback data-pid_prod).
Product page: #product-price / .pret_n .price / [itemprop=price]; old .pret_n .price_initial; stock .info_stoc.
Anti-bot: nothing strong known; intermittent Cloudflare 522/timeouts (FetchError, not "blocked").
"""
from __future__ import annotations

import re
import unicodedata
from typing import ClassVar, Optional

from bs4 import BeautifulSoup, Tag

from ..extract import make_soup
from ..models import Offer
from ..net import BlockedError, FetchError
from .base import Store
from ._itshops import (
    ShopMixin, clean_url, dedupe, enrich_pdp, image_of, landed_on_home, longest, next_page_url, num_attr,
    old_price_in, outermost, pdp_offer, pdp_title, pick_price, raise_if_challenge, stock_of_el, text_of,
)

_CODE_RE = re.compile(r"-p([A-Za-z0-9]{6,})(?:-l)?/?$")
_JSON_PRICE_RE = re.compile(r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?')
_NO_RESULTS_RE = re.compile(
    r"nu\s+(?:am|au)\s+(?:fost\s+)?g[aă]sit|nu\s+exist[aă]\s+(?:produse|rezultate)|niciun\s+(?:produs|rezultat)|"
    r"0\s+(?:produse|rezultate)\s+(?:g[aă]sit|pentru)",
    re.I,
)


def _code(url: str) -> Optional[str]:
    """CEL product code from ".../<slug>-p<CODE>-l/" (e.g. pMSQ0NDA2MiU). Codes are base64-like and carry
    upper-case letters; a plain slug ending ("...-macbook-pro/", "...-huawei-p30/") is NOT a code - taking it
    would give different products the same id ("ro", "30") and dedupe would silently drop all but one."""
    m = _CODE_RE.search(url.split("?", 1)[0])
    if not m or not any(ch.isupper() for ch in m.group(1)):
        return None
    return m.group(1)


def slugify(query: str) -> str:
    s = unicodedata.normalize("NFD", query.strip().lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


class CelStore(ShopMixin, Store):
    key = "cel"
    name = "CEL.ro"
    base_url = "https://www.cel.ro"
    domains = ("cel.ro",)
    categories = ("electronics", "pc", "appliances", "home", "diy", "toys", "general")
    page_size = 24
    min_delay = 2.5
    extra_headers = {"Referer": "https://www.cel.ro/", "Upgrade-Insecure-Requests": "1"}

    # route that last produced cards in this process ("path" = /cauta/<%20>/, "slug" = /cauta/<slug>/): tried
    # first next time, so a dead form does not cost an extra request (and min_delay) on every query
    _preferred: ClassVar[Optional[str]] = None

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/cauta/{self.q_path(query)}/"

    def slug_search_url(self, query: str) -> str:
        return f"{self.base_url}/cauta/{slugify(query)}/"

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            url = self._pager_get(page)
            if not url:
                return []
            html, soup, final = self.get_html(url)
            if landed_on_home(url, final):
                return []
            out = self._parse(soup, final)
            if out:
                self._pager_set(page + 1, next_page_url(soup, final, page, ("a.last[href]",)))
            return out
        self._pager_reset()
        routes = [("path", self.search_url(query))]
        alt = self.slug_search_url(query)
        if slugify(query) and alt != routes[0][1]:
            routes.append(("slug", alt))
        if type(self)._preferred == "slug" and len(routes) == 2:
            routes.reverse()
        first: Optional[tuple[str, BeautifulSoup, str]] = None
        missing: Optional[FetchError] = None
        said_none = False
        went_home = False
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
            if landed_on_home(url, final):
                raise_if_challenge(html, soup, final)
                went_home = True                 # home-page promotions are not search results
                continue
            out = self._parse(soup, final)
            if out:
                type(self)._preferred = name
                self.last_strategy = "cards" if name == "path" else "cards:slug"
                self._pager_set(2, next_page_url(soup, final, 1, ("a.last[href]",)))
                return out
            raise_if_challenge(html, soup, final)
            if first is None:
                first = (html, soup, final)
            # an explicit "nothing found" may be an encoding issue of this route -> still try the next one
            said_none = bool(_NO_RESULTS_RE.search(
                text_of(soup.select_one("#listProducts") or soup.select_one("main") or soup)))
        if first is None:
            if went_home:
                self.last_strategy = "redirect:home"
                return []
            raise missing or FetchError("no search route answered", None, routes[0][1])
        if said_none:
            self.last_strategy = "no-results"
            return []
        html, soup, final = first
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ parsing
    def _parse(self, soup: BeautifulSoup, base: str) -> list[Offer]:
        root = soup.select_one("#listProducts") or soup
        cards = outermost(root.select("div.product_data"))
        return dedupe(self._card(c, base) for c in cards)

    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        a = (c.select_one("h2.productTitle a[href]") or c.select_one(".productTitle a[href]")
             or c.select_one("a.product_link[href]") or c.select_one(".productListing-nume a[href]"))
        if a is None:
            return None
        url = clean_url(a.get("href"), base)
        span = a.find("span")
        img = c.select_one(".productListing-poza img")
        title = longest(span.get_text(" ", strip=True) if span else "", a.get_text(" ", strip=True), a.get("title"))
        if not title and img is not None:
            title = img.get("alt") or ""
        if not url or not title:
            return None
        price_el, price = pick_price(c, ".pret_n .price", "span.price[content]", "[itemprop='price']", ".price")
        if not price:
            return None
        old = old_price_in(c, price, exclude=[price_el])
        pid = _code(url) or (str(c.get("data-pid_prod")).strip() if c.get("data-pid_prod") else None)
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock_of_el(c.select_one(".info_stoc") or c.select_one(".stoc_list")),
            offer_id=pid or None, image=image_of(c, base, ".productListing-poza img, img"), source="cel",
        )

    # ------------------------------------------------------------------ product page
    _PDP_OLD = (".pret_n .price_initial", ".pret_vechi", "del.price")

    def product(self, url: str) -> Optional[Offer]:
        o = super().product(url)
        raw = self.last_raw or ""
        soup = make_soup(raw) if raw else None
        if o is not None:
            o.offer_id = _code(url) or o.offer_id
            return enrich_pdp(o, soup, stock_sels=(".info_stoc",), old_sels=self._PDP_OLD)
        if soup is None:
            return None
        raise_if_challenge(raw, soup, url)
        o = pdp_offer(self, soup, url, price_sels=("#product-price", ".pret_n .price", "[itemprop='price']"),
                      old_sels=self._PDP_OLD, stock_sels=(".info_stoc",), offer_id=_code(url), source="cel-pdp")
        if o is None:          # research: the price is also in page scripts as "price": <plain decimal>
            m = _JSON_PRICE_RE.search(raw)
            price = num_attr(m.group(1)) if m else None
            title = pdp_title(soup)
            if price and title:
                o = Offer(store=self.key, title=title, url=url, price=price, offer_id=_code(url),
                          in_stock=stock_of_el(soup.select_one(".info_stoc")), store_name=self.name,
                          source="cel-pdp-json")
        return o
