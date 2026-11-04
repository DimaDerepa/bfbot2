"""Auchan Romania (auchan.ro) - hypermarket on VTEX IO.

Search (primary, VTEX Intelligent Search, no auth):
  GET https://www.auchan.ro/api/io/_v/api/intelligent-search/product_search/?query={q}&count=50&page={N}&locale=ro-RO
  (requests params -> spaces '+'; page is 1-based). Response: products[] with productId, productName, brand,
  link ('/slug/p'), linkText, items[].{itemId, ean, measurementUnit, unitMultiplier, images[].imageUrl,
  sellers[].{sellerName, sellerDefault, commertialOffer.{Price, ListPrice, PriceWithoutDiscount,
  AvailableQuantity, IsAvailable}}}; recordsFiltered = total.
  Weight-sold goods (measurementUnit kg/l): Price is per kg/l, package price = Price * unitMultiplier.
Fallback: VTEX legacy /api/catalog_system/pub/products/search?ft=&_from=&_to= when IS errors out or answers an
  unexpected shape; it returns the same product objects, parsed with the same rules (kg/l multiplier, seller, image).
Product page: /api/catalog_system/pub/products/search/{linkText}/p, then JSON-LD.
Verified against alexandruiftimi-ns/StorePriceTracker (stores/auchan.py, Sep 2026). No blocking reported.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from ..net import BlockedError, FetchError
from ..models import Offer
from ..prices import parse_price
from ._ro_retail import clean_url
from .platforms import VtexStore

log = logging.getLogger("bot.stores")

_IS_PATH = "/api/io/_v/api/intelligent-search/product_search/"
_PDP_RE = re.compile(r"^/([^/]+)/p/?$")


class AuchanStore(VtexStore):
    key = "auchan"
    name = "Auchan"
    base_url = "https://www.auchan.ro"
    domains = ("auchan.ro",)
    categories = ("general", "home", "appliances", "electronics", "toys")
    page_size = 50

    _headers = {"Accept": "application/json, */*", "Referer": "https://www.auchan.ro/"}

    def search(self, query: str) -> list[Offer]:
        self._last_page = False
        out: list[Offer] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            offers = self.search_page(query, page)
            new = [o for o in offers if o.key not in seen]
            for o in new:
                seen.add(o.key)
                o.store_name = self.name
            out.extend(new)
            if self._last_page or not new:
                break
        return out

    def search_page(self, query: str, page: int) -> list[Offer]:
        params = {"query": query.strip(), "count": str(self.page_size), "page": str(page), "locale": "ro-RO"}
        try:
            data = self.get_json(self.base_url + _IS_PATH, params=params, headers=self._headers)
        except BlockedError:
            raise
        except FetchError as e:
            log.info("auchan intelligent-search failed (%s), using legacy catalog search", e)
            data = None
        products = data.get("products") if isinstance(data, dict) else None
        if not isinstance(products, list):
            # legacy catalog_system search: same product objects (a JSON array), parsed with the same rules
            # (kg/l unit multiplier, seller, image, clean URL) as Intelligent Search
            frm = (page - 1) * self.page_size
            legacy = self.get_json(f"{self.base_url}/api/catalog_system/pub/products/search", headers=self._headers,
                                   params={"ft": query.strip(), "_from": str(frm), "_to": str(frm + self.page_size - 1)})
            products = legacy if isinstance(legacy, list) else []
            self.last_strategy = "vtex-legacy"
            self._last_page = len(products) < self.page_size
            return self._offers(products)
        self.last_strategy = "vtex-intelligent-search"
        total = data.get("recordsFiltered")
        self._last_page = len(products) < self.page_size or (
            isinstance(total, int) and page * self.page_size >= total)
        return self._offers(products)

    def _offers(self, products: list) -> list[Offer]:
        out: list[Offer] = []
        seen: set[str] = set()
        for p in products:
            o = self._offer(p) if isinstance(p, dict) else None
            if o is not None and o.key not in seen:
                seen.add(o.key)
                out.append(o)
        return out

    # ------------------------------------------------------------------ parsing
    def _offer(self, p: dict) -> Optional[Offer]:
        name = " ".join(str(p.get("productName") or p.get("productTitle") or "").split())
        if not name:
            return None
        best: Optional[tuple[tuple, dict, dict, float]] = None
        for item in p.get("items") or []:
            if not isinstance(item, dict):
                continue
            for s in item.get("sellers") or []:
                if not isinstance(s, dict):
                    continue
                co = s.get("commertialOffer")
                if not isinstance(co, dict):
                    continue
                price = parse_price(co.get("Price"))
                if not price:
                    continue
                avail = _available(co)
                rank = (avail is True, s.get("sellerDefault") is True, -price)
                if best is None or rank > best[0]:
                    best = (rank, item, s, price)
        if best is None:
            return None
        _, item, seller, price = best
        co = seller.get("commertialOffer") or {}
        old = None
        for k in ("ListPrice", "PriceWithoutDiscount"):
            v = parse_price(co.get(k))
            if v and v > price + 0.009:
                old = v
                break
        unit = str(item.get("measurementUnit") or "").lower()
        mult = item.get("unitMultiplier")
        if unit in ("kg", "l") and isinstance(mult, (int, float)) and mult > 0 and mult != 1:
            price = round(price * mult, 2)       # Price is per kg / l: package price
            old = round(old * mult, 2) if old else None
        link = p.get("link") or ""
        if isinstance(link, str) and link.startswith("http"):
            url = clean_url(link, self.base_url)
        elif isinstance(link, str) and link.startswith("/"):
            url = clean_url(self.base_url + link, self.base_url)
        elif p.get("linkText"):
            url = f"{self.base_url}/{quote(str(p['linkText']).strip('/'), safe='-_.')}/p"
        else:
            return None
        images = item.get("images") or []
        image = images[0].get("imageUrl") if images and isinstance(images[0], dict) else None
        if not isinstance(image, str) or not image.startswith("http"):
            image = None
        sname = seller.get("sellerName")
        if not isinstance(sname, str) or not sname.strip() or "auchan" in sname.lower():
            sname = None
        pid = p.get("productId") or item.get("itemId")
        return Offer(
            store=self.key, title=name, url=url, price=price,
            old_price=old if old and old > price else None, in_stock=_available(co),
            offer_id=str(pid) if pid not in (None, "") else None, image=image,
            seller=sname.strip() if sname else None, source="vtex-is",
        )

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        m = _PDP_RE.match(urlsplit(url).path)
        if m:
            api = f"{self.base_url}/api/catalog_system/pub/products/search/{quote(m.group(1), safe='-_.')}/p"
            try:
                data = self.get_json(api, headers=self._headers)
            except BlockedError:
                raise
            except FetchError:
                data = None
            if isinstance(data, list) and data and isinstance(data[0], dict):
                o = self._offer(data[0])
                if o is not None:
                    o.url = url
                    o.store_name = self.name
                    o.source = "vtex-pdp"
                    return o
        return super().product(url)


def _available(co: dict) -> Optional[bool]:
    q: Any = co.get("AvailableQuantity")
    if isinstance(q, (int, float)) and not isinstance(q, bool):
        return q > 0
    if isinstance(co.get("IsAvailable"), bool):
        return co["IsAvailable"]
    return None
