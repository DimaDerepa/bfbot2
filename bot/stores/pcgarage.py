"""PC Garage (pcgarage.ro) - custom PHP shop, server-rendered listings, Cloudflare Bot Management.

Search: GET https://www.pcgarage.ro/cauta/?q={quote_plus(q)}   e.g. /cauta/?q=rtx+4070
        (live fetch 2026-09-25 returned <title>Cautare: rtx 4070 - PC Garage</title> with product_box cards;
        the path form /cauta/<q>/ is unverified and answered 403 to libergent, so it is not used).
        Page N > 1: only the pager's next link (ul.pagination / .lr-pagination; categories use /pagina{N}/).
Cards (bf-price-monitor 2026-09, GoogeMV 2026-08, Pretzi 2026-03):
  div.product_box
    .product_box_name a[href]          absolute URL; name = text (or a[title])
    .product_box_image > a[href] img
    [data-pid] / a[href*="adaugaincos-<id>"]          product id
    .product_box_price_container .pb-price p.price    "1.798,99 RON", sometimes "de la ..."; no <sup>
    .product_box_availability.instock | .insupplierstock | .outofstock
        ("Stoc magazin suficient/limitat", "In stoc furnizor", "Stoc epuizat")
  Old/list price on listings is undocumented: struck/"old"/"prp" elements or a later, higher price in the
  price container are used (unverified - diagnostics will tell).
Product page: meta[itemprop=price] (plain decimal) -> fallback .ps_sell_price .price_num; resealed offers
  (#pe_unsealed) are ignored.
Anti-bot: Cloudflare. Normal pages embed /cdn-cgi/challenge-platform/scripts/jsd/main.js (passive, NOT a block);
  a managed challenge (_cf_chl_opt / .../h/<x>/orchestrate/ / "Just a moment") -> BlockedError.
  GitHub-hosted runner IPs are reported blocked; BOT_BROWSER=pcgarage enables the Playwright fallback.
"""
from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from ..extract import make_soup
from ..models import Offer
from .base import Store
from ._itshops import (
    PagerMixin, clean_url, current_price, dedupe, image_of, longest, next_page_url, old_price_in, other_higher_price,
    outermost, pdp_offer, raise_if_challenge, stock_of_el,
)

_CART_ID_RE = re.compile(r"adaugaincos-(\d+)")


class PcGarageStore(PagerMixin, Store):
    key = "pcgarage"
    name = "PC Garage"
    base_url = "https://www.pcgarage.ro"
    domains = ("pcgarage.ro",)
    categories = ("pc", "electronics", "appliances")
    page_size = 24
    min_delay = 5.0          # 403/429 bursts reported; 2026 scrapers keep a 5 s per-domain floor
    extra_headers = {"Referer": "https://www.pcgarage.ro/", "Upgrade-Insecure-Requests": "1"}

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
        cards = outermost(soup.select("div.product_box"))
        out = dedupe(self._card(c, final) for c in cards)
        if out:
            self.last_strategy = "cards"
            self._pager_set(page + 1, next_page_url(soup, final, page))
            return out
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ parsing
    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        a = (c.select_one(".product_box_name a[href]") or c.select_one(".product_box_image a[href]")
             or c.select_one("h2 a[href]"))
        if a is None:
            return None
        url = clean_url(a.get("href"), base)
        img = c.select_one(".product_box_image img")
        title = longest(a.get_text(" ", strip=True), a.get("title")) or (img.get("alt", "") if img else "")
        if not url or not title:
            return None
        container = c.select_one(".product_box_price_container") or c
        price_el = (container.select_one(".pb-price p.price") or container.select_one("p.price")
                    or container.select_one(".price") or container.select_one(".pb-price"))
        price = current_price(price_el)
        if not price:
            return None
        old = old_price_in(container, price, exclude=[price_el]) or other_higher_price(container, price_el, price)
        pid = None
        el = c if c.get("data-pid") else c.select_one("[data-pid]")
        if el is not None and str(el.get("data-pid") or "").strip().isdigit():
            pid = str(el["data-pid"]).strip()
        if not pid:
            for cart in c.select("a[href*='adaugaincos-'], form[action*='adaugaincos-']"):
                m = _CART_ID_RE.search(cart.get("href") or cart.get("action") or "")
                if m:
                    pid = m.group(1)
                    break
        return Offer(
            store=self.key, title=title, url=url, price=price,
            old_price=old if old and old > price else None,
            in_stock=stock_of_el(c.select_one(".product_box_availability")),
            offer_id=pid, image=image_of(c, base, ".product_box_image img"), source="pcgarage",
        )

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        o = super().product(url)
        if o is not None or not self.last_raw:
            return o
        soup: BeautifulSoup = make_soup(self.last_raw)
        raise_if_challenge(self.last_raw, soup, url)
        for el in soup.select("#pe_unsealed"):
            el.decompose()  # resealed (desigilat) offers have their own, lower price
        return pdp_offer(self, soup, url, price_sels=('meta[itemprop="price"]', ".ps_sell_price .price_num",
                                                       ".ps_sell_price"),
                         old_sels=(".ps_old_price", ".ps_prp_price", ".old_price", "del"),
                         stock_sels=(".product_availability", ".ps_availability", "[class*='availability']"),
                         title_sels=("h1#product_name", "h1"), source="pcgarage-pdp")

