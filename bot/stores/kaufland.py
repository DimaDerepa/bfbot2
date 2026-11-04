"""Kaufland Romania (kaufland.ro) - no e-shop / marketplace in RO; only the weekly offers (flyer) page.

Source: GET https://www.kaufland.ro/oferte/prezentare-generala-oferte.html?kloffer-week=current
        (server-rendered, fetched once per run and filtered locally by the query words).
Tiles (verified in alexandruiftimi-ns/StorePriceTracker stores/kaufland.py, Sep 2026):
  .k-product-tile: name = .k-product-tile__title + ' ' + .k-product-tile__subtitle,
  .k-price-tag: current .k-price-tag__price ("9,99", no currency), old .k-price-tag__old-price,
  discount .k-price-tag__discount; .k-price-tag--xtra = Kaufland Card-only price (-> seller "Kaufland Card");
  package .k-product-tile__unit-price, base price .k-product-tile__base-price. Dedupe on (name, price).
  A tile with a regular and a Kaufland Card price tag gives one offer per tag.
Offers have no product page: the URL is the tile link (own or wrapping <a>, utm_* dropped), else the offers page
(offer_id keeps them apart). A failed / blocked flyer fetch is remembered: one request per run, error per query.
Shelf prices per store exist only in the government monitor (monitorulpreturilor.info, needs lat/lon) - not used.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import Tag

from ..models import Offer
from ..net import BlockedError
from ..prices import element_price_text
from ..text import has_term, norm, query_terms, slug
from ._ro_retail import image_of, norm_text, price_text, raise_if_challenge
from .base import Store

OFFERS_URL = "https://www.kaufland.ro/oferte/prezentare-generala-oferte.html?kloffer-week=current"
_ID_ATTRS = ("data-article-id", "data-articleid", "data-kloffer-id", "data-offer-id", "data-product-id", "data-id")
_TRACKING = ("utm_", "gclid", "fbclid", "cid", "ref")


def _tile_href(tile: Tag) -> Optional[str]:
    a = tile if tile.name == "a" and tile.get("href") else (tile.find("a", href=True)
                                                           or tile.find_parent("a", href=True))
    if a is None:
        return None
    href = (a.get("href") or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    p = urlsplit(urljoin(OFFERS_URL, href))
    if not p.netloc.endswith("kaufland.ro"):
        return None
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith(_TRACKING)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


def _price(tag: Optional[Tag]) -> Optional[float]:
    """'9,99' / '1.799,99' (no currency); '7<sup>99</sup>' -> 7.99."""
    if tag is None:
        return None
    txt = element_price_text(tag)
    if not re.search(r"\d", txt) or re.search(r"%|\d\s*\+\s*\d", txt):   # "-25%", "2+1 gratis" are not prices
        return None
    return price_text(txt)


class KauflandStore(Store):
    key = "kaufland"
    name = "Kaufland"
    base_url = "https://www.kaufland.ro"
    domains = ("kaufland.ro",)
    categories = ("general",)
    page_size = 1000      # the whole flyer is one "page"

    def __init__(self, http, *, max_pages: int = 1) -> None:
        super().__init__(http, max_pages=max_pages)
        self._offers: Optional[list[Offer]] = None
        self._error: Optional[Exception] = None

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            return []
        terms = query_terms(query)
        if not terms:
            return []
        return [o for o in self._flyer() if all(has_term(norm(o.title), t) for t in terms)]

    def _flyer(self) -> list[Offer]:
        if self._error is not None:      # one failed fetch per run is enough: same error for every query
            raise self._error
        if self._offers is None:
            try:
                html, soup, final = self.get_html(OFFERS_URL)
            except Exception as e:
                self._error = e
                raise
            offers = self.parse_tiles(soup, final)
            if offers:
                self.last_strategy = "kaufland-flyer"
            else:
                try:
                    raise_if_challenge(soup, html, final)     # anti-bot page served with HTTP 200
                except BlockedError as e:
                    self._error = e
                    raise
                offers = self.fallback(html, final, soup)
            self._offers = offers
        return self._offers

    def parse_tiles(self, soup, page_url: str) -> list[Offer]:
        out: list[Offer] = []
        seen: set[tuple[str, float]] = set()
        ids: dict[str, int] = {}
        for tile in soup.select(".k-product-tile"):
            title = norm_text(tile.select_one(".k-product-tile__title"))
            subtitle = norm_text(tile.select_one(".k-product-tile__subtitle"))
            name = f"{title} {subtitle}".strip()
            # a tile can carry a regular price tag and a Kaufland Card one (k-price-tag--xtra): one offer each
            tags = tile.select(".k-price-tag")
            if not name or not tags:
                continue
            package = norm_text(tile.select_one(".k-product-tile__unit-price"))
            href = _tile_href(tile)
            data_id = next((str(el.get(a)).strip() for el in (tile, tile.find("a")) if isinstance(el, Tag)
                            for a in _ID_ATTRS if el.get(a)), None)
            for tag in tags:
                price = _price(tag.select_one(".k-price-tag__price"))
                if not price:
                    continue
                if (name.lower(), price) in seen:
                    continue
                seen.add((name.lower(), price))
                old = _price(tag.select_one(".k-price-tag__old-price"))
                card_only = "k-price-tag--xtra" in (tag.get("class") or [])
                if data_id:
                    oid = f"{data_id}-card" if card_only else data_id
                else:
                    oid = slug(" ".join(x for x in (name, package, "card" if card_only else "") if x))
                n = ids.get(oid, 0) + 1
                ids[oid] = n
                if n > 1:
                    oid = f"{oid}-{n}"
                out.append(Offer(
                    store=self.key, title=name, url=href or OFFERS_URL, price=price,
                    old_price=old if old and old > price else None, in_stock=None,
                    offer_id=oid, image=image_of(tile, page_url),
                    seller="Kaufland Card" if card_only else None, source="kaufland-flyer",
                ))
        return out
