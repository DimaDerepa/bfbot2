"""Helpers shared by the fashion / beauty adapters (Fashion Days, Zalando, Notino, Sephora, elefant, Answear,
Decathlon, Douglas, ABOUT YOU, Sinsay / Reserved).

Not a store module: the leading underscore keeps it out of the registry scan.

* `clean_url`       absolute https URL without tracking query / fragment (`keep` names query keys that select
                    a product variant, e.g. Decathlon's `mc`).
* `join_title`      "brand + product name" without repeating the brand when the name already carries it
                    (users match fashion offers on brand / model words: "Nike Air Max 90 - Pantofi sport").
* `num`             machine price ("269.99", 1299.5) -> float first; Romanian text ("1.299,99 lei") otherwise.
* `money_values`    every "NNN lei" amount in a text, in order.
* `price_lines`     current / original (RRP) / lowest-30-days (EU Omnibus) out of a card's price lines.
* `current_price`   visible price of an element with struck/"old" children, unit prices ("12,50 lei / 1 ml"),
                    instalments and "-20%" badges removed.
* `old_price_in`    highest struck / old-marked price inside a card that is > the current price.
* `stock_from_text` "Stoc epuizat" / "indisponibil" -> False, "în stoc" / "ultimele bucăți" -> True, else None.
* `raise_if_challenge`  Cloudflare / Akamai / DataDome block page served with HTTP 200 -> BlockedError
                    (only called when the site-specific parser found nothing on the page).
* `NextPager`       page N>1 follows the "next" link remembered from page N-1 (never built from a template
                    when the shop's pagination is undocumented).
"""
from __future__ import annotations

import copy
import json
import re
import unicodedata
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..net import BlockedError
from ..prices import PRICE_WITH_CURRENCY_RE, element_price_text, parse_price

# --------------------------------------------------------------------------- text / urls


def norm(s: Any) -> str:
    return " ".join(str(s or "").replace(" ", " ").replace(" ", " ").split())


def text_of(el) -> str:
    return norm(el.get_text(" ", strip=True)) if el is not None else ""


def fold(s: Any) -> str:
    """Lowercase, diacritics stripped, whitespace collapsed (for comparisons only)."""
    t = unicodedata.normalize("NFKD", norm(s).lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def slugify(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-")


def _contains_words(hay: str, needle: str) -> bool:
    if not needle:
        return True
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay) is not None


def join_title(*parts: Any, sep: str = " ") -> str:
    """Join brand / name / description, skipping a part that the title already contains (word-wise, case and
    diacritics insensitive) and replacing the title by a part that contains it."""
    out = ""
    for part in parts:
        p = norm(part).strip(" -–|,;")
        if not p:
            continue
        if out and _contains_words(fold(out), fold(p)):
            continue
        if out and _contains_words(fold(p), fold(out)):
            out = p
            continue
        out = f"{out}{sep}{p}" if out else p
    return out


def first_el(root, *selectors: str):
    """First element matching the selectors in PRIORITY order (a CSS selector list returns document order)."""
    if root is None:
        return None
    for sel in selectors:
        el = root.select_one(sel)
        if el is not None:
            return el
    return None


def longest(*cands: Any) -> str:
    best = ""
    for c in cands:
        c = norm(c)
        if len(c) > len(best):
            best = c
    return best


def clean_url(href: Optional[str], base: str, keep: Iterable[str] = ()) -> str:
    """Absolute https URL without query (except `keep` keys) / fragment; "" for javascript:/mailto:/# links."""
    h = (href or "").strip()
    if not h or h.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return ""
    p = urlsplit(urljoin(base, h))
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    q = ""
    keep = tuple(keep)
    if keep and p.query:
        q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k in keep])
    return urlunsplit(("https", p.netloc.lower(), p.path or "/", q, ""))


def landed_on_home(requested: str, final: str) -> bool:
    """A search request redirected to the bare site root (or a locale root like /ro/ro/): home-page
    promotions, not search results."""
    try:
        req, fin = urlsplit(requested), urlsplit(final)
    except ValueError:
        return False
    if fin.query or re.sub(r"^(/[a-z]{2}){0,2}/?$", "", fin.path or "/"):
        return False
    return (req.path or "/").rstrip("/") != (fin.path or "/").rstrip("/") or bool(req.query)


_PLACEHOLDER_IMG = re.compile(r"(placeholder|blank|spacer|lazy|loading|no[-_]?image|1x1|transparent)\.(gif|png|svg|jpe?g|webp)", re.I)


def image_of(card: Optional[Tag], base: str, selector: str = "img") -> Optional[str]:
    if card is None:
        return None
    for img in card.select(selector):
        for attr in ("data-original", "data-src", "data-lazy-src", "data-lazy", "data-srcset", "srcset", "src"):
            v = (img.get(attr) or "").strip()
            if attr in ("data-srcset", "srcset") and v:
                v = v.split(",")[0].strip().split(" ")[0]
            if not v or v.startswith("data:") or _PLACEHOLDER_IMG.search(v):
                continue
            u = urljoin(base, v)
            if u.startswith("//"):
                u = "https:" + u
            if u.startswith("http"):
                return u
    return None


# --------------------------------------------------------------------------- prices

_PLAIN_NUM = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")
UNIT_PRICE_RE = re.compile(
    r"(?:lei|ron)\*?\s*/\s*(?:\d+(?:[.,]\d+)?\s*)?(?:ml|cl|l|litr\w*|g|gr|gram\w*|kg|kilogram\w*|buc|bucat[aăi]|m|oz|"
    r"doz[aăe]|capsul[aăe]|tablet[aăe]|plic(?:uri)?|spal[aă]ri)\b",
    re.I,
)
NOT_PRICE_RE = re.compile(
    r"/\s*lun|\blunar|\bluna\b|\brate\b|\brat[aă]\b|economis|cashback|puncte|livrare|transport|garan|voucher|cupon",
    re.I,
)
_PERCENT_ONLY = re.compile(r"^[-–−]?\s*\d+(?:[.,]\d+)?\s*%$")
# class tokens of a struck / list price; token-anchored so "fw-bold" is not "old"
_OLD_TOKEN = re.compile(
    r"(?:^|[-_])(?:old|vechi|initial|prp|rrp|strike|striked|strikethrough|crossed|through|before|was|original|"
    r"regular|recommended|recomandat)(?:$|[-_])|oldprice|pretvechi|listprice|list-price|price-list|fullprice|"
    r"full-price|^st-price$",
    re.I,
)
_OLD_TESTID = re.compile(r"original|old|previous|strike|recommended|regular|list-?price|rrp", re.I)


def num(value: Any) -> Optional[float]:
    """Machine value ("269.99", 1299.5) -> float first; human text -> Romanian parse."""
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


def minor(value: Any, digits: int = 2) -> Optional[float]:
    """Integer minor units (bani): 49999 -> 499.99."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return round(v / (10 ** digits), 2) if v > 0 else None


def money_values(text: Any) -> list[float]:
    out: list[float] = []
    for m in PRICE_WITH_CURRENCY_RE.finditer(norm(text)):
        v = parse_price(m.group(0))
        if v:
            out.append(v)
    return out


def is_unit_price(text: Any) -> bool:
    return bool(UNIT_PRICE_RE.search(norm(text)))


_LOW30_RE = re.compile(r"30\s*(?:de\s*)?zile|cel\s+mai\s+mic|lowest|omnibus|ultimele\s+30", re.I)
_ORIG_RE = re.compile(
    r"ini[tț]ial|original|recomandat|\bPRP\b|\bRRP\b|pre[tț]ul?\s+vechi|[iî]nainte|anterior|nereduce|standard", re.I)
_FROM_RE = re.compile(r"(?:^|\s)(?:de\s+la|from|ab|incep[aâ]nd)(?:\s|$)", re.I)


def price_lines(lines: Iterable[Any]) -> dict[str, Any]:
    """Read a card's price block given as text lines (one per <p>/row).

    The first unlabelled amount is the current price; an amount labelled "Preț inițial" / "PRP" / "recomandat" is
    the original (RRP), one labelled "cel mai mic preț din ultimele 30 de zile" is the EU Omnibus low. Unlabelled
    extra amounts follow the Zalando convention: the higher one is the original, the lower one (when there are
    two more) the 30-day low. Unit prices ("12,50 lei / 1 ml") are ignored."""
    current: Optional[float] = None
    price_from = False
    orig: list[float] = []
    low: list[float] = []
    others: list[float] = []
    for raw in lines:
        line = norm(raw if isinstance(raw, str) else " ".join(str(x) for x in (raw or [])))
        if not line:
            continue
        pos = 0
        for m in PRICE_WITH_CURRENCY_RE.finditer(line):
            ctx = line[pos:m.start()]
            tail = line[m.end():m.end() + 14]
            pos = m.end()
            if re.match(r"\*?\s*/", tail):
                continue  # unit price
            v = parse_price(m.group(0))
            if not v:
                continue
            if _LOW30_RE.search(ctx):
                low.append(v)
            elif _ORIG_RE.search(ctx):
                orig.append(v)
            elif current is None:
                current = v
                price_from = bool(_FROM_RE.search(ctx))
            else:
                others.append(v)
    original = max(orig) if orig else (max(others) if others else None)
    if low:
        lowest: Optional[float] = min(low)
    elif not orig and len(others) > 1:
        lowest = min(others)
    elif orig and others:
        lowest = min(others)
    else:
        lowest = None
    return {"price": current, "original": original, "lowest": lowest, "price_from": price_from}


def labeled_prices(texts: Iterable[Any]) -> tuple[Optional[float], Optional[float]]:
    """(original, lowest_30_days) from texts that LABEL their amount ("PRP: 349,99 lei", "Cel mai mic preț din
    ultimele 30 de zile: 299,99 lei"); unlabelled amounts are ignored."""
    orig: list[float] = []
    low: list[float] = []
    for raw in texts:
        line = norm(raw)
        pos = 0
        for m in PRICE_WITH_CURRENCY_RE.finditer(line):
            ctx, pos = line[pos:m.start()], m.end()
            if re.match(r"\*?\s*/", line[m.end():m.end() + 6]):
                continue
            v = parse_price(m.group(0))
            if not v:
                continue
            if _LOW30_RE.search(ctx):
                low.append(v)
            elif _ORIG_RE.search(ctx):
                orig.append(v)
    return (max(orig) if orig else None), (min(low) if low else None)


def best_old(price: Optional[float], original: Optional[float], lowest: Optional[float]) -> Optional[float]:
    """Old price for an Offer: the EU Omnibus 30-day low when shown and above the price (it is what tells a real
    Black-Friday discount), else the struck / RRP price; never <= price."""
    if not price:
        return None
    if lowest and lowest > price:
        return lowest
    if original and original > price:
        return original
    return None


def is_old_el(el) -> bool:
    if not isinstance(el, Tag):
        return False
    if el.name in ("del", "s", "strike"):
        return True
    if any(_OLD_TOKEN.search(c) for c in (el.get("class") or [])):
        return True
    tid = el.get("data-testid") or el.get("data-test") or ""
    if tid and _OLD_TESTID.search(tid):
        return True
    return "line-through" in (el.get("style") or "").replace(" ", "").lower()


def _is_noise(child: Tag) -> bool:
    t = text_of(child)
    if not t:
        return False
    if _PERCENT_ONLY.match(t) or is_unit_price(t):
        return True
    if not NOT_PRICE_RE.search(t):
        return False
    return not any(NOT_PRICE_RE.search(text_of(g)) for g in child.find_all(True))


def _old_ancestor(el: Tag, stop: Optional[Tag]) -> bool:
    for p in el.parents:
        if p is stop:
            return False
        if is_old_el(p):
            return True
    return False


def within(el, anc) -> bool:
    if el is anc:
        return True
    return any(p is anc for p in getattr(el, "parents", ()))


_PRICE_ATTRS = ("data-price-amount", "data-price", "content", "data-value")


def el_price(el) -> Optional[float]:
    """Price of an element: machine attributes first (plain decimals), then visible text (<sup> decimals)."""
    if el is None or not isinstance(el, Tag):
        return None
    for attr in _PRICE_ATTRS:
        v = el.get(attr)
        if v:
            p = num(v)
            if p:
                return p
    txt = element_price_text(el)
    if is_unit_price(txt) and len(money_values(txt)) <= 1:
        return None
    return parse_price(txt)


def current_price(el) -> Optional[float]:
    """Price of `el` ignoring struck / old children, unit prices, instalments and percentage badges."""
    if el is None or not isinstance(el, Tag):
        return None
    c = copy.copy(el)
    drop = [ch for ch in c.find_all(True) if is_old_el(ch) or _is_noise(ch)]
    for ch in drop:
        ch.extract()
    t = text_of(c)
    if t and NOT_PRICE_RE.search(t) and not money_values(t):
        return None
    return el_price(c)


def pick_price(root: Optional[Tag], *selectors: str) -> tuple[Optional[Tag], Optional[float]]:
    """First (element, price) over `selectors` in priority order that is not an old price and yields a price."""
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


def old_price_in(card: Optional[Tag], price: Optional[float], exclude: Iterable[Optional[Tag]] = ()) -> Optional[float]:
    """Highest struck / old-marked price inside `card` that is greater than `price` (< 20x)."""
    if card is None or not price:
        return None
    excl = [e for e in exclude if e is not None]
    best: Optional[float] = None
    for el in card.find_all(True):
        if not is_old_el(el) or any(within(e, el) for e in excl) or _old_ancestor(el, card):
            continue
        t = text_of(el)
        if not t and not el.get("content"):
            continue
        if t and (NOT_PRICE_RE.search(t) or (is_unit_price(t) and len(money_values(t)) <= 1)):
            continue
        v = el_price(el)
        if v and price < v < price * 20 and (best is None or v > best):
            best = v
    return best


def higher_money_in(el: Optional[Tag], price: Optional[float], limit: float = 5.0) -> Optional[float]:
    """Highest "NNN lei" amount in the element's text that is above `price` (an unmarked RRP)."""
    if el is None or not price:
        return None
    vals = [v for v in money_values(_visible_text(el)) if price * 1.005 < v < price * limit]
    return max(vals) if vals else None


def _visible_text(el: Tag) -> str:
    c = copy.copy(el)
    for ch in c.find_all(["script", "style", "noscript", "template"]):
        ch.extract()
    for ch in c.find_all(True):
        if is_unit_price(text_of(ch)) and len(money_values(text_of(ch))) <= 1:
            ch.extract()
    return element_price_text(c)


# --------------------------------------------------------------------------- stock

_OUT_TXT = re.compile(
    r"stoc\s+epuizat|\bepuizat|indisponibil|nu\s+(?:este|e|mai\s+este)\s+(?:[iî]n\s+)?stoc|nu\s+este\s+disponibil|"
    r"f[aă]r[aă]\s+stoc|sold\s*[-_ ]?out|out\s+of\s+stock|unavailable|stoc\s*:\s*0\b",
    re.I,
)
_IN_TXT = re.compile(
    r"[iî]n\s+stoc|stoc\s+(?:limitat|suficient|disponibil)|ultimele\s+(?:buc|\d)|ultimul\s+produs|\bdisponibil\b|"
    r"limited|in[_ ]stock|available|cantitate\s+limitat",
    re.I,
)


def stock_from_text(text: Any) -> Optional[bool]:
    t = norm(text)
    if not t:
        return None
    if _OUT_TXT.search(t):
        return False
    if _IN_TXT.search(t):
        return True
    return None


# --------------------------------------------------------------------------- block pages

_CF_MANAGED = re.compile(r"_cf_chl_opt|/cdn-cgi/challenge-platform/h/[a-z]+/orchestrate/|cf-chl-", re.I)
_BLOCK_MARKERS = (
    "captcha-delivery.com", "geo.captcha-delivery", "sec-if-cpt-container", "/_sec/cp_challenge", "px-captcha",
    "_incapsula_resource", "hcaptcha.com/1/api.js",
)
_BLOCK_TITLES = (
    "just a moment", "attention required", "access denied", "checking your browser", "are you a robot",
    "verify you are human", "captcha", "security check", "request blocked", "pardon our interruption",
    "robot check", "ddos-guard", "forbidden", "un moment", "verificare",
)


def is_challenge(html: str, soup: Optional[BeautifulSoup] = None) -> bool:
    low = (html or "").lower()
    if "just a moment" in low[:3000] or _CF_MANAGED.search(low[:400_000]):
        return True
    if any(m in low[:400_000] for m in _BLOCK_MARKERS):
        return True
    title = ""
    if soup is not None and soup.title is not None:
        title = soup.title.get_text(" ", strip=True).lower()
    return any(t in title for t in _BLOCK_TITLES)


def raise_if_challenge(html: str, soup: Optional[BeautifulSoup], url: str) -> None:
    """Call only when the store parser found no products: then a challenge marker means a block page."""
    if is_challenge(html, soup):
        title = soup.title.get_text(" ", strip=True)[:60] if soup is not None and soup.title else ""
        raise BlockedError(f"challenge page (HTTP 200) {title!r}", 200, url)


# --------------------------------------------------------------------------- json

_DECODER = json.JSONDecoder()


def json_at(text: str, idx: int) -> Any:
    """JSON value starting at text[idx] (whitespace skipped); None if it does not decode."""
    while idx < len(text) and text[idx] in " \t\r\n":
        idx += 1
    try:
        value, _ = _DECODER.raw_decode(text, idx)
    except ValueError:
        return None
    return value


def first(v: Any) -> Any:
    """Luigi's Box / Algolia style attributes: ["x"] -> "x"."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def dig(d: Any, *path: Any) -> Any:
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and -len(d) <= p < len(d):
            d = d[p]
        else:
            return None
    return d


# --------------------------------------------------------------------------- pagination / misc

_NEXT_TEXT = re.compile(r"^(?:›|>|→|urm[aă]toarea|urm[aă]toare|[iî]nainte|next|pagina\s+urm[aă]toare)$", re.I)


def next_page_url(soup: Optional[BeautifulSoup], base: str) -> Optional[str]:
    """The pager's "next" link: rel=next, then a link with class/aria-label/text "next"."""
    if soup is None:
        return None

    def ok(href: Optional[str]) -> Optional[str]:
        h = (href or "").strip()
        if not h or h.startswith(("#", "javascript:")):
            return None
        u = urljoin(base, h)
        return u if u.startswith("http") and u.rstrip("/") != base.rstrip("/") else None

    for el in soup.select("link[rel~='next'][href], a[rel~='next'][href]"):
        u = ok(el.get("href"))
        if u:
            return u
    for a in soup.select(".pagination a[href], .pager a[href], nav a[href], [class*='paginat'] a[href]"):
        cls = " ".join(a.get("class") or []).lower()
        label = (a.get("aria-label") or a.get("title") or "").strip()
        if _NEXT_TEXT.match(text_of(a)) or re.search(r"(?:^|[-_ ])next(?:$|[-_ ])", cls) or _NEXT_TEXT.match(label) \
                or (a.get("data-test") or "").lower().endswith("nextbutton"):
            u = ok(a.get("href"))
            if u:
                return u
    return None


class NextPager:
    """Mixed in before `Store`: page N+1 is fetched only through the pager link remembered from page N."""

    def _pager_reset(self) -> None:
        self._next_urls: dict[int, str] = {}

    def _pager_get(self, page: int) -> Optional[str]:
        return getattr(self, "_next_urls", {}).get(page)

    def _pager_set(self, page: int, url: Optional[str]) -> None:
        if not hasattr(self, "_next_urls"):
            self._next_urls = {}
        if url:
            self._next_urls[page] = url

    def fallback(self, html: str, url: str, soup: Optional[BeautifulSoup] = None) -> list:
        offers = super().fallback(html, url, soup)  # type: ignore[misc]
        for o in offers:
            o.url = clean_url(o.url, url) or o.url
        return dedupe(offers)


def dedupe(offers: Iterable[Any]) -> list:
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
