"""Carrefour Romania (carrefour.ro, no www) - hypermarket + marketplace, Magento 2 (theme CarrefourMarketplace).

Search: GET https://carrefour.ro/catalogsearch/result/?q={quote_plus(q)}   page N: &p=N   (24 per page)
        (robots.txt reportedly disallows search pages -> keep frequency low)

Real listing markup (verified on live pages, 2026-11) - NOT the stock Magento selectors:
    <ol class="products list">
      <li class="product" data-product-id="1299403" data-type="catalog-product">
        <div class="productItem category-listing">
          <div class="productItem-name"><a href="https://carrefour.ro/produse/<slug>-19-42509267" title="...">Title</a>
          <div class="price-box" data-product-id="1299403">
            <link itemprop="availability" href="https://schema.org/InStock">        (only on some cards)
            <div class="price price-old" id="old-price-...">169 <span>99</span> Lei</div>   (struck price, no data attr)
            <div class="price price-final" data-price-amount="135,99" itemprop="price">135 <span>99</span> Lei</div>
            <meta itemprop="price" content="135.99"><meta itemprop="priceCurrency" content="RON">
          <button class="action tocart" disabled data-simple-id="19-42509267" data-price="135.99"
                  data-dimension10="available">                                   (disabled until JS runs)
`data-price-amount` uses Romanian decimals ("1.249,99"); visible text renders the decimals in a separate element
("2.599 99 Lei" = 2599.99). Product URL https://carrefour.ro/produse/{slug}-{sku}, sku like "19-42509267" (stable id).
No results: <h1 class="page-title">... <span>0 rezultate gasite</span>, `.products.wrapper.hide`, no cards.
The header mega-menu is full of price-bucket promo links ("Articole bebe sub 10 ron", "Jucarii intre 50 si 150 lei")
that a generic price heuristic mistakes for products -> only /produse/<slug>-<sku> URLs are ever returned.
Seller line: "Supermarket Online" / Carrefour (own) vs marketplace partners (-> Offer.seller).
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ..models import Offer
from ..prices import element_price_text, parse_price
from ._ro_retail import (
    RoMagentoStore, _in_widget, clean_url, image_of, magento_cards, no_results, norm_text, price_text,
    raise_if_challenge, seller_of, stock_of,
)

_ID_RE = re.compile(r"[/-](\d{1,3}-\d{5,})/?$")
_PRODUCT_PATH_RE = re.compile(r"^/produse/(?:[^/]+-)?\d{1,3}-\d{5,}/?$")
_ZERO_RESULTS_RE = re.compile(r"(?<![\d.,])0\s+rezultate\s+g[aă]site", re.I)
_OOS_VALUES = ("out", "unavailable", "indisponibil", "epuizat", "not_available", "notavailable", "no_stock")


def is_product_url(url: str) -> bool:
    p = urlsplit(url)
    return p.netloc.lower().endswith("carrefour.ro") and bool(_PRODUCT_PATH_RE.match(p.path))


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    return parse_price(v)


def _card_price(box: Tag, card: Tag) -> Optional[float]:
    final = box.select_one(".price-final") or box.select_one("[itemprop='price']:not(meta)")
    meta = box.select_one("meta[itemprop='price'][content]")
    btn = card.select_one("button.tocart[data-price], [data-price]")
    for v in ((meta.get("content") if meta is not None else None),
              (final.get("data-price-amount") if final is not None else None),
              (btn.get("data-price") if btn is not None else None)):
        p = _num(v)
        if p:
            return p
    if final is not None:
        return price_text(element_price_text(final))
    return None


def _card_stock(card: Tag) -> Optional[bool]:
    av = card.select_one("[itemprop='availability']")
    if av is not None:
        v = (av.get("href") or av.get("content") or "").lower()
        if "outofstock" in v or "soldout" in v or "discontinued" in v:
            return False
        if "instock" in v or "limitedavailability" in v or "onlineonly" in v:
            return True
    btn = card.select_one("[data-dimension10]")
    if btn is not None:
        v = (btn.get("data-dimension10") or "").strip().lower()
        if v and any(m in v for m in _OOS_VALUES):
            return False
        if v in ("available", "in stock", "in_stock", "instock", "disponibil", "in stoc"):
            return True
    s = stock_of(card)
    return False if s is False else s


def _cards(soup: BeautifulSoup) -> list[Tag]:
    root = soup.select_one(".search.results") or soup
    out: list[Tag] = []
    ids: set[int] = set()
    for item in root.select("div.productItem"):
        if _in_widget(item):
            continue
        card = item.find_parent("li", class_="product") or item
        if id(card) not in ids:
            ids.add(id(card))
            out.append(card)
    return out


def carrefour_cards(soup: BeautifulSoup, page_url: str, store: str, *, own_names: tuple[str, ...] = (),
                    with_seller: bool = False) -> list[Offer]:
    out: list[Offer] = []
    seen: set[str] = set()
    for card in _cards(soup):
        a = card.select_one(".productItem-name a[href]") or card.select_one("a.productItem-image[href]")
        if a is None:
            continue
        url = clean_url(a.get("href"), page_url)
        if not is_product_url(url):
            continue
        title = " ".join((a.get("title") or "").split()) or norm_text(a)
        if not title:
            img = card.select_one("img[alt]")
            title = " ".join((img.get("alt") or "").split()) if img is not None else ""
        if not title:
            continue
        box = card.select_one(".price-box") or card
        price = _card_price(box, card)
        if not price:
            continue
        old_el = box.select_one(".price-old") or box.select_one(".old-price .price, .old-price")
        old = (_num(old_el.get("data-price-amount")) or price_text(element_price_text(old_el))) \
            if old_el is not None else None
        m = _ID_RE.search(urlsplit(url).path)
        pid = m.group(1) if m else None
        if not pid:
            holder = card.select_one("[data-simple-id]")
            pid = holder.get("data-simple-id") if holder is not None else card.get("data-product-id")
        o = Offer(
            store=store, title=title, url=url, price=price,
            old_price=old if old and old > price else None,
            in_stock=_card_stock(card),
            offer_id=str(pid).strip() if pid and str(pid).strip() else None,
            image=image_of(card, page_url, "img.product-image-photo"),
            seller=seller_of(card, own_names) if with_seller else None,
            currency="RON",
            source="carrefour-cards",
        )
        if o.key not in seen:
            seen.add(o.key)
            out.append(o)
    return out


def zero_results(soup: BeautifulSoup) -> bool:
    title = soup.select_one("h1.page-title") or soup.select_one(".page-title-wrapper")
    if title is not None and _ZERO_RESULTS_RE.search(norm_text(title)):
        return True
    wrap = soup.select_one(".products.wrapper")
    if wrap is not None and "hide" in (wrap.get("class") or []) and not wrap.select_one("div.productItem"):
        return True
    return no_results(soup.select_one("#maincontent") or soup)


class CarrefourStore(RoMagentoStore):
    key = "carrefour"
    name = "Carrefour"
    base_url = "https://carrefour.ro"
    domains = ("carrefour.ro",)
    categories = ("general", "electronics", "appliances", "home", "pc", "toys")
    page_size = 24
    min_delay = 3.0
    search_path = "/catalogsearch/result/"
    page_param = "p"
    id_re = _ID_RE
    with_seller = True
    own_names = ("carrefour", "supermarket online")

    def search_page(self, query: str, page: int) -> list[Offer]:
        html, soup, final = self.get_html(self.base_url.rstrip("/") + self.search_path,
                                          params=self.search_params(query, page))
        own = self.own_names or (self.name.lower(),)
        out = carrefour_cards(soup, final, self.key, own_names=own, with_seller=self.with_seller)
        if out:
            self.last_strategy = "carrefour-cards"
            return out
        # stock Magento 2 markup (older theme / A-B variants)
        out = [o for o in magento_cards(soup, final, self.key, id_from_url=self.id_from_url, own_names=own,
                                        with_seller=self.with_seller) if is_product_url(o.url)]
        if out:
            self.last_strategy = "magento2-cards"
            return out
        raise_if_challenge(soup, html, final)
        if zero_results(soup):
            self.last_strategy = "no-results"
            return []
        # generic fallback, restricted to real product URLs: the mega-menu price-bucket links are never products
        seen: set[str] = set()
        uniq: list[Offer] = []
        for o in self.fallback(html, final, soup):
            o.url = clean_url(o.url, final)
            if not is_product_url(o.url):
                continue
            o.offer_id = self.id_from_url(o.url) or o.offer_id
            if o.key not in seen:
                seen.add(o.key)
                uniq.append(o)
        return uniq
