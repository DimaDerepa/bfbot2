"""Helpers shared by the mass-retail adapters (Flanco, Dedeman, Carrefour, Leroy Merlin).

Not a store module: the leading underscore keeps it out of the registry scan.

* `price_text` - Romanian price text, including decimals rendered in a separate element
  ("2.999,<sup>99</sup> lei", "2.599 99 Lei", "2.999, 99 lei", "279.00 lei /bucata").
* `magento_cards` - stricter Magento 2 listing parser than platforms.parse_magento_listing:
  scoped to the search results, data-price-amount first, old price only if higher, stock from
  `.stock.unavailable` / "Stoc epuizat" / add-to-cart form, images, ids from URL or data-product-id.
* `RoMagentoStore` - Magento2Store subclass using the parser above (+ PDP fallback for product()).
"""
from __future__ import annotations

import re
from typing import Callable, ClassVar, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..extract import make_soup
from ..models import Offer
from ..net import BlockedError
from ..prices import element_price_text, parse_price
from .platforms import Magento2Store

# --------------------------------------------------------------------------- urls / prices


def clean_url(href: Optional[str], base: str) -> str:
    """Absolute URL without query string / fragment (tracking params, list positions)."""
    p = urlsplit(urljoin(base, (href or "").strip()))
    return urlunsplit((p.scheme or "https", p.netloc, p.path, "", ""))


_GLUE_SEP_RE = re.compile(r"(\d)\s*([.,])\s+(\d)|(\d)\s+([.,])\s*(\d)")
# "2.599 99 Lei": integer part, a space, two decimals (decimals rendered in their own element)
_SPLIT_DEC_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+)\s+(\d{2})\s*(?:lei|ron)\b", re.I)


def _glue(m: re.Match) -> str:
    if m.group(1) is not None:
        return m.group(1) + m.group(2) + m.group(3)
    return m.group(4) + m.group(5) + m.group(6)


def glue_separators(text: str) -> str:
    """'2.999, 99 lei' / '2.999 ,99 lei' -> '2.999,99 lei' (separator split from the decimals by markup)."""
    t = str(text).replace("\xa0", " ").replace("\u202f", " ")
    return _GLUE_SEP_RE.sub(_glue, t)


def price_text(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    t = glue_separators(text)
    m = _SPLIT_DEC_RE.search(t)
    if m:
        return parse_price(f"{m.group(1)},{m.group(2)} lei")
    return parse_price(t)


def el_price(el) -> Optional[float]:
    """Price of an element: data-price-amount / data-price / content first, then its text."""
    if el is None:
        return None
    for attr in ("data-price-amount", "data-price", "content", "data-value"):
        v = el.get(attr) if hasattr(el, "get") else None
        if v not in (None, ""):
            p = parse_price(v)
            if p:
                return p
    return price_text(element_price_text(el))


def norm_text(el) -> str:
    if el is None:
        return ""
    return " ".join(el.get_text(" ", strip=True).split())


def image_of(card: Tag, base: str, selector: str = "img") -> Optional[str]:
    img = card.select_one(selector) or card.find("img")
    if img is None:
        return None
    for attr in ("data-src", "data-original", "data-lazy", "data-lazy-src", "src"):
        v = (img.get(attr) or "").strip()
        if v and not v.startswith("data:"):
            return urljoin(base, v)
    srcset = (img.get("data-srcset") or img.get("srcset") or "").strip()
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first and not first.startswith("data:"):
            return urljoin(base, first)
    return None


# --------------------------------------------------------------------------- page state

_BLOCK_TITLES = (
    "just a moment", "attention required", "access denied", "are you a robot", "verify you are human",
    "pardon our interruption", "request unsuccessful", "captcha", "security check", "one more step",
)


def raise_if_challenge(soup: BeautifulSoup, html: str, url: str) -> None:
    """Raise BlockedError for an anti-bot page that came back with HTTP 200 (call only when nothing parsed)."""
    title = norm_text(soup.title).lower() if soup.title else ""
    if any(m in title for m in _BLOCK_TITLES):
        raise BlockedError(f"challenge page: {title[:60]}", 200, url)
    low = html[:300000].lower()
    if "captcha-delivery.com" in low or "geo.captcha-delivery" in low or "cf-chl-widget" in low:
        raise BlockedError("challenge page (captcha)", 200, url)


_NO_RESULTS = (
    "nu a returnat niciun rezultat", "nu a returnat nici un rezultat", "nu am gasit niciun", "nu am găsit niciun",
    "nu au fost gasite produse", "nu au fost găsite produse", "nu am gasit produse", "nu am găsit produse",
    "your search returned no results", "0 rezultate pentru", "niciun rezultat pentru", "nu exista produse",
    "nu există produse",
)


def no_results(root) -> bool:
    txt = norm_text(root).lower()
    return any(m in txt for m in _NO_RESULTS)


# --------------------------------------------------------------------------- stock

_OOS_TEXT = (
    "stoc epuizat", "produs indisponibil", "momentan indisponibil", "nu mai este disponibil", "nu este disponibil",
    "nu este in stoc", "nu este în stoc", "indisponibil online", "out of stock", "fara stoc", "fără stoc",
)
_IN_TEXT = ("în stoc", "in stoc", "stoc limitat", "ultimele bucati", "ultimele bucăți", "ultimul produs")


def stock_of(card: Tag) -> Optional[bool]:
    if card.select_one(".stock.unavailable, .out-of-stock, .product-out-of-stock, .unavailable.stock"):
        return False
    txt = norm_text(card).lower()
    if any(m in txt for m in _OOS_TEXT):
        return False
    for el in card.find_all(["span", "div", "p", "strong", "label"]):
        if el.get_text(" ", strip=True).lower() in ("indisponibil", "epuizat"):
            return False
    if card.select_one(".stock.available"):
        return True
    if any(m in txt for m in _IN_TEXT):
        return True
    btn = card.select_one("button.tocart, .action.tocart, form[data-role='tocart-form'] button")
    if btn is not None and not btn.has_attr("disabled"):
        return True
    return None


_SELLER_PREFIX_RE = re.compile(
    r"^(?:v[âa]ndut(?:\s+(?:și|si|ş)\s+livrat)?\s+de|v[âa]nz[ăa]tor|seller|oferit\s+de)\s*:?\s*", re.I)


_SOLD_BY_RE = re.compile(r"v[âa]ndut(?:\s+(?:și|si|ş)\s+livrat)?\s+de", re.I)


def seller_of(card: Tag, own_names: tuple[str, ...]) -> Optional[str]:
    """Marketplace seller of a card ("Vândut de: X" / element with class *seller*); None for the shop itself."""
    t = ""
    el = card.select_one("[class*='seller'], [class*='vendor'], [class*='sold-by'], [class*='soldby']")
    if el is not None:
        t = _SELLER_PREFIX_RE.sub("", norm_text(el)).strip(" :-")
    if not t:
        s = card.find(string=_SOLD_BY_RE)
        if s is not None and isinstance(s.parent, Tag):
            t = _SELLER_PREFIX_RE.sub("", norm_text(s.parent)).strip(" :-")
            if not t:
                sib = s.parent.find_next_sibling()
                t = norm_text(sib).strip(" :-") if sib is not None else ""
    if not t or len(t) > 60 or any(n in t.lower() for n in own_names):
        return None
    return t


# --------------------------------------------------------------------------- Magento 2 listing


def _is_old(el: Tag, stop: Tag) -> bool:
    node = el
    while isinstance(node, Tag):
        if node.name in ("del", "s", "strike"):
            return True
        if node.get("data-price-type") == "oldPrice":
            return True
        if "old-price" in (node.get("class") or []):
            return True
        if node is stop:
            break
        node = node.parent
    return False


def _find_cards(root) -> list[Tag]:
    cards = root.select("li.product-item") or root.select("div.product-item-info") or root.select("div.product-item")
    if cards:
        return cards
    found: list[Tag] = []
    ids: set[int] = set()
    for a in root.select("a.product-item-link[href]"):
        node = a
        for _ in range(6):
            node = node.parent
            if not isinstance(node, Tag) or node.name in ("body", "html"):
                node = None
                break
            if node.select_one("[data-price-type], .price-box, .price"):
                break
        if node is not None and id(node) not in ids:
            ids.add(id(node))
            found.append(node)
    return found


def magento_cards(soup: BeautifulSoup, page_url: str, store: str, *,
                  id_from_url: Optional[Callable[[str], Optional[str]]] = None,
                  own_names: tuple[str, ...] = (), with_seller: bool = False,
                  source: str = "magento2") -> list[Offer]:
    root = soup.select_one(".search.results") or soup.select_one(".products.wrapper") or soup
    cards = _find_cards(root)
    if not cards and root is not soup and not no_results(root):
        cards = _find_cards(soup)
    out: list[Offer] = []
    seen: set[str] = set()
    for card in cards:
        o = _card_offer(card, page_url, store, id_from_url, own_names, with_seller, source)
        if o is not None and o.key not in seen:
            seen.add(o.key)
            out.append(o)
    return out


def _card_offer(card: Tag, page_url: str, store: str, id_from_url, own_names, with_seller, source) -> Optional[Offer]:
    a = card.select_one("a.product-item-link[href]") or card.select_one(
        ".product-item-name a[href], .product-name a[href], h2 a[href], h3 a[href]")
    if a is None:
        return None
    title = norm_text(a) or " ".join((a.get("title") or "").split())
    url = clean_url(a.get("href"), page_url)
    if not title or not url.startswith("http"):
        return None
    box = card.select_one(".price-box") or card
    price: Optional[float] = None
    for sel in ('[data-price-type="finalPrice"]', '[data-price-type="minPrice"]'):
        el = box.select_one(sel)
        if el is not None:
            price = el_price(el)
            if price:
                break
    if not price:
        for sel in (".special-price .price", ".price-final_price .price", ".price"):
            for el in box.select(sel):
                if _is_old(el, box):
                    continue
                price = price_text(element_price_text(el))
                if price:
                    break
            if price:
                break
    if not price:
        return None
    old_el = box.select_one('[data-price-type="oldPrice"]') or box.select_one(".old-price .price, .old-price")
    old = el_price(old_el) if old_el is not None else None

    pid = id_from_url(url) if id_from_url else None
    if not pid:
        holder = box if box.get("data-product-id") else (card if card.get("data-product-id") else card.select_one("[data-product-id]"))
        pid = holder.get("data-product-id") if holder is not None else None
    if not pid:
        inp = card.select_one("input[name='product'][value]")
        pid = inp.get("value") if inp is not None else None
    return Offer(
        store=store, title=title, url=url, price=price,
        old_price=old if old and old > price else None,
        in_stock=stock_of(card),
        offer_id=str(pid).strip() if pid and str(pid).strip() else None,
        image=image_of(card, page_url, "img.product-image-photo"),
        seller=seller_of(card, own_names) if with_seller else None,
        source=source,
    )


def magento_pdp(soup: BeautifulSoup, url: str, store: str) -> Optional[Offer]:
    """Magento 2 product page: .product-info-main price box (used when JSON-LD is missing/poor)."""
    main = soup.select_one(".product-info-main") or soup.select_one(".product-info") or None
    if main is None:
        return None
    box = main.select_one(".price-box") or main
    price = el_price(box.select_one('[data-price-type="finalPrice"]'))
    if not price:
        for sel in (".special-price .price", ".price-final_price .price", ".price"):
            el = next((e for e in box.select(sel) if not _is_old(e, box)), None)
            price = price_text(element_price_text(el)) if el is not None else None
            if price:
                break
    if not price:
        return None
    h1 = soup.select_one("h1.page-title") or soup.find("h1")
    title = norm_text(h1)
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = (og.get("content") or "").strip() if og else ""
    if not title:
        return None
    old_el = box.select_one('[data-price-type="oldPrice"]') or box.select_one(".old-price .price, .old-price")
    old = el_price(old_el) if old_el is not None else None
    return Offer(store=store, title=title, url=url, price=price, old_price=old if old and old > price else None,
                 in_stock=stock_of(main), source="magento2-pdp")


class RoMagentoStore(Magento2Store):
    """Magento 2 storefront with the stricter card parser (no `key`: never registered itself)."""

    search_path: ClassVar[str] = "/catalogsearch/result/"
    page_param: ClassVar[str] = "p"
    extra_params: ClassVar[dict] = {}
    id_re: ClassVar[Optional[re.Pattern]] = None     # stable product id inside the URL path
    with_seller: ClassVar[bool] = False
    own_names: ClassVar[tuple[str, ...]] = ()

    def search_params(self, query: str, page: int) -> dict:
        params = {"q": query.strip(), **self.extra_params}
        if page > 1:
            params[self.page_param] = str(page)
        return params

    def id_from_url(self, url: str) -> Optional[str]:
        if self.id_re is None:
            return None
        m = self.id_re.search(urlsplit(url).path)
        return m.group(1) if m else None

    def search_page(self, query: str, page: int) -> list[Offer]:
        html, soup, final = self.get_html(self.base_url.rstrip("/") + self.search_path,
                                          params=self.search_params(query, page))
        out = magento_cards(soup, final, self.key, id_from_url=self.id_from_url,
                            own_names=self.own_names or (self.name.lower(),), with_seller=self.with_seller)
        if out:
            self.last_strategy = "magento2-cards"
            return out
        raise_if_challenge(soup, html, final)
        if no_results(soup):
            self.last_strategy = "no-results"
            return []
        out = self.fallback(html, final, soup)
        seen: set[str] = set()
        uniq: list[Offer] = []
        for o in out:
            o.url = clean_url(o.url, final)
            o.offer_id = self.id_from_url(o.url) or o.offer_id
            if o.key not in seen:
                seen.add(o.key)
                uniq.append(o)
        return uniq

    def product(self, url: str) -> Optional[Offer]:
        o = super().product(url)
        soup = make_soup(self.last_raw) if self.last_raw else None
        pdp = magento_pdp(soup, url, self.key) if soup is not None else None
        if pdp is not None and (o is None or o.source == "meta"):
            o = pdp
        elif o is not None and pdp is not None and abs(pdp.price - o.price) < 0.5:
            if o.old_price is None and pdp.old_price:
                o.old_price = pdp.old_price
            if o.in_stock is None:
                o.in_stock = pdp.in_stock
        if o is None:
            return None
        o.url = url
        o.store_name = self.name
        o.offer_id = self.id_from_url(url) or o.offer_id
        return o
