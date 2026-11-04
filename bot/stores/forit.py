"""ForIT (forit.ro) - small IT shop (PMC Group Distributie SRL).

Search: GET https://www.forit.ro/cauta/?q={quote_plus(q)}   e.g. /cauta/?q=ssd+samsung+1tb
        (official search route; libergent's direct check 2026-09-28 accepted 15 and 9 SSD offers from it.
        Customer reviews say the site search is weak - category URLs such as /placi-video/ can be
        watched as product-page URLs instead). Page N > 1: only the pager's next link.
Listing data (libergent 2026-09 parser): every product card carries buttons with
    data-product='{"id":12,"title":"SSD Samsung 1TB","price":499.9,"img":"https://...webp","href":"/ssd-samsung-..."}'
    (HTML-entity encoded JSON, plain decimal price, relative href; the same product appears on 2+ buttons).
  The card around those buttons (largest ancestor holding only this product and no page-level markup such
  as h1/main/aside/filters/pager) is used for struck/old prices and stock text ("in stoc furnizor",
  "la comanda", "stoc epuizat"); instalment prices ("de la 86 lei/luna") are ignored.
  A redirect to the bare home page is never parsed.
  No data-product -> generic extraction (JSON-LD, microdata, embedded JSON, heuristic cards).
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ..models import Offer
from .base import Store
from ._itshops import (
    ShopMixin, clean_url, dedupe, image_of, landed_on_home, longest, next_page_url, norm, num_attr, old_price_in,
    raise_if_challenge, stock_from, stock_of_el, text_of,
)

_OLD_KEYS = ("old_price", "oldPrice", "price_old", "priceOld", "full_price", "fullPrice", "initial_price",
             "initialPrice", "regular_price", "regularPrice", "list_price", "listPrice", "pret_vechi")
_STOCK_KEYS = ("stock", "in_stock", "inStock", "availability", "stoc", "stock_status")
# markup that never sits inside one product card: the card search stops below it
_PAGE_LEVEL = ("h1, main, aside, nav, header, footer, input[type='search'], input[name='q'], .pagination, "
               "[role='navigation'], [class*='filter'], [id*='filter']")


def _json_attr(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _stock_value(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    if isinstance(v, str):
        if v.strip().isdigit():
            return int(v) > 0
        return stock_from((), v)
    return None


class ForitStore(ShopMixin, Store):
    key = "forit"
    name = "ForIT"
    base_url = "https://www.forit.ro"
    domains = ("forit.ro",)
    categories = ("pc", "electronics")
    page_size = 24
    min_delay = 3.0          # small shop: keep request rates low
    extra_headers = {"Referer": "https://www.forit.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/cauta/?q={self.q_plus(query)}"

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
            self.last_strategy = "redirect:home"     # home-page promotions are not search results
            return []
        out = self._parse(soup, final)
        if out:
            self.last_strategy = "data-product"
            self._pager_set(page + 1, next_page_url(soup, final, page))
            return out
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ parsing
    def _parse(self, soup: BeautifulSoup, base: str) -> list[Offer]:
        items: list[tuple[Tag, dict]] = []
        for el in soup.select("[data-product]"):
            data = _json_attr(el.get("data-product"))
            if data:
                items.append((el, data))
        hrefs = {id(el): str(d.get("href") or d.get("url") or "") for el, d in items}
        out = [self._offer(d, el, base, hrefs) for el, d in items]
        return dedupe(o for o in out if o is not None)

    @staticmethod
    def _card_of(el: Tag, hrefs: dict[int, str]) -> Optional[Tag]:
        """Largest ancestor (max 6 levels) that still holds only this product's data-product elements and no
        page-level markup. Without the page-level stop a single-result page would climb to <main> and read the
        sidebar's struck prices / "Stoc epuizat" filter labels as this product's old price and stock."""
        card = None
        node = el
        for _ in range(6):
            node = node.parent
            if not isinstance(node, Tag) or node.name in ("body", "html", "main", "aside", "nav", "header", "footer"):
                break
            if node.select_one(_PAGE_LEVEL) is not None:
                break
            inside = {hrefs.get(id(o), "") for o in node.select("[data-product]")} - {""}
            if len(inside) > 1:
                break
            card = node
        return card

    def _offer(self, d: dict, el: Tag, base: str, hrefs: dict[int, str]) -> Optional[Offer]:
        title = norm(d.get("title") or d.get("name"))
        price = num_attr(d.get("price"))
        href = str(d.get("href") or d.get("url") or "")
        url = clean_url(href, base) if href else ""
        host = urlsplit(url).netloc.removeprefix("www.") if url else ""
        if not title or not price or host != "forit.ro":
            return None
        card = self._card_of(el, hrefs)
        if card is not None:
            link = next((a for a in card.select("a[href]") if clean_url(a.get("href"), base) == url), None)
            if link is not None:
                title = longest(title, link.get("title"), text_of(link)) if len(title) < 12 else title
        old = None
        for k in _OLD_KEYS:
            v = num_attr(d.get(k))
            if v and v > price:
                old = v
                break
        if old is None and card is not None:
            old = old_price_in(card, price)
        stock: Optional[bool] = None
        for k in _STOCK_KEYS:
            if k in d:
                stock = _stock_value(d.get(k))
                if stock is not None:
                    break
        if stock is None and card is not None:
            stock = stock_of_el(card.select_one("[class*='stoc'], [class*='stock'], [class*='availability']"))
            if stock is None:
                stock = stock_from((), text_of(card))
        pid = d.get("id") or d.get("product_id") or d.get("sku")
        img = d.get("img") or d.get("image")
        if not (isinstance(img, str) and img.startswith(("http", "/"))):
            img = image_of(card, base) if card is not None else None
        elif img.startswith("/"):
            img = self.abs(img)
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=str(pid) if pid not in (None, "") else None, image=img, source="forit",
        )
