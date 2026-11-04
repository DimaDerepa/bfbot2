"""Price.ro - price comparator (custom PHP site behind Cloudflare). EXPERIMENTAL: no verified selectors.

Search: GET https://www.price.ro/rezultate~{slug}.html
    slug: lowercase, diacritics stripped, every run of non-alphanumerics -> '-', and digit->letter boundaries split
    the way the site does it ('256gb' -> '256-gb', '5g' -> '5-g'):
    "Apple iPhone 15 Pro 256GB" -> https://www.price.ro/rezultate~apple-iphone-15-pro-256-gb.html
  When that answers 404: the legacy form endpoint
    https://www.price.ro/index.php?action=q&text={quote_plus(q)}&submit=Cauta   (probably redirects)
  Page N > 1: only the pager's next link.
Parsing - no open-source parser exists and every scraper seen was Cloudflare-blocked, so this is inferred from
  indexed result pages (each product: lowest price "de la 5.920 lei" + "14 magazine" + a link to
  /preturi-<slug>-<id>):
  1. JSON-LD (ItemList / Product + AggregateOffer) when the page has it
  2. product links /preturi-<slug>-<id> + the nearest "de la X lei" (min price, else any "X lei") and
     "N magazine" (shop count) inside the outermost container that holds only that product
  3. generic extraction
  The first diag run must save the raw HTML before these results are trusted.
  Shipping ("Transport 19,99 lei") and instalments ("Rate de la 247 lei", "247 lei/lună") are blanked out first.
  A redirect to the home page -> []; straight to a product page -> one Offer from that page.
Product page https://www.price.ro/preturi-<slug>-<id>: JSON-LD, else the "de la X lei" / "N magazine" text, else
  generic microdata/meta (the first itemprop=price of a comparator page is only the first listed shop).
Prices: "5.920 lei", "3.967,99 lei", "9.799 lei" (dot thousands, optional comma decimals, VAT included).
Anti-bot: Cloudflare challenge - 403 for libergent in Sept 2026 (local and from a Cloudflare Worker), "not
  evaluated, Cloudflare-blocked" in Nov 2026 -> BlockedError (no retry). Expected to be blocked on GitHub Actions.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..extract import extract_product_page, offers_from_jsonld
from ..models import Offer
from ..net import FetchError
from ..prices import parse_price
from .base import Store
from ._aggregators import (
    PRICE_RE, NextPager, clean_url, count_in, dedupe, link_cards, next_page_url, raise_if_challenge, slugify,
    strip_noise, text_of,
)

_ID_RE = re.compile(r"^/preturi-[a-z0-9-]+-(\d{4,})/?$")
# min price over shops; callers run it on strip_noise()d text so "Rate de la 247 lei/lună" never matches
_DE_LA_RE = re.compile(r"de\s+la\s*((?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?)\s*(?:lei|ron)\b", re.I)


def pricero_id(url: Optional[str]) -> Optional[str]:
    m = _ID_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def pricero_slug(query: str) -> str:
    return re.sub(r"(\d)([a-z])", r"\1-\2", slugify(query))


class PriceRoStore(NextPager, Store):
    key = "pricero"
    name = "Price.ro"
    base_url = "https://www.price.ro"
    domains = ("price.ro",)
    categories = ("electronics", "pc", "home", "appliances", "general", "diy", "toys")
    aggregator = True
    page_size = 20
    min_delay = 5.0
    extra_headers = {"Referer": "https://www.price.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/rezultate~{pricero_slug(query)}.html"

    def legacy_url(self, query: str) -> str:
        return f"{self.base_url}/index.php?action=q&text={self.q_plus(query)}&submit=Cauta"

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page == 1:
            self._pager_reset()
            if not pricero_slug(query):
                return []
            try:
                html, soup, final = self.get_html(self.search_url(query))
            except FetchError as e:
                if e.status != 404:
                    raise
                html, soup, final = self.get_html(self.legacy_url(query))
        else:
            url = self._pager_get(page)
            if not url:
                return []
            html, soup, final = self.get_html(url)
        fp = urlsplit(final)
        if fp.netloc.lower() in ("price.ro", "www.price.ro") and fp.path in ("", "/", "/index.php") \
                and "text=" not in fp.query:
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect-home"          # home page promos are not search results
            return []
        if page == 1 and pricero_id(final):               # landed straight on a product page
            o = self.parse_product_page(html, soup, clean_url(final, self.base_url))
            if o is not None:
                o.store_name = self.name
                self.last_strategy = "product-page"
                return [o]
        out = self.parse_listing(soup, final)
        if out:
            self._pager_set(page + 1, next_page_url(soup, final))
            return out
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    def parse_listing(self, soup: BeautifulSoup, base: str) -> list[Offer]:
        lds = [o for o in offers_from_jsonld(soup, base, self.key) if pricero_id(o.url)]
        for o in lds:
            o.url = clean_url(o.url, base)
            o.offer_id = pricero_id(o.url)
        if len(lds) >= 2:
            self.last_strategy = "jsonld"
            return dedupe(lds)
        out = link_cards(soup, base, self.key, _ID_RE, (_DE_LA_RE, PRICE_RE), source="pricero-links")
        if out:
            self.last_strategy = "product-links"
            return out
        if lds:
            self.last_strategy = "jsonld"
        return lds

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        html, soup, final = self.get_html(url)
        o = self.parse_product_page(html, soup, url)
        if o is None:
            raise_if_challenge(html, soup, final)
            return None
        o.store_name = self.name
        return o

    def parse_product_page(self, html: str, soup: BeautifulSoup, url: str) -> Optional[Offer]:
        """JSON-LD (Product / AggregateOffer) > "de la X lei" min price > generic microdata / meta
        (on a comparator page the first itemprop=price is just the first listed shop, not the minimum)."""
        generic = extract_product_page(html, url, self.key)
        text = strip_noise(text_of(soup.body or soup))
        m = _DE_LA_RE.search(text)
        o = generic if generic is not None and generic.source == "jsonld" else None
        if o is None and m:
            price = parse_price(m.group(1))
            title = text_of(soup.find("h1")) or (generic.title if generic is not None else "")
            if price and title:
                o = Offer(store=self.key, title=title, url=url, price=price, source="pricero-pdp")
        o = o or generic
        if o is None:
            return None
        o.url = url
        o.offer_id = pricero_id(url) or o.offer_id
        if not o.shop_count and m:   # "de la 5.920 lei · 14 magazine" - only next to the min price, not the footer
            o.shop_count = count_in(text[max(0, m.start() - 150):m.end() + 150])
        return o
