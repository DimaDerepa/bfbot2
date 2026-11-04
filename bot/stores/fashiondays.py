"""Fashion Days (fashiondays.ro) - fashion & beauty, server-rendered listings.

Search: GET https://www.fashiondays.ro/search/?q={quote(q, safe='')}   spaces -> %20 (/search/?q=adidas%20samba)
        (route validated 2026-09-28 by libergent: 90 cards for "adidas samba"; an older route answered 404).
        One page of ~36-90 cards; no documented pagination, so page > 1 is never requested.
Cards (libergent parser + test fixture, 2026-09): li.product-card, each running until the next card.
  The first <a data-gtm-price> of the card carries the GTM tracking data:
    data-gtm-price       CURRENT price, machine number in RON ("269.99")
    data-gtm-name        product name ("Pantofi Samba");   title attribute = fallback
    data-gtm-brand-name  brand ("adidas Originals")       -> title "adidas Originals Pantofi Samba"
    data-gtm-status      "limited_quantity" / "in_stock" / "sold_out" ... (sold out / unavailable -> in_stock False)
    href                 https://www.fashiondays.ro/p/{slug}-p{id}[-{n}]/?gtm_data=...  (query stripped; only /p/ links)
  img[data-original]     lazy-loaded image (fdcdn.akamaized.net)
  Old / RRP price is not in the GTM attributes; it is read from the visible price block (struck / "old" / "rrp"
  element, else the highest other "NNN Lei" amount of the card) - selector unverified, diagnostics will tell.
  Visible prices are Romanian ("1.299,99 Lei").
Product page: JSON-LD / meta (generic), offer id from the -p{id} URL suffix.
Anti-bot: none reported (static assets on Akamai CDN only); plain fetches answered in 300-750 ms.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import Tag

from ..models import Offer
from .base import Store
from ._fashion import (
    NextPager, clean_url, current_price, dedupe, higher_money_in, image_of, join_title, landed_on_home, norm, num,
    old_price_in, outermost, pick_price, raise_if_challenge, stock_from_text, text_of,
)

_PID_RE = re.compile(r"-p(\d{4,}(?:-\d{1,3})?)/?$")
_SOLD_OUT_RE = re.compile(r"sold.?out|unavailable|out.?of.?stock|epuizat|indisponibil", re.I)
_IN_STOCK_RE = re.compile(r"in.?stock|available|limited|last|ultim", re.I)


def fd_offer_id(url: str) -> Optional[str]:
    m = _PID_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


class FashionDaysStore(NextPager, Store):
    key = "fashiondays"
    name = "Fashion Days"
    base_url = "https://www.fashiondays.ro"
    domains = ("fashiondays.ro",)
    categories = ("fashion", "beauty")
    page_size = 90
    min_delay = 2.0
    extra_headers = {"Referer": "https://www.fashiondays.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/search/?q={self.q_path(query)}"

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            return []  # single result page
        url = self.search_url(query)
        html, soup, final = self.get_html(url)
        if landed_on_home(url, final):
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect:home"
            return []
        cards = outermost(soup.select("li.product-card"))
        out = dedupe(self._card(c) for c in cards)
        if out:
            self.last_strategy = "gtm-cards"
            return out
        if urlsplit(final).path.startswith("/p/"):
            o = self.product_from_html(html, final)
            if o is not None:
                self.last_strategy = "product-page"
                return [o]
        raise_if_challenge(html, soup, final)
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ parsing
    def _card(self, c: Tag) -> Optional[Offer]:
        a = c.select_one("a[data-gtm-price]")
        links = [a] if a is not None else []
        links += [x for x in c.select("a[href]") if x is not a]
        url = ""
        for x in links:
            u = clean_url(x.get("href"), self.base_url)
            if u and urlsplit(u).netloc.endswith("fashiondays.ro") and urlsplit(u).path.startswith("/p/"):
                url = u
                break
        if not url:
            return None  # campaign / category tile
        gtm = a if a is not None else c
        name = norm(gtm.get("data-gtm-name")) or norm(gtm.get("title"))
        brand = norm(gtm.get("data-gtm-brand-name"))
        if not name:
            name = text_of(c.select_one(".product-card-title, .product-card-name, [class*='title'], [class*='name']"))
        if not brand:
            brand = text_of(c.select_one(".product-card-brand, [class*='brand']"))
        if not name:
            img = c.select_one("img[alt]")
            name = norm(img.get("alt")) if img is not None else ""
        title = join_title(brand, name)
        if not title:
            return None
        price = num(gtm.get("data-gtm-price"))
        price_el = None
        if not price:
            price_el, price = pick_price(c, ".product-card-price .new-price", ".new-price", ".price-new",
                                         "[class*='new-price']", "[class*='price']")
        if not price:
            return None
        if price_el is None:
            price_el = c.select_one(".new-price, .price-new, [class*='new-price']")
        old = old_price_in(c, price, exclude=[price_el])
        if not old:
            box = c.select_one(".product-card-price, .product-card-prices, [class*='prices'], [class*='price-box']") or c
            old = higher_money_in(box, price)
        status = norm(gtm.get("data-gtm-status"))
        stock: Optional[bool] = None
        if status:
            if _SOLD_OUT_RE.search(status):
                stock = False
            elif _IN_STOCK_RE.search(status):
                stock = True
        if stock is None and stock_from_text(text_of(c)) is False:
            stock = False  # "Stoc epuizat" / "Sold out" badge; a positive text on a card proves nothing
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=fd_offer_id(url), image=image_of(c, self.base_url, "img"),
            source="fashiondays",
        )

    # ------------------------------------------------------------------ product page
    def product_from_html(self, html: str, url: str) -> Optional[Offer]:
        from ..extract import extract_product_page, make_soup

        o = extract_product_page(html, url, self.key)
        if o is None:
            soup = make_soup(html)
            el = soup.select_one("[data-gtm-price][data-gtm-name]")
            if el is not None and num(el.get("data-gtm-price")):
                o = Offer(store=self.key, title=join_title(el.get("data-gtm-brand-name"), el.get("data-gtm-name")),
                          url=url, price=num(el.get("data-gtm-price")), source="fashiondays-pdp")
            elif el is None:
                price = current_price(soup.select_one(".new-price, .product-price"))
                h1 = soup.find("h1")
                if price and h1 is not None:
                    o = Offer(store=self.key, title=text_of(h1), url=url, price=price, source="fashiondays-pdp")
        if o is not None:
            o.url = clean_url(url, self.base_url) or url
            o.offer_id = fd_offer_id(o.url) or o.offer_id
            o.store_name = self.name
        return o

    def product(self, url: str) -> Optional[Offer]:
        resp = self.http.get(url)
        self.last_raw = resp.text
        o = self.product_from_html(resp.text, url)
        if o is not None:
            o.url = url
        return o
