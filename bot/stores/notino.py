"""Notino Romania (notino.ro) - perfume & cosmetics; server-rendered React (styled-components, hashed classes).

Search: GET https://www.notino.ro/search.asp?exps={quote(q, safe='')}   spaces -> %20 (search.asp?exps=dior%20sauvage)
        Only page 1: paginated listing URLs (?f=...) are answered 403 by Cloudflare even on residential IPs
        (hlidac-shopu 2026), so page > 1 is never requested.
Listing cards (pretzi RO / notino.lv selectors - class names are hashed, only data-testid is stable):
  [data-testid="product-container"] (category pages also: div[data-product])
    a[href]  /{brand-slug}/{product-slug}/ (+ /p-{variantId}/ for a variant)  -> offer_id = variantId when present
    h2 = brand, h3 = product name, [data-testid*=description] = "Eau de Parfum pentru bărbați"
         -> title "Dior Sauvage Eau de Parfum pentru bărbați"; img[alt] as fallback
    [data-testid="price-component"] | [data-testid="product-price"]   "189 lei" / "1.299,90 lei"
    unit prices ("12,50 RON / 1 ml") are never the price; struck / "original" price -> old_price
Product page (URL watches; NOT Cloudflare-gated per hlidac-shopu): <script id="__APOLLO_STATE__"> JSON
  CatalogVariant:{id}: webId, url, name, variantName, additionalInfo, price.value (RON float), originalPrice.value,
  recentMinPrice.value (EU Omnibus 30-day low), availability.state ("CanBeBought" = in stock),
  attributes.VoucherDiscount.discountedPrice / ConditionalVoucherDiscount (Black-Friday voucher price);
  Brand:* .name.  Price logic (hlidac-shopu): voucher price -> current = voucher, old = recentMinPrice or price;
  else current = price, old = recentMinPrice if price < recentMinPrice < originalPrice else originalPrice.
  Selected variant: /p-{id}/ in the URL or input[name=productId]. Fallbacks: [data-testid="price-variant"][content],
  #pd-price span[content], JSON-LD / meta.
  A search that redirects straight to a product page returns its variants from the same JSON.
Anti-bot: Cloudflare Bot Management (libergent's plain fetch got a challenge 403 on 2026-09-28) -> BlockedError.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ..extract import extract_product_page, make_soup
from ..models import Offer
from .base import Store
from ._fashion import (
    NextPager, best_old, clean_url, dedupe, dig, first_el, image_of, join_title, json_at, labeled_prices, landed_on_home,
    money_values, norm, num, old_price_in, outermost, pick_price, raise_if_challenge, stock_from_text, text_of,
)

_VARIANT_RE = re.compile(r"/p-(\d+)/?$")
_CATALOG_VARIANT_RE = re.compile(r"^CatalogVariant:(\d+)$")


def notino_variant(url: Optional[str]) -> Optional[str]:
    m = _VARIANT_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def apollo_state(html: str, soup: Optional[BeautifulSoup] = None) -> dict:
    soup = soup if soup is not None else make_soup(html)
    sc = soup.find("script", id="__APOLLO_STATE__")
    raw = (sc.string or sc.get_text() or "") if sc is not None else ""
    raw = raw.strip()
    if raw:
        while raw.endswith(";"):
            raw = raw[:-1].rstrip()
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            pass
        brace = raw.find("{")
        data = json_at(raw, brace) if brace >= 0 else None      # "window.__APOLLO_STATE__ = {...};" / trailing junk
        if isinstance(data, dict):
            return data
        try:  # hlidac-shopu reads it as JSON.parse(text.replace(/;/g, ""))
            data = json.loads(raw.replace(";", ""))
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    i = (html or "").find("__APOLLO_STATE__")
    while i >= 0:
        j = html.find("=", i)
        k = html.find("{", i)
        if 0 <= j < k and k - j < 20:
            data = json_at(html, k)
            if isinstance(data, dict):
                return data
        i = html.find("__APOLLO_STATE__", i + 1)
    return {}


def _conditional_voucher(v: Any) -> Optional[float]:
    if not isinstance(v, dict):
        return None
    conds = [c for c in v.get("discountConditions") or [] if isinstance(c, dict)]
    for c in conds:
        if c.get("productMeetsCondition"):
            return num(c.get("discountedPrice"))
    if v.get("conditionType") != "Price" or not conds:
        return None
    conds.sort(key=lambda c: num(c.get("conditionMin")) or 0)
    return num(conds[0].get("discountedPrice"))


def apollo_prices(v: dict) -> tuple[Optional[float], Optional[float]]:
    """(current, old) of a CatalogVariant, hlidac-shopu logic."""
    price = num(dig(v, "price", "value"))
    orig = num(dig(v, "originalPrice", "value"))
    recent = num(dig(v, "recentMinPrice", "value"))
    attrs = v.get("attributes") if isinstance(v.get("attributes"), dict) else {}
    voucher = num(dig(attrs, "VoucherDiscount", "discountedPrice")) or _conditional_voucher(
        attrs.get("ConditionalVoucherDiscount"))
    if voucher and (not price or voucher < price):
        old = recent or price
        return voucher, old if old and old > voucher else None
    if not price:
        return None, None
    old = recent if (recent and orig and price < recent < orig) else orig
    return price, old if old and old > price else None


class NotinoStore(NextPager, Store):
    key = "notino"
    name = "Notino"
    base_url = "https://www.notino.ro"
    domains = ("notino.ro",)
    categories = ("beauty",)
    page_size = 24
    min_delay = 4.0
    extra_headers = {"Referer": "https://www.notino.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/search.asp?exps={self.q_path(query)}"

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            return []  # ?f= pagination is Cloudflare-gated
        url = self.search_url(query)
        html, soup, final = self.get_html(url)
        if landed_on_home(url, final):
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect:home"
            return []
        state = apollo_state(html, soup)
        # a product detail page (add-to-cart form with productId / a /p-{id}/ URL), not a brand or category listing
        on_product = not urlsplit(final).path.lower().startswith("/search") and (
            bool(notino_variant(final)) or soup.select_one("input[name=productId]") is not None)
        if on_product:
            # the search went straight to a product page: its variants, not the "similar products" tiles
            variants = self.apollo_offers(state, final)
            if variants:
                self.last_strategy = "apollo"
                return variants
        cards = outermost(soup.select('[data-testid="product-container"]')) or outermost(soup.select("div[data-product]"))
        out = dedupe(self._card(c, final) for c in cards)
        if out:
            self.last_strategy = "cards"
            return out
        variants = [] if on_product else self.apollo_offers(state, final)
        if variants:
            self.last_strategy = "apollo"
            return variants
        raise_if_challenge(html, soup, final)
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ listing
    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        a = c if c.name == "a" and c.get("href") else c.select_one("a[href]")
        if a is None:
            return None
        url = clean_url(a.get("href"), base)
        if not url or not urlsplit(url).netloc.endswith("notino.ro") or urlsplit(url).path in ("", "/"):
            return None
        brand = text_of(first_el(c, '[data-testid="product-brand"]', '[data-testid*="brand"]', "h2"))
        name = text_of(first_el(c, '[data-testid="product-name"]', '[data-testid="product-title"]', "h3"))
        desc = text_of(first_el(c, '[data-testid="product-description"]', '[data-testid*="description"]'))
        if desc and (len(desc) > 90 or money_values(desc)):
            desc = ""
        img = c.select_one("img[alt]")
        alt = norm(img.get("alt")) if img is not None else ""
        title = join_title(brand, name, desc) or alt
        if not title:
            return None
        price_el, price = pick_price(c, '[data-testid="price-component"]', '[data-testid="product-price"]',
                                     '[data-testid*="price"]', '[class*="price"]', '[class*="Price"]')
        if not price:
            return None
        old = old_price_in(c, price, exclude=[price_el])
        orig_lbl, low = labeled_prices(text_of(el) for el in c.find_all(["div", "span", "p"])
                                       if el.find(["div", "p"]) is None)
        old = best_old(price, old or orig_lbl, low)
        stock = False if stock_from_text(text_of(c)) is False else None
        pid = notino_variant(url)
        if not pid:
            for el in (c, a):
                for attr in ("data-product", "data-id", "data-product-id", "id"):
                    v = str(el.get(attr) or "").strip()
                    if v.isdigit():
                        pid = v
                        break
                if pid:
                    break
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old, in_stock=stock, offer_id=pid,
            image=image_of(c, base, "img"), source="notino",
        )

    # ------------------------------------------------------------------ apollo (product pages)
    def apollo_offers(self, state: dict, base: str, wanted: Optional[str] = None) -> list[Offer]:
        if not state:
            return []
        brand = ""
        for k, v in state.items():
            if isinstance(k, str) and k.startswith("Brand:") and isinstance(v, dict) and v.get("name"):
                brand = norm(v["name"])
                break
        out: list[Offer] = []
        for k, v in state.items():
            m = _CATALOG_VARIANT_RE.match(k) if isinstance(k, str) else None
            if not m or not isinstance(v, dict):
                continue
            vid = str(v.get("webId") or m.group(1))
            if wanted and vid != wanted and m.group(1) != wanted:
                continue
            cur = str(dig(v, "price", "currency") or "RON").upper()
            if cur not in ("RON", "LEI"):
                continue
            price, old = apollo_prices(v)
            if not price:
                continue
            title = join_title(brand, v.get("name"), v.get("variantName"), v.get("additionalInfo"))
            if not title:
                continue
            state_txt = str(dig(v, "availability", "state") or "")
            stock = (state_txt == "CanBeBought") if state_txt else None
            href = v.get("url")
            url = clean_url(href, self.base_url) if isinstance(href, str) and href else clean_url(base, self.base_url)
            out.append(Offer(store=self.key, title=title, url=url, price=price, old_price=old, in_stock=stock,
                             offer_id=vid, source="notino-apollo"))
        return dedupe(out)

    def product(self, url: str) -> Optional[Offer]:
        resp = self.http.get(url)
        self.last_raw = resp.text
        soup = make_soup(resp.text)
        wanted = notino_variant(url)
        if not wanted:
            inp = soup.select_one("input[name=productId]")
            wanted = str(inp.get("value") or "").strip() if inp is not None else None
        state = apollo_state(resp.text, soup)
        offers = self.apollo_offers(state, url, wanted or None) if wanted else []
        if not offers and state:
            offers = sorted(self.apollo_offers(state, url), key=lambda o: (o.in_stock is not True, o.price))
        o: Optional[Offer] = offers[0] if offers else None
        if o is None:
            el = soup.select_one('[data-testid="price-variant"][content], #pd-price span[content], '
                                 'span[data-testid="pd-price"]')
            price = num(el.get("content")) if el is not None and el.get("content") else None
            if price is None and el is not None:
                price = num(text_of(el))
            h1 = soup.select_one('h1[data-testid="pd-header-title"]') or soup.find("h1")
            if price and h1 is not None:
                o = Offer(store=self.key, title=text_of(h1), url=url, price=price, offer_id=wanted or None,
                          in_stock=stock_from_text(text_of(soup.select_one("#pdAddToCart"))), source="notino-pdp")
        if o is None:
            raise_if_challenge(resp.text, soup, url)
            o = extract_product_page(resp.text, url, self.key)
        if o is not None:
            o.url = url
            o.store_name = self.name
        return o
