"""Decathlon Romania (decathlon.ro) - sport equipment & sportswear; Svelte SSR ("Vitamin" design system, vtmn-*).

Search: GET https://www.decathlon.ro/search?Ntt={quote_plus(q)}   e.g. ?Ntt=bicicleta+mtb  (Ntt = Decathlon's search
        parameter on FR/TR and other storefronts; libergent's /search?query= got 403). Page N > 1: only the pager's
        next link (never built from a template).
Cards (msakirc/kutai decathlon_tr.py, same platform assumed on RO - unverified):
  [data-supermodelid]                      the value is the model id -> offer_id (stable across colour swatches)
    a.dpb-product-model-link[href]         relative, /p/{slug}/_/R-p-{productId}?mc={modelCode} (mc kept: it selects
                                           the colour/model, other query parameters dropped)
    span.vh inside the link                full (visually hidden) product name
    brand: short [class*=brand] element (e.g. "ROCKRIDER") -> prefixed to the title when missing
    span.vtmn-price                        "1.299,99 lei" (whole-lei prices may have no decimals); a struck /
                                           strikethrough / "old" vtmn-price, else another higher vtmn-price, = old price
    img[src] / img[srcset]
Product page: JSON-LD Product / offers (generic extraction).
Anti-bot: hard - Cloudflare managed challenge on decathlon.nl (2026-07, incl. the home page), 403 to libergent's plain
  fetch on decathlon.ro (2026-09-28), DataDome reported for Decathlon -> BlockedError; >= 5 s between requests.
  Expect failures from GitHub Actions without a browser (BOT_BROWSER=decathlon) or a residential proxy.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import Tag

from ..models import Offer
from .base import Store
from ._fashion import (
    NextPager, clean_url, dedupe, image_of, is_old_el, join_title, landed_on_home, longest, next_page_url, norm,
    old_price_in, outermost, pick_price, raise_if_challenge, stock_from_text, text_of, current_price,
)

_RP_RE = re.compile(r"/R-p-([A-Za-z0-9-]+?)(?:/|$)")


def decathlon_pid(url: Optional[str]) -> Optional[str]:
    m = _RP_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


class DecathlonStore(NextPager, Store):
    key = "decathlon"
    name = "Decathlon"
    base_url = "https://www.decathlon.ro"
    domains = ("decathlon.ro",)
    categories = ("sport", "fashion")
    page_size = 40
    min_delay = 5.0
    extra_headers = {"Referer": "https://www.decathlon.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/search?Ntt={self.q_plus(query)}"

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
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect:home"
            return []
        items = outermost(soup.select("[data-supermodelid]"))
        out = dedupe(self._item(it, final) for it in items)
        if out:
            self.last_strategy = "vtmn-cards"
            self._pager_set(page + 1, next_page_url(soup, final))
            return out
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    def _item(self, it: Tag, base: str) -> Optional[Offer]:
        a = it.select_one("a.dpb-product-model-link[href]") or it.select_one("a[href*='/p/']")
        if a is None:
            return None
        url = clean_url(a.get("href"), base, keep=("mc",))
        if not url:
            return None
        vh = a.select_one("span.vh") or it.select_one("span.vh")
        name = text_of(vh) or longest(a.get("title"), a.get_text(" ", strip=True))
        if not name:
            img = it.select_one("img[alt]")
            name = norm(img.get("alt")) if img is not None else ""
        brand = ""
        for el in it.select("[class*='brand']"):
            t = text_of(el)
            if t and len(t) <= 30 and not re.search(r"\d.*lei", t, re.I):
                brand = t
                break
        title = join_title(brand, name)
        if len(title) < 3:
            return None
        prices = [el for el in it.select("span.vtmn-price, .vtmn-price") if el.find(class_="vtmn-price") is None]
        current = [el for el in prices if not is_old_el(el) and not any(is_old_el(p) for p in el.parents if p is not it)
                   and not any("strikethrough" in c or "crossed" in c for c in el.get("class") or [])]
        price_el, price = None, None
        for el in current:
            p = current_price(el)
            if p:
                price_el, price = el, p
                break
        if not price:
            price_el, price = pick_price(it, "[class*='price']")
        if not price:
            return None
        old = old_price_in(it, price, exclude=[price_el])
        if not old:
            for el in prices:
                if el is price_el:
                    continue
                v = current_price(el)
                if v and price < v < price * 5 and (old is None or v > old):
                    old = v
        stock = False if stock_from_text(text_of(it)) is False else None
        pid = norm(it.get("data-supermodelid")) or decathlon_pid(url)
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=pid or None, image=image_of(it, base, "img"), source="decathlon",
        )
