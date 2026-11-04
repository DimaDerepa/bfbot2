"""ShopMania.ro - price comparator (ShopMania Group platform, same code as shopmania.rs / .bg / .hu).

Search: GET https://www.shopmania.ro/q-{slug}
    slug = lowercase, diacritics stripped (NFKD), every run of non-[a-z0-9] chars -> '-', trimmed
    e.g. "iPhone 5 husă" -> https://www.shopmania.ro/q-iphone-5-husa  (SEO search pages indexed by search engines)
  When /q-{slug} answers 404, shows the "Ooops" no-results page or redirects home (it is an SEO landing page that
    need not exist for an arbitrary query): GET https://www.shopmania.ro/shopping?q={quote(q)}  (spaces -> %20;
    libergent's route, HTTP 200 in Sept 2026; redirects are followed).
  A search that lands on a product page (/<cat>/p-<slug>-<id>) gives one Offer from that page.
  Page N > 1: only the pager's next link (search pagination is undocumented; categories use /<cat>/p<N>).
Cards (verified on shopmania.rs by a 2025/26 scraper; the .ro site runs the same platform - unverified):
  .prod-item
    h2 > a[href]             relative product page /<category>/p-<slug>-<id>, or the absolute outbound
                             https://www.shopmania.ro/redirect?d=<opaque> for single-shop items (never fetched;
                             kept as the clickable shop link, offer id then derived from the title)
    .prod-price              "de la 2.896,00 RON" (min over shops) | "2.896,00 RON"
    .prod-item-img-wrap img  data-src before src; placeholder https://s.cdnshm.com/img/site/na.svg ignored
    .grid-image.grid-image--out-of-stock  -> out of stock
  h2.h4.serif.mb-2 "Ooops..."  -> no results.
  Fallback: anchors /<cat>/p-<slug>-<id> + the nearest "NNN,NN RON" text, then generic extraction.
Product page https://www.shopmania.ro/<category>/p-<slug>-<id>: JSON-LD (unverified), else the header text
  "Vezi 37 oferte, de la 2.896,00 RON" (shop count + min price), else generic microdata/meta (the first
  itemprop=price of a comparator page is only the first listed shop). "Cost livrare de la 28,00 RON" is shipping.
Prices: dot thousands, decimal comma, "RON".
Anti-bot: none observed (plain HTTP 200 from local Node and a Cloudflare Worker, Sept 2026).
Search relevance is poor (mostly accessories): the matcher's strict / accessory filters do the rest.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..extract import extract_product_page
from ..models import Offer
from ..net import FetchError
from ..prices import parse_price
from .base import Store
from ._aggregators import (
    NextPager, clean_url, count_in, current_and_old, dedupe, image_of, link_cards, next_page_url, outermost,
    raise_if_challenge, slugify, strip_noise, text_of,
)

_ID_RE = re.compile(r"/p-[a-z0-9-]+-(\d{4,})/?$")
_PRICE_RE = re.compile(r"(?<![\w.,])((?:\d{1,3}(?:[.\s]\d{3})+|\d+),\d{2})\s*RON\b", re.I)
_HEADER_RE = re.compile(r"Vezi\s+(\d+)\s+oferte,\s*de la\s*(\d[\d.\s]*,\d{2})\s*RON", re.I)
_REDIRECT_RE = re.compile(r"/redirect\b", re.I)


def shopmania_id(url: Optional[str]) -> Optional[str]:
    m = _ID_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def _redirect_url(href: str, base: str) -> str:
    """Outbound /redirect?d=<opaque>: `d` IS the destination, so only it is kept (other params dropped)."""
    p = urlsplit(urljoin(base, href.strip()))
    keep = [(k, v) for k, v in parse_qsl(p.query) if k == "d"]
    return urlunsplit(("https", p.netloc.lower(), p.path, urlencode(keep), ""))


class ShopmaniaStore(NextPager, Store):
    key = "shopmania"
    name = "ShopMania"
    base_url = "https://www.shopmania.ro"
    domains = ("shopmania.ro",)
    categories = ("electronics", "pc", "home", "appliances", "fashion", "beauty", "general", "diy", "toys", "books")
    aggregator = True
    page_size = 20
    min_delay = 2.0
    extra_headers = {"Referer": "https://www.shopmania.ro/"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/q-{slugify(query)}"

    def shopping_url(self, query: str) -> str:
        return f"{self.base_url}/shopping?q={quote(query.strip(), safe='')}"

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            url = self._pager_get(page)
            if not url:
                return []
            html, soup, final = self.get_html(url)
            return self._results(html, soup, final, page)
        self._pager_reset()
        if not slugify(query):
            return []
        try:
            html, soup, final = self.get_html(self.search_url(query))
        except FetchError as e:
            if e.status != 404:
                raise
            return self._shopping(query)
        if self._is_home(final) or (soup.select_one(".prod-item") is None and self._empty_page(soup)):
            # /q-<slug> is an SEO landing page that need not exist for an arbitrary query -> the site search
            return self._shopping(query)
        return self._results(html, soup, final, 1)

    def _shopping(self, query: str) -> list[Offer]:
        html, soup, final = self.get_html(self.shopping_url(query))
        return self._results(html, soup, final, 1)

    def _results(self, html: str, soup: BeautifulSoup, final: str, page: int) -> list[Offer]:
        if self._is_home(final):
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect-home"       # home page promos are not search results
            return []
        if page == 1 and shopmania_id(final):          # landed straight on a product page
            o = self.parse_product_page(html, soup, clean_url(final, self.base_url))
            if o is not None:
                o.store_name = self.name
                self.last_strategy = "product-page"
                return [o]
        out = self.parse_listing(soup, final)
        if out:
            self._pager_set(page + 1, next_page_url(soup, final))
            return out
        if self._empty_page(soup):
            self.last_strategy = "no-results"
            return []
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    @staticmethod
    def _is_home(url: str) -> bool:
        p = urlsplit(url)
        return p.netloc.lower() in ("shopmania.ro", "www.shopmania.ro") and p.path in ("", "/")

    @staticmethod
    def _empty_page(soup: BeautifulSoup) -> bool:
        return any("ooops" in text_of(h).lower() for h in soup.select("h2.h4.serif.mb-2, h1, h2"))

    # ------------------------------------------------------------------ listing
    def parse_listing(self, soup: BeautifulSoup, base: str) -> list[Offer]:
        cards = outermost(soup.select(".prod-item"))
        if cards:
            out = dedupe(self._card(c, base) for c in cards)
            if out:
                self.last_strategy = "cards"
                return out
        out = link_cards(soup, base, self.key, _ID_RE, (_PRICE_RE,), source="shopmania-links")
        if out:
            self.last_strategy = "product-links"
        return out

    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        h2 = c.select_one("h2")
        a = c.select_one("h2 > a[href]") or c.select_one("h2 a[href]")
        title = text_of(h2) or ((a.get("title") or "").strip() if a is not None else "")
        if not title:
            img = c.select_one("img[alt]")
            title = (img.get("alt") or "").strip() if img is not None else ""
        url, pid, outbound = "", None, False
        for cand in [a, *c.select("a[href]")]:
            if cand is None:
                continue
            u = clean_url(cand.get("href"), base)
            if u and shopmania_id(u):
                url, pid = u, shopmania_id(u)
                break
        if not url and a is not None and a.get("href"):
            href = a["href"]
            outbound = bool(_REDIRECT_RE.search(href))
            url = _redirect_url(href, base) if outbound else clean_url(href, base)
        if not title or not url:
            return None
        price, old = current_and_old(c.select_one(".prod-price"))
        if not price:
            return None
        if not pid:
            pid = "t-" + slugify(title)[:90]   # outbound redirect link: no product id, keep the key stable
        stock = False if c.select_one(".grid-image--out-of-stock, .grid-image.grid-image--out-of-stock") else None
        seller_el = c.select_one(".prod-shop, .prod-store, .prod-merchant, .merchant-name, .shop-name")
        shop_count = count_in(strip_noise(text_of(c)).replace(title, " "))
        if shop_count is None and outbound:
            shop_count = 1                     # /redirect?d= = the item is sold by a single shop
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old, in_stock=stock, offer_id=pid,
            image=image_of(c, base, ".prod-item-img-wrap img, img"),
            seller=text_of(seller_el) or None, shop_count=shop_count, source="shopmania",
        )

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
        """JSON-LD (Product / AggregateOffer) > header "Vezi N oferte, de la X RON" > generic microdata / meta.
        On a comparator page the first itemprop=price is just the first listed shop, not the minimum."""
        generic = extract_product_page(html, url, self.key)
        m = _HEADER_RE.search(text_of(soup.body or soup))
        o = generic if generic is not None and generic.source == "jsonld" else None
        if o is None and m:
            price = parse_price(m.group(2))
            title = text_of(soup.find("h1")) or (generic.title if generic is not None else "")
            if price and title:
                o = Offer(store=self.key, title=title, url=url, price=price, source="shopmania-pdp")
        o = o or generic
        if o is None:
            return None
        o.url = url
        o.offer_id = shopmania_id(url) or o.offer_id
        if m and not o.shop_count:
            o.shop_count = int(m.group(1))
        return o
