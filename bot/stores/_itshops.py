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
* `landed_on_home`  a search request redirected to the bare site root -> home-page promotions, not results.
* `ShopMixin`       page 1 builds the search URL, page N>1 follows the next link remembered from page N-1;
                    URLs of generic-fallback offers are cleaned like the card parsers' ones.
"""
from __future__ import annotations

import copy
import re
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ..net import BlockedError
from ..prices import parse_price

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


_PERCENT_ONLY = re.compile(r"^[-–−]?\s*\d+(?:[.,]\d+)?\s*%$")


def _is_noise(child: Tag) -> bool:
    """Sub-element of a price box that is not the price: instalment / saving / shipping line, or a "-12%" badge.
    Only the innermost such element counts, so a wrapper that also holds the real price is kept."""
    t = text_of(child)
    if not t:
        return False
    if _PERCENT_ONLY.match(t):
        return True
    if not NOT_PRICE_CONTEXT.search(t):
        return False
    return not any(NOT_PRICE_CONTEXT.search(text_of(g)) for g in child.find_all(True))


def _strip_old(el: Tag) -> Tag:
    el = copy.copy(el)
    drop = [child for child in el.find_all(True) if is_old_el(child) or _is_noise(child)]
    for child in drop:       # collected first: nested elements leave together with their parent
        child.extract()
    return el


_GLUE_RE = re.compile(r"(\d)\s*([.,])\s*(\d)")
_PRICE_ATTRS = ("data-price-amount", "data-price", "content", "data-value")


def price_text(el) -> str:
    """Visible text of a price element; <sup> decimals joined whether the separator sits inside the <sup>
    ("1.299<sup>,99</sup>"), before it ("1.299,<sup>99</sup>") or nowhere ("1.299<sup>99</sup>")."""
    if el is None:
        return ""
    el = copy.copy(el)
    for sup in el.find_all("sup"):
        t = sup.get_text("", strip=True)
        d = t.lstrip(",.").strip()
        prev = sup.previous_sibling
        ptxt = str(prev) if isinstance(prev, NavigableString) else (prev.get_text() if isinstance(prev, Tag) else "")
        if d.isdigit() and len(d) <= 2:
            sup.replace_with(("" if ptxt.rstrip().endswith((",", ".")) else ",") + d)
        else:
            sup.replace_with(" " + t)
    return _GLUE_RE.sub(r"\1\2\3", norm(el.get_text(" ", strip=True)))


_THOUSANDS_ONLY = re.compile(r"^\s*\d{1,3}\.\d{3}\s*$")


def el_price(el) -> Optional[float]:
    """Price of an element: machine attributes first (plain decimals), then visible text.
    An attribute like content="1.016" is ambiguous (1.016 vs 1016); when the visible text of the same element
    says 1016 the text wins (CEL renders <span class="price" content="...">1016</span>)."""
    if el is None or not isinstance(el, Tag):
        return None
    for attr in _PRICE_ATTRS:
        v = el.get(attr)
        if v:
            p = num_attr(v)
            if p:
                if _THOUSANDS_ONLY.match(str(v)):
                    t = parse_price(price_text(el))
                    if t and abs(t - p * 1000) < 1:
                        return t
                return p
    return parse_price(price_text(el))


def current_price(el) -> Optional[float]:
    """Price of `el` (attributes, <sup> decimals) ignoring struck/old children and instalment / saving /
    percentage sub-elements ("sau 125 Lei / luna", "Economisesti 500 RON", "-12%")."""
    if el is None or not isinstance(el, Tag):
        return None
    return el_price(_strip_old(el))


def pick_price(root: Optional[Tag], *selectors: str) -> tuple[Optional[Tag], Optional[float]]:
    """First (element, price) over `selectors` in priority order whose element is not a struck/old price and
    actually yields a price - an empty or struck-only first candidate does not end the search."""
    if root is None:
        return None, None
    for sel in selectors:
        for el in root.select(sel):
            if is_old_el(el) or _old_ancestor(el, root):
                continue
            p = current_price(el)
            if p:
                return el, p
    return None, None


def first_current(root: Optional[Tag], *selectors: str) -> Optional[Tag]:
    """First element matching the selectors (in priority order) that is not a struck/old price."""
    if root is None:
        return None
    for sel in selectors:
        for el in root.select(sel):
            if not is_old_el(el) and not _old_ancestor(el, root):
                return el
    return None


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
    """Highest list/struck price inside `card` that is greater than `price`.
    `exclude` = the current-price element(s): an old-marked element that IS one or CONTAINS one is skipped, but a
    struck price nested inside the current-price element ("<p class=price><s>3.599</s> 3.299 RON</p>") counts."""
    if card is None or not price:
        return None
    excl = [e for e in exclude if e is not None]
    best: Optional[float] = None
    for el in card.find_all(True):
        if not is_old_el(el):
            continue
        if any(within(e, el) for e in excl):
            continue
        if _old_ancestor(el, card):
            continue  # nested inside an old element already handled
        ctx = text_of(el)
        if not ctx or NOT_PRICE_CONTEXT.search(ctx):
            continue
        v = el_price(el)
        if v and v > price and v < price * 20 and (best is None or v > best):
            best = v
    return best


def other_higher_price(container: Optional[Tag], price_el: Optional[Tag], price: Optional[float]) -> Optional[float]:
    """A second, higher price in a price box without an "old" class (PRP / list price) - leaf elements only."""
    if container is None or price_el is None or not price:
        return None
    best: Optional[float] = None
    for el in container.find_all(["p", "span", "div", "strong", "b"]):
        if within(el, price_el) or within(price_el, el) or el.find(["p", "span", "div", "strong", "b"]) is not None:
            continue
        t = text_of(el)
        if not t or not re.search(r"\d", t) or NOT_PRICE_CONTEXT.search(t):
            continue
        v = el_price(el)
        if v and price < v < price * 5 and (best is None or v > best):
            best = v
    return best


# --------------------------------------------------------------------------- stock

_OUT_CLS = re.compile(r"out_?of_?stock|out[-_]stoc|no[-_]stoc|epuizat|indisponibil|unavailable|sold[-_]?out", re.I)
_IN_CLS = re.compile(
    r"instock|in[-_]stock|in[-_]stoc|instoc|insupplierstock|supplier|limited[-_]?stock|disponibil", re.I)
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

_NEXT_TEXT = re.compile(
    r"^(?:»|›|>|>>|→|urm[aă]toarea|urm[aă]toare|inainte|înainte|next|pagina\s+urm[aă]toare)$", re.I)
_PAGER_SEL = (
    "ul.pagination, .pagination, .pager, .paginare, [class*='paginat'], nav[role='navigation'], "
    "[role='navigation'], .lr-pagination, .pageresults"
)


def next_page_url(soup: BeautifulSoup, base: str, page: int, extra_selectors: Iterable[str] = ()) -> Optional[str]:
    """URL of page `page + 1` taken from the pager.

    Order: rel=next, then the link whose text is exactly the next page number (unambiguous), then
    '›' / 'Urmatoarea' / class "next" (">>" / "»" are "last page" on some pagers, e.g. CEL's `a.last`, so
    they only count when no numbered link exists), then page-number patterns in hrefs, then `extra_selectors`."""
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
            if text_of(a) == str(nxt):
                u = ok(a.get("href"))
                if u:
                    return u
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
            if num_re.search(a.get("href") or ""):
                u = ok(a.get("href"))
                if u:
                    return u
    for sel in extra_selectors:
        for a in soup.select(sel):
            u = ok(a.get("href"))
            if u:
                return u
    return None


def landed_on_home(requested: str, final: str) -> bool:
    """The search request was redirected to the site root (no query left): the page is the home page, whose
    promotion cards must not be reported as search results."""
    try:
        req, fin = urlsplit(requested), urlsplit(final)
    except ValueError:
        return False
    if fin.path not in ("", "/") or fin.query:
        return False
    return req.path not in ("", "/") or bool(req.query)


class ShopMixin:
    """Mixed in before `Store`:
    * remembers the pager's next link so that page N+1 is fetched only when the shop offers one;
    * generic-fallback offers get the same URL hygiene as the card parsers (no tracking query / fragment)."""

    def fallback(self, html: str, url: str, soup: Optional[BeautifulSoup] = None) -> list:
        offers = super().fallback(html, url, soup)  # type: ignore[misc]
        for o in offers:
            o.url = clean_url(o.url, url) or o.url
        return dedupe(offers)

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
            v = el_price(el)
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
    if stock is None:
        from ..extract import availability_flag

        av = soup.find(attrs={"itemprop": "availability"}) or soup.find(
            "meta", attrs={"property": re.compile(r"^(?:product|og):availability$")})
        if av is not None:
            stock = availability_flag(av.get("href") or av.get("content") or av.get_text())
    old = pdp_old_price(soup, price, old_sels)
    return Offer(store=store.key, title=title, url=url, price=price, old_price=old, in_stock=stock,
                 offer_id=offer_id, store_name=store.name, source=source)


def enrich_pdp(o, soup: Optional[BeautifulSoup], *, title_sels: Iterable[str] = ("h1",),
               stock_sels: Iterable[str] = (), old_sels: Iterable[str] = ()):
    """Fill what the generic product-page extraction leaves weak: the title of a bare "meta" hit (which may be
    the URL or the <title> with the shop name), unknown stock, missing old price."""
    if o is None or soup is None:
        return o
    if o.source == "meta" or not o.title or o.title == o.url:
        t = pdp_title(soup, title_sels)
        if t:
            o.title = t
    if o.in_stock is None:
        for sel in stock_sels:
            o.in_stock = stock_of_el(soup.select_one(sel))
            if o.in_stock is not None:
                break
    if not o.old_price and o.price:
        o.old_price = pdp_old_price(soup, o.price, old_sels)
    return o


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
