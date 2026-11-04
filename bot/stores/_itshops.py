"""Helpers shared by the Romanian IT-shop adapters (PC Garage, ITGalaxy, evoMAG, CEL.ro, Vexio, ForIT).

Not a store module: the leading underscore keeps it out of the registry scan.

* `clean_url`       absolute https URL without query string / fragment (tracking params).
* `num_attr`        price from a machine attribute ("1798.99" -> plain float FIRST; the Romanian rule would
                    turn it into 179899), falls back to `parse_price` for "1.798,99 lei".
* `current_price`   visible price of an element with struck-through / "old" children removed.
* `old_price_in`    highest struck/"old"/"vechi"/"initial"/"prp" price inside a card that is > price.
* `stock_from`      availability from class names (instock / insupplierstock / outofstock / in_stoc /
                    out_stoc / no_stoc ...) and Romanian text ("Stoc epuizat", "in stoc furnizor" ...).
* `raise_if_challenge`  Cloudflare managed challenge / block page served with HTTP 200 -> BlockedError.
                    The bare `/cdn-cgi/challenge-platform/scripts/jsd/main.js` loader is NOT a block
                    (PC Garage and Flanco load it on every normal page), nor is an in-page Turnstile widget.
* `next_page_url`   the pager's "next" link (search pagination is undocumented for these shops, so it is
                    never built from a template).
* `PagerMixin`      page 1 builds the search URL, page N>1 follows the next link remembered from page N-1.
"""
from __future__ import annotations

import copy
import re
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..net import BlockedError
from ..prices import element_price, parse_price

# --------------------------------------------------------------------------- urls / text


def clean_url(href: Optional[str], base: str) -> str:
    """Absolute https URL without query/fragment; "" for javascript:/mailto:/# links."""
    h = (href or "").strip()
    if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    p = urlsplit(urljoin(base, h))
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return urlunsplit(("https", p.netloc.lower(), p.path or "/", "", ""))


def norm(s: Optional[str]) -> str:
    return " ".join(str(s or "").split())


def text_of(el) -> str:
    return norm(el.get_text(" ", strip=True)) if el is not None else ""


def longest(*cands: Optional[str]) -> str:
    """Longest non-empty candidate (visible names vs. shortened title attributes)."""
    best = ""
    for c in cands:
        c = norm(c)
        if len(c) > len(best):
            best = c
    return best


_PLACEHOLDER_IMG = re.compile(r"(placeholder|blank|spacer|lazy|loading|no[-_]?image|1x1)\.(gif|png|svg|jpg)", re.I)


def image_of(card: Optional[Tag], base: str, selector: str = "img") -> Optional[str]:
    if card is None:
        return None
    for img in card.select(selector):
        for attr in ("data-src", "data-original", "data-lazy", "data-lazy-src", "data-srcset", "src"):
            v = (img.get(attr) or "").strip()
            if attr == "data-srcset" and v:
                v = v.split(",")[0].strip().split(" ")[0]
            if not v or v.startswith("data:") or _PLACEHOLDER_IMG.search(v):
                continue
            u = urljoin(base, v)
            if u.startswith("http"):
                return u
    return None


# --------------------------------------------------------------------------- prices

_PLAIN_NUM = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")
# instalments, savings, shipping, loyalty points ... never the product price
NOT_PRICE_CONTEXT = re.compile(
    r"(/\s*lun|\bluna\b|lunar|\brate\b|\brat[aă]\b|economis|reducere|transport|livrare|cashback|voucher|"
    r"puncte|garan|discount)",
    re.I,
)
# class tokens that mark a list / struck price.  Token-anchored so "fw-bold" is not "old".
_OLD_TOKEN = re.compile(
    r"(?:^|[-_])(?:old|vechi|initial|prp|rrp|strike|striked|crossed|through)(?:$|[-_])|oldprice|pretvechi",
    re.I,
)


def num_attr(value) -> Optional[float]:
    """Machine value ("1798.99", 1798.99) -> float first; human text -> Romanian parse."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value).strip()
    if _PLAIN_NUM.match(s):
        try:
            v = float(s)
        except ValueError:
            return None
        return v if v > 0 else None
    return parse_price(s)


def is_old_el(el) -> bool:
    if not isinstance(el, Tag):
        return False
    if el.name in ("del", "s", "strike"):
        return True
    return any(_OLD_TOKEN.search(c) for c in (el.get("class") or []))


def _strip_old(el: Tag) -> Tag:
    el = copy.copy(el)
    for child in el.find_all(True):
        if is_old_el(child):
            child.decompose()
    return el


def current_price(el) -> Optional[float]:
    """Price of `el` (attributes, <sup> decimals) ignoring struck/old children."""
    if el is None:
        return None
    return element_price(_strip_old(el))


def within(el, anc) -> bool:
    """`el` is `anc` or a descendant of it (identity, not bs4's structural ==)."""
    if el is anc:
        return True
    return any(p is anc for p in el.parents)


def _old_ancestor(el: Tag, stop: Tag) -> bool:
    for p in el.parents:
        if p is stop:
            return False
        if is_old_el(p):
            return True
    return False


def old_price_in(card: Optional[Tag], price: Optional[float], exclude: Iterable[Tag] = ()) -> Optional[float]:
    """Highest list/struck price inside `card` that is greater than `price`."""
    if card is None or not price:
        return None
    excl = [e for e in exclude if e is not None]
    best: Optional[float] = None
    for el in card.find_all(True):
        if not is_old_el(el):
            continue
        if any(within(el, e) or within(e, el) for e in excl):
            continue
        if _old_ancestor(el, card):
            continue  # nested inside an old element already handled
        ctx = text_of(el)
        if not ctx or NOT_PRICE_CONTEXT.search(ctx):
            continue
        v = element_price(el)
        if v and v > price and v < price * 20 and (best is None or v > best):
            best = v
    return best


# --------------------------------------------------------------------------- stock

_OUT_CLS = re.compile(r"out_?of_?stock|out[-_]stoc|no[-_]stoc|epuizat|indisponibil|unavailable|sold[-_]?out", re.I)
_IN_CLS = re.compile(r"instock|in[-_]stock|in[-_]stoc|instoc|insupplierstock|supplier|limited[-_]?stock|disponibil", re.I)
_OUT_TXT = re.compile(
    r"stoc\s+epuizat|\bepuizat|indisponibil|nu\s+(?:este|e)\s+(?:[iî]n\s+)?stoc|nu\s+este\s+disponibil|"
    r"f[aă]r[aă]\s+stoc|out\s+of\s+stock|stoc\s*:\s*0\b",
    re.I,
)
_IN_TXT = re.compile(
    r"[iî]n\s+stoc|stoc\s+(?:magazin|limitat|suficient|furnizor|depozit)|ultimele\s+buc|ultimul\s+produs|"
    r"\bdisponibil\b|la\s+comand[aă]|precomand[aă]|livrare\s+in\s+\d",
    re.I,
)


def stock_from(classes: Iterable[str] = (), text: str = "") -> Optional[bool]:
    cls = " ".join(classes or ()).lower()
    if cls:
        # "disponibil" inside "indisponibil" is caught by the out-check running first
        if _OUT_CLS.search(cls):
            return False
        if _IN_CLS.search(cls):
            return True
    t = norm(text)
    if t:
        if _OUT_TXT.search(t):
            return False
        if _IN_TXT.search(t):
            return True
    return None


def stock_of_el(el) -> Optional[bool]:
    if el is None:
        return None
    return stock_from(el.get("class") or [], (el.get("aria-label") or "") + " " + text_of(el))


# --------------------------------------------------------------------------- block pages

_CF_MANAGED = re.compile(r"_cf_chl_opt|/cdn-cgi/challenge-platform/h/[a-z]+/orchestrate/", re.I)
_BLOCK_TITLES = (
    "just a moment", "attention required", "access denied", "checking your browser", "ddos-guard",
    "are you a robot", "verify you are human", "captcha", "security check", "request blocked",
)


def is_challenge(html: str, soup: Optional[BeautifulSoup] = None) -> bool:
    low = (html or "").lower()
    if "just a moment" in low[:2000] or _CF_MANAGED.search(low[:400_000]):
        return True
    title = ""
    if soup is not None and soup.title is not None:
        title = soup.title.get_text(" ", strip=True).lower()
    return any(t in title for t in _BLOCK_TITLES)


def raise_if_challenge(html: str, soup: Optional[BeautifulSoup], url: str) -> None:
    if is_challenge(html, soup):
        title = soup.title.get_text(" ", strip=True)[:60] if soup is not None and soup.title else ""
        raise BlockedError(f"challenge page (HTTP 200) {title!r}", 200, url)


# --------------------------------------------------------------------------- pagination

_NEXT_TEXT = re.compile(r"^(?:»|›|>|>>|→|urm[aă]toarea|urm[aă]toare|inainte|înainte|next|pagina\s+urm[aă]toare)$", re.I)
_PAGER_SEL = (
    "ul.pagination, .pagination, .pager, .paginare, [class*='paginat'], nav[role='navigation'], "
    "[role='navigation'], .lr-pagination"
)


def next_page_url(soup: BeautifulSoup, base: str, page: int, extra_selectors: Iterable[str] = ()) -> Optional[str]:
    """URL of page `page + 1` taken from the pager (rel=next, '»' / 'Urmatoarea', or page-number links)."""
    if soup is None:
        return None
    def ok(href: Optional[str]) -> Optional[str]:
        h = (href or "").strip()
        if not h or h.startswith(("#", "javascript:")):
            return None
        u = urljoin(base, h)
        if not u.startswith("http") or u.rstrip("/") == base.rstrip("/"):
            return None
        return u

    for el in soup.select("link[rel~='next'][href], a[rel~='next'][href]"):
        u = ok(el.get("href"))
        if u:
            return u
    nxt = page + 1
    num_re = re.compile(
        rf"(?:pagina[:/=-]?{nxt}(?!\d)|[?&](?:p|page|pg|pagina|sn\.p)={nxt}(?!\d)|/p{nxt}(?:/|$)|/page/{nxt}(?!\d))",
        re.I,
    )
    pagers = soup.select(_PAGER_SEL)
    for pager in pagers:
        for a in pager.select("a[href]"):
            txt = text_of(a)
            cls = " ".join(a.get("class") or []).lower()
            title = (a.get("title") or a.get("aria-label") or "").strip()
            if _NEXT_TEXT.match(txt) or "next" in cls or _NEXT_TEXT.match(title):
                u = ok(a.get("href"))
                if u:
                    return u
        for a in pager.select("a[href]"):
            if text_of(a) == str(nxt) or num_re.search(a.get("href") or ""):
                u = ok(a.get("href"))
                if u:
                    return u
    for sel in extra_selectors:
        for a in soup.select(sel):
            u = ok(a.get("href"))
            if u:
                return u
    return None


class PagerMixin:
    """Remembers the pager's next link so that page N+1 is fetched only when the shop offers one."""

    def _pager_reset(self) -> None:
        self._next_urls: dict[int, str] = {}

    def _pager_get(self, page: int) -> Optional[str]:
        return getattr(self, "_next_urls", {}).get(page)

    def _pager_set(self, page: int, url: Optional[str]) -> None:
        if not hasattr(self, "_next_urls"):
            self._next_urls = {}
        if url:
            self._next_urls[page] = url


# --------------------------------------------------------------------------- product pages


def pdp_title(soup: BeautifulSoup, selectors: Iterable[str] = ("h1",)) -> str:
    for sel in selectors:
        t = text_of(soup.select_one(sel))
        if t:
            return t
    og = soup.find("meta", attrs={"property": "og:title"})
    if og is not None and og.get("content"):
        return norm(og["content"])
    return text_of(soup.title) if soup.title is not None else ""


def pdp_old_price(soup: BeautifulSoup, price: float, selectors: Iterable[str]) -> Optional[float]:
    best: Optional[float] = None
    for sel in selectors:
        for el in soup.select(sel):
            if NOT_PRICE_CONTEXT.search(text_of(el)):
                continue
            v = element_price(el)
            if v and price < v < price * 20 and (best is None or v > best):
                best = v
    return best


def pdp_offer(store, soup: BeautifulSoup, url: str, *, price_sels: Iterable[str], old_sels: Iterable[str] = (),
              stock_sels: Iterable[str] = (), title_sels: Iterable[str] = ("h1",), offer_id: Optional[str] = None,
              source: str = "pdp"):
    """Single product page from store-specific selectors (used when JSON-LD/microdata found nothing)."""
    from ..models import Offer

    price: Optional[float] = None
    for sel in price_sels:
        for el in soup.select(sel):
            price = num_attr(el.get("content")) if el.get("content") else current_price(el)
            if price:
                break
        if price:
            break
    title = pdp_title(soup, title_sels)
    if not price or not title:
        return None
    stock: Optional[bool] = None
    for sel in stock_sels:
        stock = stock_of_el(soup.select_one(sel))
        if stock is not None:
            break
    old = pdp_old_price(soup, price, old_sels)
    return Offer(store=store.key, title=title, url=url, price=price, old_price=old, in_stock=stock,
                 offer_id=offer_id, store_name=store.name, source=source)


# --------------------------------------------------------------------------- misc


def dedupe(offers) -> list:
    seen: set[str] = set()
    out = []
    for o in offers:
        if o is None or o.key in seen:
            continue
        seen.add(o.key)
        out.append(o)
    return out


def outermost(cards: list[Tag]) -> list[Tag]:
    """Drop cards nested inside another selected card (same product rendered in nested wrappers)."""
    ids = {id(c) for c in cards}
    return [c for c in cards if not any(id(p) in ids for p in c.parents)]
