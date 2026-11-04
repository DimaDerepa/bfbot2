"""IKEA Romania (ikea.com/ro/ro) - furniture / home, public SIK search JSON (no auth).

Search: GET https://sik.search.blue.cdtapps.com/ro/ro/search-result-page
        ?q={q}&size=24&types=PRODUCT&c=sr&v=20210322&autocorrect=true&subcategories-style=tree-navigation
        (requests params, spaces '+'; c=sr = search-results client, v = API version - from vrslev/ikea-api-client).
        Headers like ikea_api: Origin https://www.ikea.com, Referer https://www.ikea.com/, Accept-Language ro.
        GET /ro/ro/search on www.ikea.com is a 404 - do not use it.
Response: searchResultPage.products.main.{max, items[]}; items[].product may be null (content tiles).
  product: itemNo, itemNoGlobal, name ('BILLY'), typeName ('Biblioteca'), validDesignText ('alb'),
  itemMeasureReferenceText ('80x28x202 cm'), salesPrice.{numeral, currencyCode, current, previous},
  pipUrl (absolute), mainImageUrl.  previous = old price, only on "new lower price" items.
  No stock in this payload (in_stock stays None).
Under heavy rates SIK reportedly returns empty results before any 429 -> min_delay 4 s.
Product page (ikea.com/ro/... only; other countries are priced in other currencies -> None): SIK lookup by the
item number in the URL (.../{slug}-{itemNo}/, ...-s{itemNo}/ or /p/{itemNo}/), then JSON-LD / .pip-price__integer.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlsplit

from ..extract import make_soup
from ..models import Offer
from ..prices import parse_price
from ._ro_retail import clean_url, norm_text
from .base import Store

SIK_URL = "https://sik.search.blue.cdtapps.com/ro/ro/search-result-page"
# .../p/billy-biblioteca-alb-00263850/, .../p/...-s59182201/ (SPR combination), .../p/00263850/ (short form)
_ITEMNO_RE = re.compile(r"[-/]s?(\d{8})/?$")


def _dig(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _price_obj(v: Any) -> Optional[float]:
    """IKEA price object: {'numeral': 1299.0} or {'wholeNumber': '1.299', 'decimals': '90'} or a number."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, str)):
        return parse_price(v)
    if not isinstance(v, dict):
        return None
    p = parse_price(v.get("numeral")) if v.get("numeral") not in (None, "") else None
    if p:
        return p
    whole = re.sub(r"\D", "", str(v.get("wholeNumber") or ""))
    if not whole:
        cur = v.get("current")
        return _price_obj(cur) if isinstance(cur, dict) else None
    dec = re.sub(r"\D", "", str(v.get("decimals") or ""))
    try:
        val = float(f"{int(whole)}.{dec}") if dec else float(int(whole))
    except ValueError:
        return None
    return val if val > 0 else None


class IkeaStore(Store):
    key = "ikea"
    name = "IKEA"
    base_url = "https://www.ikea.com/ro/ro"
    domains = ("ikea.com",)
    categories = ("home", "appliances")
    page_size = 24
    min_delay = 4.0

    _headers = {
        "Accept": "application/json",
        "Accept-Language": "ro",
        "Origin": "https://www.ikea.com",
        "Referer": "https://www.ikea.com/",
    }

    def _sik(self, query: str, size: int) -> list[dict]:
        params = {
            "q": query.strip(), "size": str(size), "types": "PRODUCT", "c": "sr", "v": "20210322",
            "autocorrect": "true", "subcategories-style": "tree-navigation",
        }
        data = self.get_json(SIK_URL, params=params, headers=self._headers)
        items = _dig(data, "searchResultPage", "products", "main", "items")
        return [it["product"] for it in items or [] if isinstance(it, dict) and isinstance(it.get("product"), dict)]

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            return []      # one request; size grows with max_pages instead of paging
        size = min(100, self.page_size * self.max_pages)
        out: list[Offer] = []
        seen: set[str] = set()
        for p in self._sik(query, size):
            o = self._offer(p)
            if o is not None and o.key not in seen:
                seen.add(o.key)
                out.append(o)
        self.last_strategy = "sik-search"
        return out

    def _offer(self, p: dict) -> Optional[Offer]:
        sp = p.get("salesPrice") if isinstance(p.get("salesPrice"), dict) else {}
        cur = str(sp.get("currencyCode") or "RON").upper()
        if cur not in ("RON", "LEI"):
            return None
        price = _price_obj(sp.get("numeral")) or _price_obj(sp.get("current"))
        parts = [str(p.get(k) or "").strip() for k in ("name", "typeName")]
        head = " ".join(x for x in parts if x)
        tail = [str(p.get(k) or "").strip() for k in ("validDesignText", "itemMeasureReferenceText")]
        title = ", ".join([head, *[x for x in tail if x]]) if head else ""
        url = p.get("pipUrl")
        if not price or not title or not isinstance(url, str) or not url:
            return None
        old = None
        for k in ("previous", "previousPrice", "wasPrice"):
            if sp.get(k) is not None:
                old = _price_obj(sp.get(k))
                if old:
                    break
        item_no = p.get("itemNo") or p.get("itemNoGlobal") or p.get("id")
        img = p.get("mainImageUrl")
        return Offer(
            store=self.key, title=" ".join(title.split()), url=clean_url(url, "https://www.ikea.com/"),
            price=price, old_price=old if old and old > price else None, in_stock=None,
            offer_id=str(item_no) if item_no else None, image=img if isinstance(img, str) else None,
            source="ikea-sik",
        )

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        clean = clean_url(url, "https://www.ikea.com/")
        if not urlsplit(clean).path.startswith("/ro/"):
            # ikea.com/de/... etc.: other currency; the RO SIK index or a meta/PIP price would be read as lei
            self.last_strategy = "not-ro"
            return None
        m = _ITEMNO_RE.search(clean)
        if m:
            item_no = m.group(1)
            try:
                products = self._sik(item_no, 5)
            except Exception:
                products = []
            for p in products:
                if item_no in (str(p.get("itemNo") or ""), str(p.get("itemNoGlobal") or "")):
                    o = self._offer(p)
                    if o is not None:
                        o.url = url
                        o.store_name = self.name
                        return o
        o = super().product(url)
        if o is not None or not self.last_raw:
            return o
        # PIP HTML: price split into integer / decimals spans
        soup = make_soup(self.last_raw)
        for prefix in ("pipcom-price", "pip-price", "pipf-price", "pip-temp-price"):
            integer = soup.select_one(f".{prefix}__integer")
            if integer is None:
                continue
            whole = re.sub(r"\D", "", norm_text(integer))
            dec_el = soup.select_one(f".{prefix}__decimal")
            dec = re.sub(r"\D", "", norm_text(dec_el)) if dec_el is not None else ""
            if not whole:
                continue
            price = float(f"{int(whole)}.{dec}") if dec else float(int(whole))
            h1 = soup.find("h1")
            title = norm_text(h1)
            if price > 0 and title:
                return Offer(store=self.key, title=title, url=url, price=price,
                             offer_id=m.group(1) if m else None, store_name=self.name, source="ikea-pip")
        return None
