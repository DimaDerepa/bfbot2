"""Vexio (vexio.ro) - custom shop (Bootstrap 3 markup), Cloudflare in front.

Search: route UNVERIFIED (no source documents it). Tried in this order, the first that answers with product
  cards (or is recognisably a search page for the query) is remembered for the rest of the process:
    1. https://www.vexio.ro/cauta/?q={quote_plus(q)}             (research best guess)
    2. the home page's own GET search <form> (action + text input name, hidden inputs kept)
    3. https://www.vexio.ro/cauta/?search={quote_plus(q)}, https://www.vexio.ro/search/?q={quote_plus(q)}
  A 403/429/challenge stops immediately (BlockedError) - libergent got Cloudflare 403s from Node fetch.
  Page N > 1: only the pager's next link.
Cards (Pretzi 2026-03): article.product-box   (same class as ITGalaxy, different inner markup)
    a.text-center[href] (listing anchor, often the image link) / heading link; name from text, a[title], img alt
    .price.clearfix .pull-left.discounted   or   .price.clearfix .pull-left > strong      current price
    old price: .price.clearfix del / .old_price (unverified)
    stock: .instock / .availability.instock, text "in stoc" / "stoc epuizat" / "indisponibil"
  URL https://www.vexio.ro/<cat>/<brand>/<id>-<slug>/ -> id.
Product page: #price-value / .price-box .h3.price-value span (never .pvalue), old .price-box .old_price del,
  JSON "price": <plain decimal> in scripts.
"""
from __future__ import annotations

import re
from typing import ClassVar, Optional
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..extract import make_soup
from ..models import Offer
from ..net import BlockedError, FetchError
from .base import Store
from ._itshops import (
    ShopMixin, clean_url, current_price, dedupe, enrich_pdp, image_of, landed_on_home, longest, next_page_url,
    norm, num_attr, old_price_in, outermost, pdp_offer, pdp_title, pick_price, raise_if_challenge, stock_from,
    stock_of_el, text_of,
)

_ID_RE = re.compile(r"^/[^/]+/[^/]+/(\d+)-[^/]*/?$")
_QUERY_INPUT = re.compile(r"^(?:q|search|keyword|keywords|term|s|query|cauta|text|searchword)$", re.I)
_NO_RESULTS_RE = re.compile(
    r"nu\s+(?:am|au)\s+(?:fost\s+)?g[aă]sit|niciun\s+(?:produs|rezultat)|nu\s+exist[aă]\s+(?:produse|rezultate)|"
    r"0\s+(?:produse|rezultate)\s+(?:g[aă]sit|pentru)",
    re.I,
)
_JSON_PRICE_RE = re.compile(r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?')


def _url_id(url: str) -> Optional[str]:
    m = _ID_RE.search(urlsplit(url).path)
    return m.group(1) if m else None


class VexioStore(ShopMixin, Store):
    key = "vexio"
    name = "Vexio"
    base_url = "https://www.vexio.ro"
    domains = ("vexio.ro",)
    categories = ("electronics", "pc", "appliances", "home", "diy", "general")
    page_size = 24
    min_delay = 4.0          # 403/429/challenge observed on direct requests
    extra_headers = {"Referer": "https://www.vexio.ro/", "Upgrade-Insecure-Requests": "1"}

    primary_route = "/cauta/?q={q}"
    other_routes = ("/cauta/?search={q}", "/search/?q={q}")
    _route: ClassVar[Optional[str]] = None        # template that worked in this process
    _probed: ClassVar[bool] = False               # discovery + alternative routes already tried once

    def search_url(self, query: str) -> str:
        return self._fill(self.base_url + self.primary_route, query)

    def _fill(self, template: str, query: str) -> str:
        return template.replace("{q}", self.q_plus(query))

    # ------------------------------------------------------------------ search
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
                self._pager_set(page + 1, next_page_url(soup, final, page))
            return out
        self._pager_reset()
        tried: list[str] = []
        first: Optional[tuple[str, BeautifulSoup, str]] = None
        missing: Optional[FetchError] = None
        went_home = False
        templates: list[Optional[str]] = []
        if type(self)._route:
            templates.append(type(self)._route)
        templates.append(self.base_url + self.primary_route)
        if not type(self)._probed:   # once per process: home-page form (None) + the other guesses
            templates += [None] + [self.base_url + r for r in self.other_routes]
        for tmpl in templates:
            if tmpl is None:
                type(self)._probed = True
                tmpl = self._discover_route()
                if not tmpl:
                    continue
            if tmpl in tried:
                continue
            tried.append(tmpl)
            req_url = self._fill(tmpl, query)
            try:
                html, soup, final = self.get_html(req_url)
            except BlockedError:
                raise
            except FetchError as e:
                if e.status in (400, 404, 405, 410):
                    missing = e
                    continue
                raise
            if landed_on_home(req_url, final):
                # unknown route redirected to the home page: its promotion cards are not results, and the
                # route must neither be remembered nor feed the generic fallback
                raise_if_challenge(html, soup, final)
                went_home = True
                continue
            out = self._parse(soup, final)
            if out:
                type(self)._route = tmpl
                self.last_strategy = "cards"
                self._pager_set(2, next_page_url(soup, final, 1))
                return out
            raise_if_challenge(html, soup, final)
            if self._is_search_page(soup, query):
                type(self)._route = tmpl
                if _NO_RESULTS_RE.search(text_of(soup.body or soup)):
                    self.last_strategy = "no-results"
                    return []
                return self.fallback(html, final, soup)
            if first is None:
                first = (html, soup, final)
        if first is None:
            if went_home:
                self.last_strategy = "redirect:home"
                return []
            raise missing or FetchError("no search route answered", None, self.search_url(query))
        html, soup, final = first
        return self.fallback(html, final, soup)

    def _discover_route(self) -> Optional[str]:
        """Build a search URL template from the home page's GET search form."""
        try:
            _html, soup, final = self.get_html(self.base_url + "/")
        except BlockedError:
            raise
        except FetchError:
            return None
        for form in soup.find_all("form"):
            if (form.get("method") or "get").strip().lower() != "get":
                continue
            inp = form.select_one("input[type='search'][name]")
            if inp is None:
                inp = next((i for i in form.find_all("input", attrs={"name": True})
                            if _QUERY_INPUT.match(i["name"])
                            and (i.get("type") or "text").lower() in ("text", "search")), None)
            if inp is None:
                continue
            action = urljoin(final, form.get("action") or "/")
            if urlsplit(action).netloc.lower().removeprefix("www.") != "vexio.ro":
                continue
            hidden = [(h["name"], h.get("value") or "") for h in form.select("input[type='hidden'][name]")]
            extra = urlencode(hidden)
            sep = "&" if "?" in action else "?"
            return f"{action}{sep}{extra + '&' if extra else ''}{inp['name']}={{q}}"
        return None

    @staticmethod
    def _is_search_page(soup: BeautifulSoup, query: str) -> bool:
        q = norm(query).lower()
        if not q:
            return False
        for inp in soup.find_all("input", attrs={"name": _QUERY_INPUT}):
            if norm(inp.get("value")).lower() == q:
                return True
        heads = [text_of(soup.title)] + [text_of(h) for h in soup.find_all(["h1", "h2"], limit=3)]
        return any(q in h.lower() and re.search(r"cauta|c[aă]utare|rezultat|search", h, re.I) for h in heads)

    # ------------------------------------------------------------------ parsing
    def _parse(self, soup: BeautifulSoup, base: str) -> list[Offer]:
        cards = outermost(soup.select("article.product-box"))
        return dedupe(self._card(c, base) for c in cards)

    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        head = c.select_one("h2 a[href], h3 a[href], h4 a[href], .name a[href], .title a[href], .product-title a[href]")
        anchor = c.select_one("a.text-center[href]")
        link = head or anchor
        if link is None:
            link = next((a for a in c.select("a[href]") if _url_id(clean_url(a.get("href"), base))), None)
        if link is None:
            return None
        url = clean_url(link.get("href"), base)
        img = c.select_one("img[alt]")
        title = longest(text_of(head), head.get("title") if head is not None else "",
                        anchor.get("title") if anchor is not None else "", text_of(anchor))
        if not title and img is not None:
            title = norm(img.get("alt"))
        if not url or not title:
            return None
        box = c.select_one(".price.clearfix") or c.select_one(".price") or c
        price_el, price = pick_price(box, ".pull-left.discounted", ".pull-left > strong", ".discounted", "strong")
        if not price and box is not c:
            price_el, price = box, current_price(box)
        if not price:
            return None
        old = old_price_in(c, price, exclude=[price_el])
        stock = stock_of_el(c.select_one(".availability, .instock, .outofstock, [class*='stoc']"))
        if stock is None:
            stock = stock_from((), text_of(c))
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=_url_id(url), image=image_of(c, base), source="vexio",
        )

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        o = super().product(url)
        raw = self.last_raw or ""
        soup = make_soup(raw) if raw else None
        old_sels = (".price-box .old_price del", ".price-box del")
        if o is not None:
            o.offer_id = _url_id(url) or o.offer_id
            return enrich_pdp(o, soup, stock_sels=(".availability", ".instock", ".outofstock"), old_sels=old_sels)
        if soup is None:
            return None
        raise_if_challenge(raw, soup, url)
        for el in soup.select(".pvalue"):
            el.extract()  # a different value, not the selling price
        o = pdp_offer(self, soup, url, price_sels=("#price-value", ".price-box .h3.price-value span",
                                                   ".price-box .price-value"),
                      old_sels=old_sels, stock_sels=(".availability", ".instock", ".outofstock"),
                      offer_id=_url_id(url), source="vexio-pdp")
        if o is None:
            m = _JSON_PRICE_RE.search(raw)
            price = num_attr(m.group(1)) if m else None
            title = pdp_title(soup)
            if price and title:
                o = Offer(store=self.key, title=title, url=url, price=price, offer_id=_url_id(url),
                          store_name=self.name, source="vexio-pdp-json")
        return o
