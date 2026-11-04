"""ShopMania.ro - price comparator (ShopMania Group platform, same code as shopmania.rs / .bg / .hu).

Search: GET https://www.shopmania.ro/q-{slug}
    slug = lowercase, diacritics stripped (NFKD), every run of non-[a-z0-9] chars -> '-', trimmed
    e.g. "iPhone 5 husă" -> https://www.shopmania.ro/q-iphone-5-husa  (SEO search pages indexed by search engines)
  When /q-{slug} answers 404, shows the "Ooops" no-results page or redirects home (it is an SEO landing page that
    need not exist for an arbitrary query): GET https://www.shopmania.ro/shopping?q={quote(q)}  (spaces -> %20;
    libergent's route, HTTP 200 in Sept 2026; redirects are followed).
  A search that lands on a product page (/<cat>/p-<slug>-<id>) gives one Offer from that page.
  Page N > 1: only the pager's next link (search pagination is undocumented; categories use /<cat>/p<N>).
Cards (verified on real shopmania.ro search pages captured Nov 2026):
  ul.prod-list > li.prod-item#p-<id>          <id> = ShopMania product id (stable offer id)
    multi-shop product (has a ShopMania product page):
      h2 > a.prod-item-name[href]             absolute /<category>/p-<slug>-<id>
      .prod-interval  "de la" + .prod-price "5.739,<sup>00</sup> RON" (min over shops; "de la" only when > 1)
      span.prod-offers "5 oferte"             shop count (absent when a single shop lists it)
    single-shop item (no product page; the shop link is built by JS from data-submit):
      span.prod-item-name.gtm-click[data-submit="d=<opaque>"][data-tracking='{"id":..,"name":..,"price":139.9}']
                                              JS turns it into <a href="/redirect?d=<opaque>">, so the offer URL is
                                              https://www.shopmania.ro/redirect?d=<opaque> (never fetched)
      .prod-item-shipping "In stoc | 22,00 RON" | "Disponibil la comanda | Nu include livrarea" | "Stoc epuizat"
                                              -> stock; the price in it is SHIPPING, never the product price
      .prod-item-merchant-link[title=<shop domain>] > .clamp-1 <shop name>   -> seller, shop_count = 1
    .prod-item-img-wrap img                   data-src before src; placeholder https://s.cdnshm.com/img/site/na.svg
    no struck/old price is shown on search cards; del/s/*old* nodes inside the price box are still honoured.
  Category strips (.strip-item a[href="/<category>?q=..."]) and menus are not cards.
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

import copy
import json
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
    NextPager, clean_url, count_in, dedupe, image_of, link_cards, next_page_url, norm_ws, outermost,
    raise_if_challenge, slugify, strip_noise, text_of,
)

_ID_RE = re.compile(r"/p-[a-z0-9-]+-(\d{4,})/?$")
_PRICE_RE = re.compile(r"(?<![\w.,])((?:\d{1,3}(?:[.\s]\d{3})+|\d+),\d{2})\s*RON\b", re.I)
_HEADER_RE = re.compile(r"Vezi\s+(\d+)\s+oferte,\s*de la\s*(\d[\d.\s]*,\d{2})\s*RON", re.I)
_REDIRECT_RE = re.compile(r"/redirect\b", re.I)
_CARD_ID_RE = re.compile(r"p-(\d{4,})$")
_AMOUNT_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,(\d{1,2}))?\s*(?:RON|lei)\b", re.I)
_OLD_CLS_RE = re.compile(r"(?:^|[-_])(?:old|vechi|initial|strike|striked|crossed|before|was|prp|rrp)(?:[-_]|$)", re.I)
_OUT_TXT = re.compile(r"stoc\s+epuizat|\bepuizat|indisponibil|nu\s+(?:este|e)\s+(?:[iî]n\s+)?stoc|f[aă]r[aă]\s+stoc", re.I)
_IN_TXT = re.compile(r"[iî]n\s+stoc|\bdisponibil\b|la\s+comand[aă]|precomand[aă]|ultimele\s+buc", re.I)


def shopmania_id(url: Optional[str]) -> Optional[str]:
    m = _ID_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def _redirect_url(href: str, base: str) -> str:
    """Outbound /redirect?d=<opaque>: `d` IS the destination, so only it is kept (other params dropped)."""
    p = urlsplit(urljoin(base, href.strip()))
    keep = [(k, v) for k, v in parse_qsl(p.query) if k == "d"]
    return urlunsplit(("https", p.netloc.lower(), p.path, urlencode(keep), ""))


def _tracking(c: Tag) -> dict:
    """data-tracking='{"id":633039617,"name":"..","type":"product","price":139.9,"currency":"RON"}' of a card."""
    el = c.select_one("[data-tracking]")
    if el is None:
        return {}
    try:
        d = json.loads(el.get("data-tracking") or "")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _amount(text: str) -> Optional[float]:
    """First "5.739,00 RON" / "53,99 RON" / "2.896 RON" in `text` (dot thousands, decimal comma)."""
    m = _AMOUNT_RE.search(text or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(".", "").replace(" ", "") + "." + (m.group(2) or "0"))
    except ValueError:
        return None
    return v if v > 0 else None


def _price_text(el: Tag) -> str:
    """Visible text of a price node with "5.739,<sup>00</sup>" / "5.739<sup>00</sup>" glued to "5.739,00"."""
    el = copy.copy(el)
    for sup in el.find_all("sup"):
        t = sup.get_text("", strip=True).lstrip(",.")
        sup.replace_with("\u0001" + t if t.isdigit() and len(t) <= 2 else " " + t)
    s = norm_ws(el.get_text(" ").replace("\u00a0", " ").replace("\u202f", " "))
    s = re.sub(r"(\d)\s*,?\s*\u0001(\d)", r"\1,\2", s)       # "5.739, \x0100" -> "5.739,00"
    s = re.sub(r"(\d),\s+(\d{2})(?!\d)", r"\1,\2", s)          # "53, 99" (separate text nodes)
    return s.replace("\u0001", " ")


def _is_old(el: Tag) -> bool:
    return el.name in ("del", "s", "strike") or any(_OLD_CLS_RE.search(k) for k in (el.get("class") or []))


def _price_and_old(box: Optional[Tag]) -> tuple[Optional[float], Optional[float]]:
    """(current, old) of a card price box; old only from a struck / "*old*" node and only when above current."""
    if box is None:
        return None, None
    box = copy.copy(box)
    olds = []
    for x in outermost(x for x in box.find_all(True) if _is_old(x)):
        v = _amount(_price_text(x))
        if v:
            olds.append(v)
        x.extract()
    cur_el = box if "prod-price" in (box.get("class") or []) else (box.select_one(".prod-price") or box)
    cur = _amount(_price_text(cur_el)) or _amount(_price_text(box))
    old = max(olds) if olds else None
    return cur, (old if cur and old and old > cur else None)


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
        cards = outermost(soup.select(".prod-list .prod-item")) or outermost(soup.select(".prod-item"))
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
        track = _tracking(c)
        name = c.select_one(".prod-item-name") or c.select_one("h2 a, h2")
        a = c.select_one("h2 a[href]") or (name if name is not None and name.name == "a" and name.get("href") else None)
        title = text_of(name) or str(track.get("name") or "").strip()
        if not title and a is not None:
            title = (a.get("title") or "").strip()
        if not title:
            img = c.select_one("img[alt]")
            title = (img.get("alt") or "").strip() if img is not None else ""
        title = norm_ws(title)

        url, pid, outbound = "", None, False
        for cand in [a, *c.select("a[href]")]:
            if cand is None:
                continue
            u = clean_url(cand.get("href"), base)
            if u and shopmania_id(u):
                url, pid = u, shopmania_id(u)
                break
        if not url:
            href = (a.get("href") or "") if a is not None else ""
            if not href:
                sub = c.select_one("[data-submit]")      # single-shop item: JS builds <a href="/redirect?{data-submit}">
                if sub is not None and (sub.get("data-submit") or "").strip():
                    href = "/redirect?" + sub["data-submit"].strip().lstrip("?")
            if href:
                outbound = bool(_REDIRECT_RE.search(href))
                url = _redirect_url(href, base) if outbound else clean_url(href, base)
        if not title or not url:
            return None
        if not pid:
            m = _CARD_ID_RE.match(c.get("id") or "")
            pid = m.group(1) if m else (str(track["id"]) if str(track.get("id") or "").isdigit() else None)
        if not pid:
            pid = "t-" + slugify(title)[:90]   # outbound redirect link without product id: keep the key stable

        box = c.select_one(".prod-interval") or c.select_one(".prod-price")
        price, old = _price_and_old(box)
        if not price:
            try:
                price = float(track.get("price") or 0) or None
            except (TypeError, ValueError):
                price = None
        if not price:
            return None

        stock: Optional[bool] = None
        if c.select_one(".grid-image--out-of-stock"):
            stock = False
        else:
            ship = text_of(c.select_one(".prod-item-shipping")).lower()
            if _OUT_TXT.search(ship):
                stock = False
            elif _IN_TXT.search(ship):
                stock = True

        seller = None
        merchant = c.select_one(".prod-item-merchant-link")
        if merchant is not None:
            seller = text_of(merchant.select_one(".clamp-1") or merchant) or (merchant.get("title") or "").strip()
        if not seller:
            seller = text_of(c.select_one(".prod-shop, .prod-store, .prod-merchant, .merchant-name, .shop-name"))

        offers_el = c.select_one(".prod-offers")
        shop_count = count_in(text_of(offers_el)) if offers_el is not None else None
        if shop_count is None:
            rest = copy.copy(c)
            for x in rest.select(".prod-desc, .prod-item-shipping, h2"):
                x.extract()
            shop_count = count_in(strip_noise(text_of(rest)))
        if shop_count is None and (outbound or merchant is not None):
            shop_count = 1                     # /redirect?d= = the item is sold by a single shop
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old, in_stock=stock, offer_id=pid,
            image=image_of(c, base, ".prod-item-img-wrap img") or image_of(c, base, "img:not(.prod-item-merchant-img)"),
            seller=seller or None, shop_count=shop_count, source="shopmania",
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
