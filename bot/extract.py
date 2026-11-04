"""Generic, store-agnostic product extraction.

Used as a fallback when a store's specific parser finds nothing (layout changed),
for custom shops, and for product-page (URL) watches.

Strategies, in order of reliability:
  1. schema.org JSON-LD (Product / ItemList / AggregateOffer)
  2. schema.org microdata (itemprop=price ...)
  3. embedded state JSON (__NEXT_DATA__, window.__INITIAL_STATE__, application/json scripts)
  4. heuristic "product card" detection: link + price-with-currency inside a small container
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import Offer, canonical_url
from .prices import PRICE_WITH_CURRENCY_RE, element_price_text, parse_price

log = logging.getLogger("bot.extract")


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover
        return BeautifulSoup(html, "html.parser")


# --------------------------------------------------------------------------- JSON-LD

def iter_jsonld(soup: BeautifulSoup) -> Iterator[dict]:
    for sc in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = sc.string or sc.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            try:  # some shops emit trailing commas / control chars
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.replace("\n", " ")))
            except ValueError:
                continue
        yield from _flatten_ld(data)


def _flatten_ld(data: Any) -> Iterator[dict]:
    if isinstance(data, list):
        for d in data:
            yield from _flatten_ld(d)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _flatten_ld(data["@graph"])
        else:
            yield data


def _types(obj: dict) -> set[str]:
    t = obj.get("@type")
    if isinstance(t, list):
        return {str(x).lower() for x in t}
    return {str(t).lower()} if t else set()


_IN_STOCK = ("instock", "limitedavailability", "onlineonly", "presale", "preorder", "backorder")
_OUT_STOCK = ("outofstock", "soldout", "discontinued")


def availability_flag(value: Any) -> Optional[bool]:
    if value is None:
        return None
    v = str(value).lower()
    if any(x in v for x in _OUT_STOCK):
        return False
    if any(x in v for x in _IN_STOCK):
        return True
    return None


def offer_from_ld_product(obj: dict, base_url: str, store: str) -> Optional[Offer]:
    name = obj.get("name")
    if isinstance(name, list):
        name = name[0] if name else None
    offers = obj.get("offers")
    if isinstance(offers, list):
        offers = [o for o in offers if isinstance(o, dict)]
        # cheapest offer
        offers = sorted(offers, key=lambda o: parse_price(o.get("price") or o.get("lowPrice")) or 1e12)
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        offers = {}
    price = parse_price(offers.get("price"))
    if price is None:
        price = parse_price(offers.get("lowPrice"))
    if price is None:
        ps = offers.get("priceSpecification")
        if isinstance(ps, list) and ps:
            ps = ps[0]
        if isinstance(ps, dict):
            price = parse_price(ps.get("price"))
    if not name or price is None:
        return None
    currency = (offers.get("priceCurrency") or "RON").upper()
    if currency not in ("RON", "LEI"):
        return None
    url = obj.get("url") or offers.get("url") or obj.get("@id") or base_url
    if isinstance(url, list):
        url = url[0]
    image = obj.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    old = None
    hp = offers.get("highPrice")
    if hp and offers.get("@type", "").lower() != "aggregateoffer":
        old = parse_price(hp)
    sku = obj.get("sku") or obj.get("productID") or obj.get("mpn")
    shop_count = None
    if str(offers.get("@type", "")).lower() == "aggregateoffer":
        try:
            shop_count = int(offers.get("offerCount")) if offers.get("offerCount") else None
        except (TypeError, ValueError):
            shop_count = None
    return Offer(
        store=store,
        title=str(name).strip(),
        url=urljoin(base_url, str(url)),
        price=price,
        old_price=old if old and old > price else None,
        in_stock=availability_flag(offers.get("availability")),
        offer_id=str(sku) if sku else None,
        image=str(image) if image else None,
        shop_count=shop_count,
        source="jsonld",
    )


def offers_from_jsonld(soup: BeautifulSoup, base_url: str, store: str) -> list[Offer]:
    out: list[Offer] = []
    for obj in iter_jsonld(soup):
        types = _types(obj)
        if "product" in types or "productgroup" in types:
            o = offer_from_ld_product(obj, base_url, store)
            if o:
                out.append(o)
        elif "itemlist" in types or "offercatalog" in types:
            for el in obj.get("itemListElement") or []:
                if not isinstance(el, dict):
                    continue
                item = el.get("item") if isinstance(el.get("item"), dict) else el
                if "product" in _types(item):
                    o = offer_from_ld_product(item, base_url, store)
                    if o:
                        out.append(o)
    return _dedupe(out)


# --------------------------------------------------------------------------- microdata

def offers_from_microdata(soup: BeautifulSoup, base_url: str, store: str) -> list[Offer]:
    out: list[Offer] = []
    for el in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/Product$", re.I)}):
        name_el = el.find(attrs={"itemprop": "name"})
        price_el = el.find(attrs={"itemprop": re.compile(r"^(price|lowPrice)$")})
        if not name_el or not price_el:
            continue
        name = name_el.get("content") or name_el.get_text(" ", strip=True)
        price = parse_price(price_el.get("content") or element_price_text(price_el))
        if not name or price is None:
            continue
        cur_el = el.find(attrs={"itemprop": "priceCurrency"})
        if cur_el is not None:
            cur = (cur_el.get("content") or cur_el.get_text(strip=True) or "RON").upper()
            if cur not in ("RON", "LEI"):
                continue
        url_el = el.find(attrs={"itemprop": "url"})
        url = None
        if url_el is not None:
            url = url_el.get("href") or url_el.get("content")
        if not url:
            a = el.find("a", href=True)
            url = a["href"] if a else base_url
        av = el.find(attrs={"itemprop": "availability"})
        sku = el.find(attrs={"itemprop": "sku"})
        out.append(Offer(
            store=store, title=name.strip(), url=urljoin(base_url, url), price=price,
            in_stock=availability_flag((av.get("href") or av.get("content") or av.get_text()) if av else None),
            offer_id=(sku.get("content") or sku.get_text(strip=True)) if sku else None,
            source="microdata",
        ))
    return _dedupe(out)


# --------------------------------------------------------------------------- embedded JSON

_STATE_ASSIGN_RE = re.compile(
    r"(?:window\.)?(__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__|__STATE__|INITIAL_DATA|__DATA__)\s*=\s*",
)


def _balanced_json(text: str, start: int) -> Optional[str]:
    """Return the JSON object/array literal starting at text[start] ('{' or '[')."""
    if start >= len(text) or text[start] not in "{[":
        return None
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, min(len(text), start + 8_000_000)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        elif c in "\"'":
            in_str, quote = True, c
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def embedded_json_blobs(html: str, soup: Optional[BeautifulSoup] = None) -> list[Any]:
    soup = soup or make_soup(html)
    blobs: list[Any] = []
    for sc in soup.find_all("script"):
        typ = (sc.get("type") or "").lower()
        raw = sc.string or ""
        if not raw:
            continue
        if sc.get("id") == "__NEXT_DATA__" or typ == "application/json":
            try:
                blobs.append(json.loads(raw))
            except ValueError:
                pass
            continue
        if "ld+json" in typ:
            continue
        for m in _STATE_ASSIGN_RE.finditer(raw):
            i = m.end()
            while i < len(raw) and raw[i] in " \t\r\n":
                i += 1
            if raw.startswith("JSON.parse(", i):
                # window.__X__ = JSON.parse("...escaped...")
                j = i + len("JSON.parse(")
                if j < len(raw) and raw[j] == '"':
                    end = j + 1
                    while end < len(raw):
                        if raw[end] == "\\":
                            end += 2
                            continue
                        if raw[end] == '"':
                            break
                        end += 1
                    try:
                        blobs.append(json.loads(json.loads(raw[j:end + 1])))
                    except ValueError:
                        pass
                continue
            lit = _balanced_json(raw, i)
            if lit:
                try:
                    blobs.append(json.loads(lit))
                except ValueError:
                    pass
    return blobs


_NAME_KEYS = ("name", "title", "productName", "product_name", "displayName", "nume", "denumire")
_PRICE_KEYS = (
    "final_price", "finalPrice", "price_final", "salePrice", "sale_price", "special_price", "specialPrice",
    "currentPrice", "current_price", "priceValue", "price", "pret", "Price", "sellingPrice", "offerPrice",
    "price_with_vat", "priceWithVat", "lowPrice", "minPrice", "min_price",
)
_OLD_KEYS = (
    "regular_price", "regularPrice", "old_price", "oldPrice", "listPrice", "ListPrice", "list_price",
    "initial_price", "initialPrice", "originalPrice", "original_price", "rrp", "pret_vechi", "fullPrice",
    "price_before_discount", "priceBeforeDiscount", "strikePrice", "strikethroughPrice", "basePrice",
)
_URL_KEYS = ("url", "link", "href", "productUrl", "product_url", "canonical_url", "permalink", "url_key", "slug", "linkText", "pipUrl")
_ID_KEYS = ("sku", "id", "productId", "product_id", "code", "offer_id", "part_number", "pnk")
_STOCK_KEYS = ("in_stock", "inStock", "isInStock", "is_in_stock", "available", "availability", "stock_status", "stockStatus")


def _num_from(v: Any) -> Optional[float]:
    if isinstance(v, dict):
        for k in ("value", "amount", "current", "price", "numeral", "final", "gross", "withVat", "with_vat"):
            if k in v:
                p = _num_from(v[k])
                if p:
                    return p
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if 0 < v < 10_000_000 else None
    if isinstance(v, str) and re.search(r"\d", v) and len(v) < 40:
        return parse_price(v)
    return None


def _first(d: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _stock_from(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    if isinstance(v, str):
        s = v.lower()
        if any(x in s for x in ("out", "epuizat", "indisponibil", "stoc 0", "nu este")):
            return False
        if any(x in s for x in ("in_stock", "instock", "in stoc", "disponibil", "available", "true")):
            return True
    return None


def product_like_dicts(obj: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 14:
        return
    if isinstance(obj, dict):
        if _first(obj, _NAME_KEYS) and isinstance(_first(obj, _NAME_KEYS), str) and _first(obj, _PRICE_KEYS) is not None:
            yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from product_like_dicts(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from product_like_dicts(v, depth + 1)


def offers_from_json_blob(blob: Any, base_url: str, store: str, url_builder=None) -> list[Offer]:
    out: list[Offer] = []
    for d in product_like_dicts(blob):
        name = _first(d, _NAME_KEYS)
        price = _num_from(_first(d, _PRICE_KEYS))
        if not isinstance(name, str) or len(name) < 4 or price is None:
            continue
        old = _num_from(_first(d, _OLD_KEYS))
        raw_url = _first(d, _URL_KEYS)
        if isinstance(raw_url, dict):
            raw_url = _first(raw_url, ("href", "url", "path"))
        url = None
        if url_builder:
            url = url_builder(d)
        if not url and isinstance(raw_url, str):
            url = urljoin(base_url, raw_url)
        if not url:
            continue
        pid = _first(d, _ID_KEYS)
        stock = _stock_from(_first(d, _STOCK_KEYS))
        out.append(Offer(
            store=store, title=name.strip(), url=url, price=price,
            old_price=old if old and old > price else None,
            in_stock=stock, offer_id=str(pid) if pid not in (None, "") else None, source="embedded-json",
        ))
    return _dedupe(out)


# --------------------------------------------------------------------------- heuristic cards

_NOT_PRICE_CONTEXT = re.compile(
    r"(/\s*lun|lunar|rate|rată|rata|economis|reducere|discount|cashback|voucher|transport|livrare|puncte|-\s*\d|garan)",
    re.I,
)
_OLD_CLASS = re.compile(r"(old|vechi|initial|rrp|prp|strike|before|crossed|regular|list|was)", re.I)
_SKIP_LINK = re.compile(r"(#|javascript:|/cos|/cart|/login|/wishlist|/compare|/favorite|mailto:|tel:)", re.I)


def _is_old_price_node(node: Tag) -> bool:
    for p in [node, *list(node.parents)[:3]]:
        if not isinstance(p, Tag):
            continue
        if p.name in ("s", "del", "strike"):
            return True
        cls = " ".join(p.get("class") or [])
        if _OLD_CLASS.search(cls):
            return True
    return False


def heuristic_cards(soup: BeautifulSoup, base_url: str, store: str, min_cards: int = 3) -> list[Offer]:
    """Find repeated containers with a product link and a 'NNN lei' price."""
    found: dict[str, Offer] = {}
    seen_containers: set[int] = set()
    for text_node in soup.find_all(string=PRICE_WITH_CURRENCY_RE):
        el = text_node.parent
        if not isinstance(el, Tag) or el.name in ("script", "style", "option", "title"):
            continue
        # climb to a container that contains a meaningful product link
        container = el
        link = None
        for _ in range(7):
            container = container.parent
            if not isinstance(container, Tag) or container.name in ("body", "html"):
                container = None
                break
            for a in container.find_all("a", href=True):
                href = a["href"]
                if _SKIP_LINK.search(href):
                    continue
                txt = a.get("title") or a.get_text(" ", strip=True)
                if not txt:
                    img = a.find("img", alt=True)
                    txt = img["alt"] if img else ""
                if len(txt) >= 12:
                    link = (a, txt)
                    break
            if link:
                break
        if not container or not link or id(container) in seen_containers:
            continue
        seen_containers.add(id(container))
        # prices inside this container
        cur: list[float] = []
        old: list[float] = []
        for tn in container.find_all(string=PRICE_WITH_CURRENCY_RE):
            par = tn.parent
            if not isinstance(par, Tag):
                continue
            ctx = par.get_text(" ", strip=True)
            if _NOT_PRICE_CONTEXT.search(ctx):
                continue
            p = parse_price(element_price_text(par))
            if p is None:
                continue
            (old if _is_old_price_node(par) else cur).append(p)
        if not cur:
            continue
        price = cur[0]
        old_price = max(old) if old else (max(cur) if len(cur) > 1 and max(cur) > price * 1.02 else None)
        a, title = link
        url = urljoin(base_url, a["href"])
        key = canonical_url(url)
        if key in found:
            continue
        ctext = container.get_text(" ", strip=True).lower()
        stock: Optional[bool] = None
        if any(x in ctext for x in ("stoc epuizat", "indisponibil", "nu este in stoc", "nu este în stoc", "out of stock")):
            stock = False
        elif any(x in ctext for x in ("în stoc", "in stoc", "disponibil", "stoc limitat", "ultimul produs")):
            stock = True
        found[key] = Offer(
            store=store, title=" ".join(title.split()), url=url, price=price,
            old_price=old_price if old_price and old_price > price else None,
            in_stock=stock, source="heuristic",
        )
    offers = list(found.values())
    return offers if len(offers) >= min_cards else []


# --------------------------------------------------------------------------- orchestration

def _dedupe(offers: list[Offer]) -> list[Offer]:
    seen: set[str] = set()
    out: list[Offer] = []
    for o in offers:
        k = o.key
        if k in seen:
            continue
        seen.add(k)
        out.append(o)
    return out


def extract_offers(html: str, base_url: str, store: str, *, soup: Optional[BeautifulSoup] = None,
                   allow_heuristic: bool = True, min_listing: int = 2) -> list[Offer]:
    """Try every generic strategy and return the first that yields a plausible listing."""
    soup = soup or make_soup(html)
    for fn in (offers_from_jsonld, offers_from_microdata):
        offers = fn(soup, base_url, store)
        if len(offers) >= min_listing:
            return offers
    best: list[Offer] = []
    for blob in embedded_json_blobs(html, soup):
        offers = offers_from_json_blob(blob, base_url, store)
        if len(offers) > len(best):
            best = offers
    if len(best) >= min_listing:
        return best
    if allow_heuristic:
        offers = heuristic_cards(soup, base_url, store)
        if offers:
            return offers
    return []


def extract_product_page(html: str, url: str, store: str) -> Optional[Offer]:
    """Single product page -> Offer (JSON-LD, microdata, meta tags)."""
    soup = make_soup(html)
    lds = offers_from_jsonld(soup, url, store)
    if lds:
        o = lds[0]
        o.url = url
        return o
    md = offers_from_microdata(soup, url, store)
    if md:
        md[0].url = url
        return md[0]
    price = None
    for prop in ("product:price:amount", "og:price:amount", "twitter:data1"):
        m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if m and m.get("content"):
            price = parse_price(m["content"])
            if price:
                break
    if price is None:
        el = soup.find(attrs={"data-price-type": "finalPrice"}) or soup.find(attrs={"itemprop": "price"})
        if el is not None:
            price = parse_price(el.get("data-price-amount") or el.get("content") or element_price_text(el))
    if price is None:
        return None
    t = soup.find("meta", attrs={"property": "og:title"})
    title = t["content"] if t and t.get("content") else (soup.title.get_text(strip=True) if soup.title else url)
    avail = soup.find("meta", attrs={"property": re.compile(r"(product|og):availability")})
    return Offer(store=store, title=title.strip(), url=url, price=price,
                 in_stock=availability_flag(avail.get("content")) if avail else None, source="meta")
