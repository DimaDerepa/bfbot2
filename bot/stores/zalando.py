"""Zalando Romania (zalando.ro) - fashion & beauty; catalog HTML with an embedded GraphQL cache.

Search: GET https://www.zalando.ro/catalog/?q={quote_plus(q)}   e.g. /catalog/?q=adidas+samba
        page N > 1: &p={N} (shared by every Zalando storefront, 84 products per page).
        An exact SKU query (?q=AD115O0NI-A11) redirects to the product page -> one offer from its JSON-LD.
Data (ScrapeUnblocker/zalando-scraper parsing.py, Oct 2026):
  1. GraphQL cache in `window.__hydrationDataConsume({...})` / `runtime['hydratePartial']({...})` script calls
     ({"graphqlCache"|"cache": {key: {data: ...}}}, all calls merged; older pages: <script type=application/json
     class="re-data-el-hydrate">).
       listing  = the entry whose data.collection.entities has edges[] + pageInfo{currentPage, numberOfPages};
                  slots = edges[].node with __typename "Product", id "ern:product::{sku}"
                  (visibility.__typename "AdvertisingVisibility" = sponsored slot, still a real product)
       cards    = entries whose data.product has sku + displayPrice + uri + brand:
                  displayPrice.original.amount / .promotional.amount  (MINOR units, bani: 49999 = 499.99 lei;
                  promotional present -> it is the current price and original the struck one),
                  displayPriceModule.prices[].block.content[].text  (visible lines; one is the EU Omnibus
                  "lowest price of the last 30 days"), brand.name, name (leading "- " dropped), uri,
                  mediumDefaultMedia.uri
  2. Only the first couple dozen cards have JSON; the others are read from the rendered grid: <article> with an
     <a href> matching -([a-z0-9]{9}-[a-z0-9]{3}).html (the SKU), h3/h2 spans = brand, name, and <section> <p> price
     lines (first amount = current, highest other = original, lowest of two others = 30-day low).
     Class names are obfuscated and never used.
  old_price = the Omnibus 30-day low when it is above the price, else the struck original (see _fashion.best_old).
Product page: JSON-LD ProductGroup.hasVariant[].offers{price, availability} + cache simples[].offer.price.
Prices on the page: "499,99 lei" (decimal comma, '.' thousands, NBSP before "lei").
Anti-bot: Akamai Bot Manager (AWS IPs blocked; Azure/GitHub-Actions IPs worked; libergent's plain fetch of
  /catalog/?q= succeeded 2026-09-28) -> low rate, block page -> BlockedError.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from bs4 import BeautifulSoup

from ..extract import availability_flag, iter_jsonld, make_soup
from ..models import Offer
from .base import Store
from ._fashion import (
    NextPager, best_old, clean_url, dedupe, dig, json_at, join_title, landed_on_home, minor, norm, num, price_lines,
    raise_if_challenge, text_of,
)

_CACHE_CALL_RE = re.compile(r"__hydrationDataConsume\(|\[['\"]hydratePartial['\"]\]\(")
_SKU_IN_URL = re.compile(r"-([a-z0-9]{9}-[a-z0-9]{3})\.html", re.I)


def zalando_sku(url: Optional[str]) -> Optional[str]:
    m = _SKU_IN_URL.search(url or "")
    return m.group(1).upper() if m else None


def graphql_cache(html: str, soup: Optional[BeautifulSoup] = None) -> dict[str, Any]:
    """Every GraphQL cache entry embedded in the page, merged into one dict."""
    cache: dict[str, Any] = {}
    for m in _CACHE_CALL_RE.finditer(html or ""):
        data = json_at(html, m.end())
        if not isinstance(data, dict):
            continue
        block = data.get("graphqlCache", data.get("cache"))
        if isinstance(block, dict):
            for k, v in block.items():
                cache.setdefault(k, v)
    if not cache and soup is not None:  # older storefront format
        for sc in soup.select("script.re-data-el-hydrate"):
            try:
                data = json.loads(sc.string or "")
            except ValueError:
                continue
            block = data.get("graphqlCache", data.get("cache")) if isinstance(data, dict) else None
            if isinstance(block, dict):
                for k, v in block.items():
                    cache.setdefault(k, v)
    return cache


def _data(v: Any) -> dict:
    return v["data"] if isinstance(v, dict) and isinstance(v.get("data"), dict) else {}


def _catalog(cache: dict[str, Any]) -> Optional[dict]:
    best: Optional[dict] = None
    for v in cache.values():
        col = _data(v).get("collection")
        ent = col.get("entities") if isinstance(col, dict) else None
        if not isinstance(ent, dict) or not isinstance(ent.get("edges"), list) or not isinstance(ent.get("pageInfo"), dict):
            continue
        if best is None or len(ent["edges"]) > len(best["edges"]):
            best = ent
    return best


def _slots(entities: dict) -> list[str]:
    out: list[str] = []
    for edge in entities.get("edges") or []:
        node = (edge or {}).get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict) or node.get("__typename") != "Product":
            continue
        eid = node.get("id")
        if isinstance(eid, str) and "::" in eid:
            sku = eid.rsplit("::", 1)[-1].upper()
            if sku and sku not in out:
                out.append(sku)
    return out


def _json_cards(cache: dict[str, Any]) -> dict[str, dict]:
    cards: dict[str, dict] = {}
    for v in cache.values():
        p = _data(v).get("product")
        if isinstance(p, dict) and p.get("sku") and isinstance(p.get("displayPrice"), dict) and p.get("uri") \
                and p.get("brand"):
            cards.setdefault(str(p["sku"]).upper(), p)
    return cards


def _name(v: Any) -> str:
    t = norm(v)
    return t[2:].strip() if t.startswith("- ") else t


def _lines_from_module(p: dict) -> list[str]:
    lines: list[str] = []
    prices = dig(p, "displayPriceModule", "prices")
    for item in prices if isinstance(prices, list) else []:
        content = dig(item, "block", "content")
        if isinstance(content, list):
            lines.append(" ".join(norm(x.get("text")) for x in content if isinstance(x, dict) and x.get("text")))
    return lines


def _is_product_group(obj: dict) -> bool:
    t = obj.get("@type")
    types = {str(x).lower() for x in (t if isinstance(t, list) else [t])}
    return "productgroup" in types or ("product" in types and bool(obj.get("hasVariant")))


class ZalandoStore(NextPager, Store):
    key = "zalando"
    name = "Zalando"
    base_url = "https://www.zalando.ro"
    domains = ("zalando.ro",)
    categories = ("fashion", "beauty")
    page_size = 84
    min_delay = 4.0
    extra_headers = {"Referer": "https://www.zalando.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str, page: int = 1) -> str:
        url = f"{self.base_url}/catalog/?q={self.q_plus(query)}"
        return url + (f"&p={page}" if page > 1 else "")

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page == 1:
            self._pages = 1
        elif getattr(self, "_pages", 1) < page:
            return []
        url = self.search_url(query, page)
        html, soup, final = self.get_html(url)
        if landed_on_home(url, final):
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect:home"
            return []
        cache = graphql_cache(html, soup)
        entities = _catalog(cache)
        if entities is not None:
            try:
                self._pages = int((entities.get("pageInfo") or {}).get("numberOfPages") or 1)
            except (TypeError, ValueError):
                self._pages = 1
        if entities is None and page == 1 and zalando_sku(final):
            # exact-SKU search redirected to the product page: its "you may also like" <article> cards and cache
            # entries are other products, so the page's own JSON-LD comes first
            o = self.parse_product(html, soup, final)
            if o is not None:
                o.store_name = self.name
                self.last_strategy = "product-page"
                return [o]
        out = self.parse_listing(html, soup, final, cache, entities)
        if out:
            return out
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ listing
    def parse_listing(self, html: str, soup: BeautifulSoup, base: str, cache: Optional[dict] = None,
                      entities: Optional[dict] = None) -> list[Offer]:
        cache = graphql_cache(html, soup) if cache is None else cache
        entities = _catalog(cache) if entities is None else entities
        cards = _json_cards(cache)
        grid = self.grid_cards(soup, base)
        order = _slots(entities) if entities else []
        if not order:
            order = list(dict.fromkeys(list(grid) + list(cards)))
        out: list[Offer] = []
        n_json = 0
        for sku in order:
            o = None
            if sku in cards:
                o = self._from_json(cards[sku])
                n_json += o is not None
            if o is None and sku in grid:
                o = grid[sku]
            if o is not None:
                out.append(o)
        if out:
            self.last_strategy = "graphql-cache" if n_json == len(out) else ("graphql+grid" if n_json else "grid")
        return dedupe(out)

    def _from_json(self, p: dict) -> Optional[Offer]:
        sku = str(p.get("sku") or "").upper()
        brand = norm((p.get("brand") or {}).get("name") if isinstance(p.get("brand"), dict) else p.get("brand"))
        title = join_title(brand, _name(p.get("name")))
        url = clean_url(p.get("uri"), self.base_url)
        disp = p.get("displayPrice") or {}
        promo = minor((disp.get("promotional") or {}).get("amount")) if isinstance(disp.get("promotional"), dict) else None
        orig = minor((disp.get("original") or {}).get("amount")) if isinstance(disp.get("original"), dict) else None
        currency = ((disp.get("original") or {}).get("currency") if isinstance(disp.get("original"), dict) else None)
        if currency and str(currency).upper() not in ("RON", "LEI"):
            return None
        price = promo or orig
        if not title or not url or not price:
            return None
        info = price_lines(_lines_from_module(p))
        lowest = info["lowest"]
        if lowest is not None and orig is not None and abs(lowest - orig) < 0.005:
            lowest = None
        old = best_old(price, orig if promo else info["original"], lowest)
        media = p.get("mediumDefaultMedia") or p.get("smallDefaultMedia") or {}
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old, in_stock=None,
            offer_id=sku or zalando_sku(url), image=media.get("uri") if isinstance(media, dict) else None,
            source="zalando-json",
        )

    def grid_cards(self, soup: BeautifulSoup, base: str) -> dict[str, Offer]:
        cards: dict[str, Offer] = {}
        for art in soup.find_all("article"):
            sku = url = None
            for a in art.find_all("a", href=True):
                sku = zalando_sku(a["href"])
                if sku:
                    url = clean_url(a["href"], base)
                    break
            if not sku or not url or sku in cards:
                continue
            heading = art.find("h3") or art.find("h2")
            parts: list[str] = []
            if heading is not None:
                spans = heading.find_all("span", recursive=False) or heading.find_all("span")
                parts = [t for t in (text_of(s) for s in spans) if t] or [text_of(heading)]
            title = join_title(*parts)
            section = art.find("section")
            lines: list[str] = []
            if section is not None:
                ps = section.find_all("p")
                lines = [" ".join(p.stripped_strings) for p in ps] if ps else [" ".join(section.stripped_strings)]
            info = price_lines(lines)
            price = info["price"]
            if not title or not price:
                continue
            img = None
            for im in art.find_all("img"):
                src = im.get("src") or ""
                if src.startswith("http"):
                    img = src
                    break
            cards[sku] = Offer(
                store=self.key, title=title, url=url, price=price,
                old_price=best_old(price, info["original"], info["lowest"]), in_stock=None, offer_id=sku,
                image=img, source="zalando-grid",
            )
        return cards

    # ------------------------------------------------------------------ product page
    def parse_product(self, html: str, soup: BeautifulSoup, url: str) -> Optional[Offer]:
        sku = zalando_sku(url)
        group = next((obj for obj in iter_jsonld(soup) if _is_product_group(obj)), None)
        if group is None:
            return None
        variants = group.get("hasVariant")
        if isinstance(variants, dict):
            variants = [variants]
        best: Optional[tuple[bool, float]] = None
        for v in variants if isinstance(variants, list) else []:
            offers = v.get("offers") if isinstance(v, dict) else None
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if not isinstance(offers, dict):
                continue
            cur = str(offers.get("priceCurrency") or "RON").upper()
            p = num(offers.get("price"))
            if not p or cur not in ("RON", "LEI"):
                continue
            avail = availability_flag(offers.get("availability")) is True   # InStock / LimitedAvailability
            cand = (avail, p)
            if best is None or (cand[0], -cand[1]) > (best[0], -best[1]):
                best = cand
        if best is None:
            return None
        in_stock, price = best
        orig = None
        for v in graphql_cache(html, soup).values():
            prod = _data(v).get("product")
            simples = prod.get("simples") if isinstance(prod, dict) else None
            for s in simples if isinstance(simples, list) else []:
                pr = dig(s, "offer", "price")
                if isinstance(pr, dict):
                    promo = minor((pr.get("promotional") or {}).get("amount")) if isinstance(pr.get("promotional"), dict) else None
                    o = minor((pr.get("original") or {}).get("amount")) if isinstance(pr.get("original"), dict) else None
                    if promo and o and abs(promo - price) < 0.01:
                        orig = o
        brand = group.get("brand")
        brand = brand.get("name") if isinstance(brand, dict) else brand
        title = join_title(brand, _name(group.get("name")))
        image = group.get("image")
        image = image[0] if isinstance(image, list) and image else image
        return Offer(
            store=self.key, title=title, url=clean_url(group.get("url") or url, self.base_url) or url, price=price,
            old_price=orig if orig and orig > price else None, in_stock=in_stock,
            offer_id=str(group.get("productGroupID") or sku or "").upper() or None,
            image=image if isinstance(image, str) else None, source="zalando-pdp",
        )

    def product(self, url: str) -> Optional[Offer]:
        resp = self.http.get(url)
        self.last_raw = resp.text
        soup = make_soup(resp.text)
        o = self.parse_product(resp.text, soup, url)
        if o is None:
            from ..extract import extract_product_page

            o = extract_product_page(resp.text, url, self.key)
            if o is None:
                raise_if_challenge(resp.text, soup, url)
            else:
                o.offer_id = zalando_sku(url) or o.offer_id
        if o is not None:
            o.url = url
            o.store_name = self.name
        return o
