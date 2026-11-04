"""Compari.ro - the biggest Romanian price comparator (Arukereso / Heureka Group platform).

Search route (robots.txt-compliant):
    GET https://www.compari.ro/s/?q={quote_plus(q)}        spaces -> '+', diacritics -> UTF-8 %XX
    = Heureka "One Platform" search (pazaruvaj.com, same group, has used it since Jan 2026; RO is next in
    Heureka's migration plan). The legacy on-site search /CategorySearch.php?st=... is deliberately NOT used:
    compari's robots.txt (Nov 2026) has `Disallow: /*?st=` (plus /Ajax*, /Jump.php, /outgoing/, /*/fn:*,
    /*/f:pret=) and this bot only requests robots-allowed paths. "Next page" links are followed only when they
    are robots-allowed. Until compari serves /s/, this route may answer 404 (diagnostics will show it); pinned
    product pages (URL watches, .../<slug>-p<id>/) always work and give the cheapest shop + number of shops.
Redirects: to the home page (www, path "/") -> [] (promo carousels are not results); to a category listing, incl. a
  category SUBDOMAIN root (https://telefoane-mobile.compari.ro/) -> parsed as a listing; straight to a product page
  (...-p<id>/) -> one Offer from its offer list.
The listing parser understands both layouts, whichever the server returns:
  A) legacy Arukereso markup (still live Nov 2026 on arukereso.hu and on compari category/product pages):
     div.product-box-container > div.product-box[data-akpid="p<id>"]
       div.name h2 a[href][title]                   product page .../<slug>-p<id>/ (category subdomains too)
       div.top-right a.price | .visible-xs .price   "de la 1 989,00 RON" (min over shops) | "476,80 RON" (1 shop)
       a.offer-num | span.offer-num                 "12 oferte" | the shop name/domain for single-shop products
       div.image-link-container img[data-lazy-src]  (p1.akcdn.net thumbs)
       a.image / a.price -> Jump.php (outbound, never fetched); the real page is a.ak-info[href]
  B) Heureka One Platform: li.c-product-list__item[data-testid="product-list-item"]
       [data-testid="product-title-link"] (href contains /p/), [data-testid="product-price"] .c-product__price
       (ranges "1 299 – 1 499 lei" -> lower bound; struck <del>/<s>/"old" children = old_price, never the price),
       img.c-product__image; /p/ URLs may carry no -p<id> -> offer_id None, keyed by the canonical URL;
       heureka.cz variant: section.c-product, a.c-product__link, h3.c-product__title, a.c-product__price
Product page (URL watches), offer list "Magazine şi preţuri": #offer-list .optoffer (device-mobile duplicates dropped
  when device-desktop rows exist, then deduped on shop+price): [itemprop=price][content] | .row-price span, .shopname | [itemprop=seller]
  [itemprop=name] | .col-logo img[alt], .offer-name h4, span.delivery-time ("În stoc"); JSON-LD fallback
  (Product + AggregateOffer lowPrice/offerCount).
Prices: SPACE (or NBSP / U+202F) thousands separator, decimal comma, "RON".
Anti-bot: Cloudflare managed challenge (403 + cf-mitigated: challenge, "Just a moment...") seen from datacenter
  IPs in Sept and Nov 2026 -> BlockedError (no retry). Expect it blocked on GitHub Actions most of the time.
  Research asks for >= 5 s between requests.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..extract import extract_product_page
from ..models import Offer
from .base import Store
from ._aggregators import (
    NextPager, clean_url, count_in, current_and_old, dedupe, el_price, image_of, next_page_url, norm_ws, outermost,
    raise_if_challenge, slugify, text_of,
)

_PID_RE = re.compile(r"-p(\d{3,})(?:/|$)")
_JUMP_RE = re.compile(r"/(?:Jump|JumpAuction)\.php|/outgoing/", re.I)
# a.offer-num labels that are not a shop name ("Vezi oferta", "Compară prețuri", ...)
_GENERIC_LABEL_RE = re.compile(r"ofert[aeăi]|compar[aă]|\bvezi\b|\bdetalii\b|\bmagazine\b|pre[tț]ur|\binfo\b", re.I)
_HOME_HOSTS = ("compari.ro", "www.compari.ro")
# robots.txt (Nov 2026): /*?st=, /Ajax*, /Jump.php, /JumpAuction.php, /outgoing/, /*/fn:*, /*/f:pret=
_DISALLOWED_RE = re.compile(r"[?&]st=|/ajax|/jump(?:auction)?\.php|/outgoing/|/fn:|/f:pret=", re.I)


def compari_pid(url: Optional[str]) -> Optional[str]:
    m = _PID_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def robots_allowed(url: str) -> bool:
    return not _DISALLOWED_RE.search(url or "")


def _jump_url(href: str, base: str) -> str:
    """Outbound Jump.php link, kept only as a clickable shop link (never fetched), cut at '&ProductType'."""
    u = urljoin(base, href.split("&ProductType", 1)[0])
    p = urlsplit(u)
    keep = [(k, v) for k, v in parse_qsl(p.query) if k.lower() in ("productattributeid", "productid")]
    return urlunsplit(("https", p.netloc.lower(), p.path, urlencode(keep), ""))


def _stock(text: str) -> Optional[bool]:
    t = text.lower()
    if any(x in t for x in ("stoc epuizat", "nu este în stoc", "nu este in stoc", "indisponibil", "fără stoc",
                            "fara stoc", "out of stock")):
        return False
    if any(x in t for x in ("în stoc", "in stoc", "pe stoc", "stoc limitat", "disponibil imediat", "livrare imediat")):
        return True
    return None


class CompariStore(NextPager, Store):
    key = "compari"
    name = "Compari.ro"
    base_url = "https://www.compari.ro"
    domains = ("compari.ro",)
    categories = ("electronics", "pc", "home", "appliances", "fashion", "beauty", "general", "diy", "toys", "books")
    aggregator = True
    page_size = 20
    min_delay = 5.0
    extra_headers = {"Referer": "https://www.compari.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/s/?q={self.q_plus(query)}"

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page == 1:
            self._pager_reset()
            url = self.search_url(query)
        else:
            url = self._pager_get(page)
            if not url:
                return []
        html, soup, final = self.get_html(url)
        fp = urlsplit(final)
        if fp.netloc.lower() in _HOME_HOSTS and fp.path in ("", "/"):
            # redirected to the home page: its promo carousels are not search results. (A category SUBDOMAIN root,
            # e.g. https://telefoane-mobile.compari.ro/, is a real listing and is parsed below.)
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect-home"
            return []
        if page == 1 and compari_pid(final):
            # an unambiguous query can land straight on the product page (...-p<id>/): read its offer list
            o = self.parse_product_page(soup, clean_url(final, self.base_url))
            if o is not None:
                o.store_name = self.name
                self.last_strategy = "product-page"
                return [o]
        out = self.parse_listing(soup, final)
        if out:
            nxt = next_page_url(soup, final)
            if nxt and robots_allowed(nxt):
                self._pager_set(page + 1, nxt)
            return out
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ listing
    def parse_listing(self, soup: BeautifulSoup, base: str) -> list[Offer]:
        items = outermost(soup.select('[data-testid="product-list-item"]'))
        if items:
            self.last_strategy = "heureka-cards"
            return dedupe(self._card_b(li, base) for li in items)
        boxes = outermost(soup.select("div.product-box"))
        if boxes:
            self.last_strategy = "legacy-cards"
            return dedupe(self._card_a(b, base) for b in boxes)
        sections = outermost(soup.select("section.c-product"))
        if sections:
            self.last_strategy = "heureka-cz-cards"
            return dedupe(self._card_b(s, base) for s in sections)
        return []

    @staticmethod
    def _product_link(card: Tag, preferred: list[Optional[Tag]], base: str) -> tuple[str, Optional[str]]:
        """(url, product id): product page (-p<id>/) over any other on-site link over the outbound Jump link."""
        anchors = [a for a in preferred if a is not None and a.get("href")]
        anchors += [a for a in card.select("a[href]") if a not in anchors]
        for a in anchors:
            href = a["href"]
            if not _JUMP_RE.search(href) and compari_pid(urljoin(base, href)):
                url = clean_url(href, base)
                return url, compari_pid(url)
        for a in anchors:
            href = a["href"]
            if _JUMP_RE.search(href) or href.startswith("#"):
                continue
            url = clean_url(href, base)
            if url and urlsplit(url).path not in ("", "/") and "/p/" in url:
                return url, None
        for a in anchors:
            if _JUMP_RE.search(a["href"]):
                return _jump_url(a["href"], base), None
        return "", None

    def _card_a(self, box: Tag, base: str) -> Optional[Offer]:
        name_a = box.select_one("div.name h2 a[href], div.name a[href], h2 a[href]")
        img_a = box.select_one("a.image[href]")
        title = ""
        for a in (name_a, img_a):
            if a is not None:
                title = norm_ws(a.get("title")) or text_of(a)
                if title:
                    break
        if not title:
            img = box.select_one("img[alt]")
            title = norm_ws(img.get("alt")) if img is not None else ""
        info = box.select_one("a.ak-info[href]")
        url, pid = self._product_link(box, [info, name_a, img_a], base)
        if not title or not url:
            return None
        price_el = (box.select_one("div.top-right a.price") or box.select_one("div.top-right .price")
                    or box.select_one(".visible-xs .price") or box.select_one("a.price")
                    or box.select_one("div.price") or box.select_one(".price"))
        price, old = current_and_old(price_el)
        if not price:
            return None
        seller: Optional[str] = None
        shop_count: Optional[int] = None
        num_el = box.select_one("a.offer-num, span.offer-num, .offer-num")
        num_txt = text_of(num_el)
        if num_txt:
            shop_count = count_in(num_txt)
            if shop_count is None:
                # single-shop product: the label is the shop name / domain and links out to the shop
                outbound = bool(_JUMP_RE.search(num_el.get("href") or ""))
                looks_shop = (len(num_txt) <= 60 and not re.fullmatch(r"[\d\s]+", num_txt)
                              and not _GENERIC_LABEL_RE.search(num_txt))
                if looks_shop:
                    seller = num_txt
                if looks_shop or outbound:
                    shop_count = 1
        if not pid:
            akpid = str(box.get("data-akpid") or "").strip().lstrip("p")
            if akpid.isdigit():
                pid = akpid
            else:
                ref = box.select_one("[data-dl-si-ref]")
                m = re.search(r"dl-si-(\d+)", ref.get("data-dl-si-ref", "")) if ref is not None else None
                pid = m.group(1) if m else None
        if not pid and _JUMP_RE.search(url):
            # Offer.key falls back to canonical_url(), which drops the query: every Jump.php card would collide
            m = re.search(r"(?:ProductAttributeId|ProductId)=([\w-]+)", url, re.I)
            pid = f"pa{m.group(1)}" if m else "t-" + slugify(title)[:90]
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old, in_stock=None, offer_id=pid,
            image=image_of(box, base, "div.image-link-container img, a.image img, img"),
            seller=seller, shop_count=shop_count, source="compari-legacy",
        )

    def _card_b(self, card: Tag, base: str) -> Optional[Offer]:
        a = (card.select_one('[data-testid="product-title-link"][href]') or card.select_one("a.c-product__link[href]")
             or card.select_one('a[href*="/p/"]'))
        title_el = card.select_one(".c-product__title, h3, h2")
        title = text_of(a) or (a.get("title") if a is not None else "") or text_of(title_el)
        url, pid = self._product_link(card, [a, card.select_one('[data-testid="product-image-link"][href]')], base)
        if not title or not url:
            return None
        price_el = (card.select_one('[data-testid="product-price"] .c-product__price')
                    or card.select_one('[data-testid="product-price"]') or card.select_one(".c-product__price"))
        # struck / "old" children of the price box are the previous price, never the current one
        price, old = current_and_old(price_el)
        if not price:
            return None
        if old is None:
            for el in card.select("del, s, [class*='price--old'], [class*='price-old'], [class*='old-price'], "
                                  "[data-testid='product-old-price']"):
                v = el_price(el)
                if v and v > price:
                    old = v
                    break
        rest = text_of(card).replace(title, " ")
        shop_count = count_in(rest)
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old, in_stock=None, offer_id=pid,
            image=image_of(card, base, "img.c-product__image, img"), shop_count=shop_count, source="compari-heureka",
        )

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        html, soup, final = self.get_html(url)
        o = self.parse_product_page(soup, url)
        if o is None:
            o = extract_product_page(html, url, self.key)   # JSON-LD Product + AggregateOffer
            if o is not None:
                o.url = url
                o.offer_id = compari_pid(url) or o.offer_id
                o.source = "compari-jsonld"
        if o is None:
            raise_if_challenge(html, soup, final)
            return None
        o.store_name = self.name
        return o

    def parse_product_page(self, soup: BeautifulSoup, url: str) -> Optional[Offer]:
        rows = (outermost(soup.select("#offer-list .optoffer")) or outermost(soup.select(".optoffer"))
                or [r for r in soup.select('[itemprop="offers"]')
                    if "aggregateoffer" not in (r.get("itemtype") or "").lower()])
        if any("device-desktop" in (r.get("class") or []) for r in rows):
            # every offer is rendered twice (desktop + mobile row); the mobile copy may label the shop differently
            # ("Flanco" vs logo alt "flanco.ro"), so (shop, price) dedupe alone would count it as another shop
            rows = [r for r in rows if "device-mobile" not in (r.get("class") or [])]
        offers: list[tuple[float, str, Optional[bool], str]] = []   # price, shop, stock, offer title
        seen: set[tuple[str, float]] = set()
        for r in rows:
            price = el_price(r.select_one('[itemprop="price"]'))
            if not price:
                price = el_price(r.select_one(".row-price span") or r.select_one("[data-akjl='Price||Price||1']")
                                 or r.select_one(".row-price"))
            if not price:
                continue
            shop = (text_of(r.select_one('[itemprop="seller"] [itemprop="name"]'))
                    or text_of(r.select_one(".shopname [itemprop='name']")) or text_of(r.select_one(".shopname")))
            if not shop:
                logo = r.select_one(".col-logo img[alt]")
                alt = (logo.get("alt") or "").strip() if logo is not None else ""
                shop = alt.split()[0] if alt else ""
            k = (shop.lower(), round(price, 2))
            if k in seen:
                continue
            seen.add(k)
            stock_txt = text_of(r.select_one("span.delivery-time, .delivery-time")) or text_of(r)
            name = text_of(r.select_one(".offer-name h4, h4[data-akjl], .offer-name"))
            offers.append((price, shop, _stock(stock_txt), name))
        h1 = soup.select_one("div.product-details h1")
        title = text_of(h1)
        if not title:
            spans = soup.select("h1.hidden-xs span[itemprop='name']")
            title = " ".join(text_of(s) for s in spans).strip() or text_of(soup.find("h1"))
        if not offers:
            head = soup.select_one("div.product-details .price")
            price = el_price(head)
            if not price or not title:
                return None
            return Offer(store=self.key, title=title, url=url, price=price, offer_id=compari_pid(url),
                         shop_count=count_in(text_of(soup.select_one("div.product-details"))),
                         source="compari-pdp")
        available = [o for o in offers if o[2] is not False] or offers
        price, shop, stock, name = min(available, key=lambda o: o[0])
        shops = {o[1].lower() for o in offers if o[1]}
        og = soup.find("meta", attrs={"property": "og:image"})
        return Offer(
            store=self.key, title=title or name, url=url, price=price,
            in_stock=stock, offer_id=compari_pid(url), image=og.get("content") if og is not None else None,
            seller=shop or None, shop_count=len(shops) or len(offers), source="compari-pdp",
        )
