"""Helpers shared by the price-comparison adapters (compari, shopmania, pricero).

Not a store module: the leading underscore keeps it out of the registry scan.

* `clean_url`        absolute https URL without query string / fragment (tracking params).
* `slugify`          lowercase, diacritics stripped (NFKD), every run of non-[a-z0-9] -> "-".
* `first_price`      price text of a comparator card: "de la 1 989,00 RON", "476,80 RON", "de la 5.920 lei",
                     and ranges "1 299 – 1 499 lei" (-> the LOWER bound, `parse_price` alone would take the upper).
* `el_price`         machine attribute (itemprop=price content="1989.00", data-price ...) first, then visible text.
* `count_in`         "12 oferte" / "14 magazine" / "Vezi 37 oferte" -> 12 / 14 / 37.
* `raise_if_challenge`  Cloudflare managed challenge / captcha page served with HTTP 200 -> BlockedError.
                     Only called when site-specific parsing found nothing, so a passive challenge-platform
                     script on a normal page is never mistaken for a block.
* `NextPager`        page 1 builds the search URL, page N>1 follows the "next" link remembered from page N-1
                     (comparator search pagination is undocumented, so it is never built from a template).
"""
from __future__ import annotations

import copy
import re
import unicodedata
from typing import Iterable, Optional, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..models import Offer
from ..net import BlockedError
from ..prices import element_price_text, parse_price

# --------------------------------------------------------------------------- urls / text


def clean_url(href: Optional[str], base: str) -> str:
    """Absolute https URL without query/fragment; "" for javascript:/mailto:/# links."""
    h = (href or "").strip()
    if not h or h.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return ""
    p = urlsplit(urljoin(base, h))
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return urlunsplit(("https", p.netloc.lower(), p.path or "/", "", ""))


def norm_ws(s: Optional[str]) -> str:
    return " ".join(str(s or "").split())


def text_of(el) -> str:
    return norm_ws(el.get_text(" ", strip=True)) if el is not None else ""


def longest(*cands: Optional[str]) -> str:
    best = ""
    for c in cands:
        c = norm_ws(c)
        if len(c) > len(best):
            best = c
    return best


def strip_diacritics(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def slugify(text: str) -> str:
    """'Căști Sony WH-1000XM5 (negru)' -> 'casti-sony-wh-1000xm5-negru'."""
    s = strip_diacritics(str(text or "")).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# --------------------------------------------------------------------------- prices / counts

# "5.920 lei", "1 989,00 RON", "3149,99 lei" inside free card text. Unlike prices.PRICE_WITH_CURRENCY_RE the
# lookbehind also rejects letters, so a model number's last digit is not glued on ("S24 999 lei" -> 999).
PRICE_RE = re.compile(r"(?<![\w.,])((?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?)\s*(?:lei|ron)\b", re.I)

_RANGE_SPLIT_RE = re.compile(r"\s*(?:[–—]|\s-\s)\s*")
_DE_LA_RE = re.compile(r"^\s*(?:de\s+la|from)\s*", re.I)


def first_price(text: Optional[str]) -> Optional[float]:
    """Lowest price in a comparator price text (handles 'de la' and 'min – max' ranges)."""
    s = norm_ws(str(text or "").replace("\u00a0", " ").replace("\u202f", " "))
    if not s or not re.search(r"\d", s):
        return None
    s = _DE_LA_RE.sub("", s)
    parts = [p for p in _RANGE_SPLIT_RE.split(s) if re.search(r"\d", p)]
    if len(parts) > 1:
        lo = parse_price(parts[0])
        if lo:
            return lo
    return parse_price(s)


_MACHINE_ATTRS = ("content", "data-price", "data-price-amount", "data-value")


def el_price(el) -> Optional[float]:
    """Price of an element: machine attribute first ("1989.00"), then its visible text."""
    if el is None or not isinstance(el, Tag):
        return None
    for attr in _MACHINE_ATTRS:
        v = el.get(attr)
        if v and re.fullmatch(r"\s*\d+(?:[.,]\d{1,2})?\s*", str(v)):
            p = parse_price(str(v).replace(",", "."))
            if p:
                return p
    return first_price(element_price_text(el))


_OLD_TOKEN_RE = re.compile(r"(?:^|[-_])(?:old|vechi|initial|strike|striked|crossed|before|was|prp|rrp)(?:[-_]|$)", re.I)


def _is_old_node(el: Tag) -> bool:
    if el.name in ("del", "s", "strike"):
        return True
    return any(_OLD_TOKEN_RE.search(c) for c in (el.get("class") or []))


def current_and_old(el) -> tuple[Optional[float], Optional[float]]:
    """(current, old) of a price box whose struck / "old" children hold the previous price."""
    if el is None or not isinstance(el, Tag):
        return None, None
    box = copy.copy(el)
    olds: list[float] = []
    old_nodes = outermost(x for x in box.find_all(True) if _is_old_node(x))
    for x in old_nodes:
        v = first_price(element_price_text(x))
        if v:
            olds.append(v)
        x.extract()
    cur = el_price(box)
    old = max(olds) if olds else None
    return cur, (old if cur and old and old > cur else None)


# the lookbehind keeps a model number's last digit out ("Galaxy S24 12 oferte" -> 12, not 2412)
_COUNT_RE = re.compile(r"(?<![\w.,])(\d{1,5})\s*(?:de\s+)?(?:oferte|ofert[aă]|magazine|magazin)", re.I)


def count_in(text: Optional[str]) -> Optional[int]:
    m = _COUNT_RE.search(norm_ws(text))
    if not m:
        return None
    try:
        n = int(re.sub(r"\D", "", m.group(1)))
    except ValueError:
        return None
    return n if 0 < n < 100000 else None


# compari's lazy loader ("/img/lazy.gif" in src, real image in data-lazy-src), shopmania's "na.svg"
_PLACEHOLDER_IMG = re.compile(
    r"(placeholder|blank|spacer|loading|no[-_]?image|1x1|/na\.svg|/lazy[\w-]*\.(?:gif|png|svg)(?:$|\?))", re.I)


def image_of(card: Optional[Tag], base: str, selector: str = "img") -> Optional[str]:
    if card is None:
        return None
    for img in card.select(selector):
        for attr in ("data-lazy-src", "data-src", "data-original", "data-srcset", "srcset", "src"):
            v = (img.get(attr) or "").strip()
            if attr.endswith("srcset") and v:
                v = v.split(",")[0].strip().split(" ")[0]
            if not v or v.startswith("data:") or _PLACEHOLDER_IMG.search(v):
                continue
            u = urljoin(base, v)
            if u.startswith("http"):
                return u
    return None


def outermost(tags: Iterable[Tag]) -> list[Tag]:
    """Drop matches nested inside another match (a selector matching wrapper and inner card)."""
    tags = list(tags)
    ids = {id(t) for t in tags}
    return [t for t in tags if not any(id(p) in ids for p in t.parents)]


def dedupe(offers: Iterable[Optional[Offer]]) -> list[Offer]:
    seen: set[str] = set()
    out: list[Offer] = []
    for o in offers:
        if o is None or o.key in seen:
            continue
        seen.add(o.key)
        out.append(o)
    return out


# --------------------------------------------------------------------------- cards from product links

# text that holds a price which is NOT the product price (shipping, monthly instalments)
_NOISE_PRICE_RE = re.compile(
    r"(?:cost\s+)?(?:livrare|transport)\w*(?![^\d]{0,12}gratu)[^\d]{0,25}\d[\d.\s]*(?:,\d{1,2})?\s*(?:ron|lei)"
    r"|\brat[eaă]\w*(?:\s+\w+){0,2}?\s+de\s+la\s*\d[\d.\s]*(?:,\d{1,2})?\s*(?:ron|lei)"    # "Rate de la 247 lei"
    r"|\d[\d.\s]*(?:,\d{1,2})?\s*(?:ron|lei)\s*(?:/|pe)\s*lun\w*",                       # "247 lei/lună"
    re.I,
)


def strip_noise(text: Optional[str]) -> str:
    """Card / page text with shipping costs and monthly instalments blanked out."""
    return _NOISE_PRICE_RE.sub(" ", text or "")


def _ids_under(node: Tag, base: str, id_re: re.Pattern) -> set[str]:
    ids: set[str] = set()
    for a in node.find_all("a", href=True):
        m = id_re.search(urlsplit(urljoin(base, a["href"])).path)
        if m:
            ids.add(m.group(1))
    return ids


def _single_product_container(a: Tag, pid: str, base: str, id_re: re.Pattern,
                              price_res: Sequence[re.Pattern]) -> Optional[Tag]:
    """Outermost ancestor of `a` that holds a price and links to no OTHER product."""
    node, found = a, None
    for _ in range(8):
        parent = node.parent
        if not isinstance(parent, Tag) or parent.name in ("body", "html", "[document]"):
            break
        if _ids_under(parent, base, id_re) - {pid}:
            break
        node = parent
        txt = strip_noise(text_of(node))
        if any(r.search(txt) for r in price_res):
            found = node
    return found


def _clean_title(s: Optional[str]) -> str:
    s = PRICE_RE.sub(" ", norm_ws(s))
    s = re.sub(r"\b(?:de la|vezi|compar[aă]|\d+\s+(?:oferte|magazine))\b", " ", s, flags=re.I)
    return norm_ws(s).strip(" -|·•")


def link_cards(soup: BeautifulSoup, base: str, store: str, id_re: re.Pattern,
               price_res: Sequence[re.Pattern] = (PRICE_RE,), source: str = "links") -> list[Offer]:
    """Product cards rebuilt from product-page links (path matching `id_re`, group 1 = product id) and the
    nearest price text (first regex in `price_res` that matches; group 1 = the number)."""
    host = urlsplit(base).netloc.lower().removeprefix("www.")
    groups: dict[str, list[tuple[Tag, str]]] = {}
    for a in soup.find_all("a", href=True):
        url = clean_url(a["href"], base)
        if not url or not urlsplit(url).netloc.removeprefix("www.").endswith(host):
            continue
        m = id_re.search(urlsplit(url).path)
        if m:
            groups.setdefault(m.group(1), []).append((a, url))
    out: list[Offer] = []
    for pid, anchors in groups.items():
        titles = []
        for a, _ in anchors:
            img = a.find("img", alt=True)
            titles += [a.get("title"), _clean_title(text_of(a)), img.get("alt") if img is not None else None]
        title = longest(*titles)
        if len(title) < 3:
            continue
        container = _single_product_container(anchors[0][0], pid, base, id_re, price_res)
        if container is None:
            continue
        txt = strip_noise(text_of(container))
        price = None
        for r in price_res:
            m = r.search(txt)
            if m:
                price = parse_price(m.group(1))
                if price:
                    break
        if not price:
            continue
        stock: Optional[bool] = None
        low = txt.lower()
        if any(x in low for x in ("stoc epuizat", "indisponibil", "nu este în stoc", "nu este in stoc")):
            stock = False
        out.append(Offer(
            store=store, title=title, url=anchors[0][1], price=price, in_stock=stock, offer_id=pid,
            image=image_of(container, base), shop_count=count_in(txt.replace(title, " ")), source=source,
        ))
    return dedupe(out)


# --------------------------------------------------------------------------- block detection

_CHALLENGE_TITLE_START = (
    "just a moment", "un moment", "attention required", "checking your browser", "access denied",
    "security check", "verificare de securitate", "one more step",
)
_CHALLENGE_TITLE_ANY = ("verify you are human", "captcha")
# Only markers of the challenge page itself. NOT the bare "/cdn-cgi/challenge-platform/" path: Cloudflare injects
# its passive bot-detection script (/cdn-cgi/challenge-platform/h/b/scripts/jsd/.../main.js) into normal pages too,
# so a real result page we fail to parse would be reported as "blocked" instead of reaching the generic fallback.
_CHALLENGE_MARKERS = (
    "_cf_chl_opt", "/orchestrate/chl_page/", "/orchestrate/managed/", "/orchestrate/jsch/", "cf-chl-widget",
    "id=\"challenge-form\"", "enable javascript and cookies to continue", "cf-browser-verification",
    "captcha-delivery.com", "px-captcha",
)


def raise_if_challenge(html: str, soup: Optional[BeautifulSoup], url: str) -> None:
    title = ""
    if soup is not None and soup.title is not None:
        title = soup.title.get_text(" ", strip=True).lower()
    if title.startswith(_CHALLENGE_TITLE_START) or any(t in title for t in _CHALLENGE_TITLE_ANY):
        raise BlockedError(f"challenge page: {title[:60]}", 200, url)
    low = (html or "")[:200_000].lower()
    if any(m in low for m in _CHALLENGE_MARKERS) and len(low) < 120_000:
        raise BlockedError("challenge page (anti-bot markers)", 200, url)


# --------------------------------------------------------------------------- pagination

_NEXT_TEXT = {"»", "›", ">", "următoarea", "urmatoarea", "pagina următoare", "pagina urmatoare", "înainte",
              "inainte", "next"}


def next_page_url(soup: BeautifulSoup, base: str) -> Optional[str]:
    cands: list[str] = []
    for el in soup.select('link[rel~="next"][href], a[rel~="next"][href]'):
        cands.append(el["href"])
    for a in soup.select(".pagination a[href], .pager a[href], nav a[href], [class*='paginat'] a[href]"):
        cls = " ".join(a.get("class") or []).lower()
        txt = text_of(a).lower()
        if "next" in cls or "urm" in cls or txt in _NEXT_TEXT or (a.get("aria-label") or "").lower() in _NEXT_TEXT:
            cands.append(a["href"])
    here = urlsplit(base)
    for href in cands:
        u = urljoin(base, href.strip())
        p = urlsplit(u)
        if p.scheme in ("http", "https") and p.netloc.lower().endswith(here.netloc.lower().removeprefix("www.")) \
                and u.rstrip("/") != base.rstrip("/"):
            return u
    return None


class NextPager:
    """Mixin: remembers the next-page link per query between search_page() calls."""

    def _pager_reset(self) -> None:
        self._next_pages: dict[int, str] = {}

    def _pager_set(self, page: int, url: Optional[str]) -> None:
        if not hasattr(self, "_next_pages"):
            self._next_pages = {}
        if url:
            self._next_pages[page] = url

    def _pager_get(self, page: int) -> Optional[str]:
        return getattr(self, "_next_pages", {}).get(page)
