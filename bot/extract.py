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

import itertools
import json
import logging
import re
import unicodedata
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import parse_qsl, unquote, urldefrag, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from bs4.element import PreformattedString

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
            # strict=False: raw TAB / CR / LF inside strings (descriptions pasted from a CMS) are accepted
            data = json.loads(raw, strict=False)
        except ValueError:
            try:  # some shops emit trailing commas
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw), strict=False)
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
    aggregate = "aggregateoffer" in _types(offers)     # "@type" may be a list: ["Offer"]
    hp = offers.get("highPrice")
    if hp and not aggregate:
        old = parse_price(hp)
    sku = obj.get("sku") or obj.get("productID") or obj.get("mpn")
    shop_count = None
    if aggregate:
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

_MD_PRODUCT_TYPE = re.compile(r"schema\.org/(?:Product|IndividualProduct|ProductModel)/?$", re.I)
# a Product item scope nested inside one of these is a list member / related item, not the page's product
_MD_NESTING_TYPE = re.compile(
    r"schema\.org/(?:Product\w*|IndividualProduct|ItemList|ListItem|OfferCatalog|Offer|AggregateOffer|SomeProducts)/?$", re.I)
_MD_RELATED_PROPS = frozenset({
    "isrelatedto", "issimilarto", "isaccessoryorsparepartfor", "isconsumablefor", "itemlistelement", "item",
    "hasvariant", "isvariantof", "model",
})
# item scopes whose 'name' is never the product title
_MD_NOT_TITLE_TYPE = re.compile(
    r"schema\.org/(?:Brand|Organization|Person|Review|Rating|AggregateRating|Place|PostalAddress|Offer|AggregateOffer)/?$", re.I)


def _md_types(tag: Tag) -> list[str]:
    t = tag.get("itemtype") or ""
    if isinstance(t, list):
        t = " ".join(t)
    return t.split()


def _md_is(tag: Tag, rx: re.Pattern) -> bool:
    return any(rx.search(t) for t in _md_types(tag))


def _md_props(tag: Tag) -> set[str]:
    v = tag.get("itemprop") or ""
    if isinstance(v, list):
        v = " ".join(v)
    return {p.lower() for p in v.split()}


def _md_scope(tag: Tag) -> Optional[Tag]:
    """The item scope a property tag belongs to (its nearest itemscope ancestor)."""
    return tag.find_parent(attrs={"itemscope": True})


def _md_own(scope: Tag, names: set[str]) -> list[Tag]:
    """Property tags of `scope` itself (not of a nested Brand / Offer / Review / related Product scope)."""
    return [t for t in scope.find_all(attrs={"itemprop": True}) if _md_props(t) & names and _md_scope(t) is scope]


def _md_offer_scopes(scope: Tag, depth: int = 0) -> list[Tag]:
    """The product scope plus its itemprop=offers scopes (Offer / AggregateOffer -> Offer) and their
    priceSpecification scopes."""
    out = [scope]
    if depth < 3:
        for t in _md_own(scope, {"offers"} if depth == 0 else {"offers", "pricespecification"}):
            if t.has_attr("itemscope"):
                out.extend(_md_offer_scopes(t, depth + 1))
    return out


def _md_first(scopes: list[Tag], names: set[str]) -> Optional[Tag]:
    for sc in scopes:
        found = _md_own(sc, names)
        if found:
            return found[0]
    return None


def _md_value(tag: Optional[Tag]) -> Optional[str]:
    if tag is None:
        return None
    for attr in ("content", "href", "src", "value"):
        v = tag.get(attr)
        if v:
            return str(v)
    return tag.get_text(" ", strip=True) or None


def _md_in_untitled_scope(tag: Tag, product: Tag) -> bool:
    sc = _md_scope(tag)
    return sc is not None and sc is not product and _md_is(sc, _MD_NOT_TITLE_TYPE)


def _md_is_top_level(el: Tag) -> bool:
    """A Product scope that is the page's own product: not a list member and not a related/similar item."""
    if _md_props(el) & _MD_RELATED_PROPS:
        return False
    return not any(isinstance(p, Tag) and p.has_attr("itemscope") and _md_is(p, _MD_NESTING_TYPE) for p in el.parents)


_MD_LIST_TYPE = re.compile(r"schema\.org/(?:ItemList|ListItem|OfferCatalog|SomeProducts)/?$", re.I)


def _md_in_list_scope(tag: Tag) -> bool:
    """Tag belongs to a product list, a list member or a related / similar product (not the page's product)."""
    for p in itertools.chain([tag], tag.parents):
        if not isinstance(p, Tag):
            continue
        if _md_props(p) & _MD_RELATED_PROPS:
            return True
        if p.has_attr("itemscope") and (_md_is(p, _MD_LIST_TYPE)
                                        or (_md_is(p, _MD_PRODUCT_TYPE) and not _md_is_top_level(p))):
            return True
    return False


def _md_products(soup: BeautifulSoup, base_url: str, store: str,
                 top_level_only: bool = False) -> Iterator[tuple[Offer, Optional[str]]]:
    """(offer, explicit itemprop=url) for each schema.org/Product item scope."""
    for el in soup.find_all(attrs={"itemtype": True}):
        if not _md_is(el, _MD_PRODUCT_TYPE):
            continue
        if top_level_only and not _md_is_top_level(el):
            continue
        offer_scopes = _md_offer_scopes(el)
        name_el = _md_first(offer_scopes, {"name"})
        if name_el is None:  # name inside some other wrapper: any 'name' that is not a Brand / seller / review name
            name_el = next((t for t in el.find_all(attrs={"itemprop": True})
                            if "name" in _md_props(t) and not _md_in_untitled_scope(t, el)), None)
        price_el = _md_first(offer_scopes, {"price", "lowprice"})
        if name_el is None or price_el is None:
            continue
        name = name_el.get("content") or name_el.get_text(" ", strip=True)
        price = parse_price(price_el.get("content") or element_price_text(price_el))
        if not name or price is None:
            continue
        cur_el = _md_first(offer_scopes, {"pricecurrency"})
        if cur_el is not None:
            cur = (cur_el.get("content") or cur_el.get_text(strip=True) or "RON").upper()
            if cur not in ("RON", "LEI"):
                continue
        explicit = _md_value(_md_first(offer_scopes, {"url"}))
        url = explicit
        if not url:
            a = el.find("a", href=True)
            url = a["href"] if a else base_url
        av = _md_first(offer_scopes, {"availability"})
        sku = _md_first(offer_scopes, {"sku", "productid"})
        image = _md_value(_md_first([el], {"image"}))
        yield Offer(
            store=store, title=" ".join(name.split()), url=urljoin(base_url, url), price=price,
            in_stock=availability_flag((av.get("href") or av.get("content") or av.get_text()) if av else None),
            offer_id=(sku.get("content") or sku.get_text(strip=True) or None) if sku else None,
            image=urljoin(base_url, image) if image else None,
            source="microdata",
        ), explicit


def offers_from_microdata(soup: BeautifulSoup, base_url: str, store: str) -> list[Offer]:
    return _dedupe([o for o, _ in _md_products(soup, base_url, store)])


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
                blobs.append(json.loads(raw, strict=False))
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
                    blobs.append(json.loads(lit, strict=False))
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
        p = parse_price(v)
        return p if p is not None and p < 10_000_000 else None
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
# Links that are never the product link of a card. Anchored to whole path segments, so /carte/... (books),
# /cartuse-... (cartridges) and /cosmetice/... are product links, and /p/1#reviews is the product itself.
_SKIP_LINK = re.compile(
    r"^\s*(?:#|javascript:|mailto:|tel:|sms:|whatsapp:)"
    r"|/(?:cos|cart|checkout|login|wishlist|compare|favorites?|cos-de-cumparaturi|cosul-meu)(?:[/?#]|$)"
    r"|add[-_]?to[-_]?cart|adauga[-_]?in[-_]?cos"
    r"|[?&](?:add_to_wishlist|wishlist|compare)=",
    re.I,
)
# link texts that are cart actions (the link is never the product link) / calls to action (never a title)
_CART_TEXT_RE = re.compile(r"^(?:adauga (?:in|la) cos\w*|cumpara(?: acum)?|add to (?:cart|bag)|buy(?: now)?|in cos)$")
_NOT_TITLE_RE = re.compile(
    r"^(?:vezi\b.*|detalii\b.*|mai multe\b.*|info|compara\b.*|citeste\b.*|toate ofertele|adauga la \w+.*"
    r"|\(?\d+\)?(?: (?:review|recenzi|oferte|magazine|pareri)\S*)?)$")
# "1.258,00 Lei / 100 ml", "37,58 lei / l", "12,99 lei/buc": a unit price, not the product's price
_UNIT_AFTER_RE = re.compile(
    r"\s*/\s*(?:\d+(?:[.,]\d+)?\s*)?(?:ml|cl|l|litr[ui]|kg|g|gr|buc|bucat[aă]|mp|m2|m²|m|metru|doz[aă]|spal[aă]ri|capsul[aă])\b",
    re.I)
_NAME_CLASS_WORDS = frozenset({"title", "name", "titlu", "nume", "denumire"})
# product links whose identity is in the query string (Jump.php?id=..., index.php?route=product&product_id=...)
_SCRIPT_PATH_RE = re.compile(r"\.(?:php|aspx?|jsp|cgi|pl|do)$|^/?$", re.I)
_TRACKING_PARAM_RE = re.compile(r"^(?:utm_\w*|gclid|fbclid|yclid|ref\w*|src|source|gtm\w*|from|list|pos(?:ition)?|sr|mc_\w*|_ga)$", re.I)
_NON_CONTENT_TAGS = frozenset({"script", "style", "option", "title", "noscript", "template", "select", "textarea"})
_CURRENCY_WORD_RE = re.compile(r"(?<![^\W\d_])(?:lei|ron)\b", re.I)    # "lei" not glued to a letter
_PRICE_TEXT_MAX = 80                                                    # a price element's text is short
_SPLIT_PREFIX_RE = re.compile(r"[\d\s.,]*\d[\d\s.,]*")
_DECIMALS_ONLY_RE = re.compile(r"\s*\d{2}\s*(?:lei|ron)\b", re.I)          # "99 Lei": decimals + currency

# Old-price detection by class: class tokens are split into words on '-', '_' and camelCase boundaries, so
# 'font-bold' / 'price-holder' / 'product-list-item' are not old prices, while 'product-old-price',
# 'price__old', 'oldPrice', 'priceOld', 'pret-vechi', 'line-through', 'regular-price' are.
_CLASS_WORD_SPLIT = re.compile(r"[-_\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_OLD_WORDS = frozenset({"old", "vechi", "rrp", "prp", "crossed", "barred", "taiat"})
_OLD_IF_PRICE_WORDS = frozenset({"regular", "initial", "before", "was", "original", "full", "previous", "prev", "anterior"})
_PRICE_WORDS = frozenset({"price", "prices", "pret", "preturi", "amount"})


# the same written as one lower-case word: 'oldprice', 'priceold', 'pretvechi', 'regularprice'
_GLUED_OLD_WORD_RE = re.compile(
    r"(?:old|was|regular|initial|before|original|full|prev|previous)(?:price|pret)s?"
    r"|(?:price|pret)(?:old|vechi|was|initial|before|regular|original)")


def _is_old_class_token(token: str) -> bool:
    low = token.lower()
    if "linethrough" in re.sub(r"[-_]", "", low) or re.search(r"list[-_]*price", low):
        return True
    words = {w.lower() for w in _CLASS_WORD_SPLIT.split(token) if w}
    if any(w in _OLD_WORDS or w.startswith(("strike", "crossed")) or _GLUED_OLD_WORD_RE.fullmatch(w) for w in words):
        return True
    return bool(words & _OLD_IF_PRICE_WORDS) and bool(words & _PRICE_WORDS)


def _is_old_price_node(node: Tag, stop: Optional[Tag] = None) -> bool:
    """Price element (or one of its 3 nearest ancestors below `stop`) marks a struck-through / old price."""
    p: Any = node
    for _ in range(4):
        if not isinstance(p, Tag) or p is stop:
            break
        if p.name in ("s", "del", "strike"):
            return True
        if any(_is_old_class_token(c) for c in (p.get("class") or [])):
            return True
        if "line-through" in str(p.get("style") or "").lower():
            return True
        p = p.parent
    return False


def _short_price_text(el: Tag) -> Optional[str]:
    raw = el.get_text(" ", strip=True)
    if len(raw) > _PRICE_TEXT_MAX:
        return None
    return element_price_text(el)


def _price_seed(s: Any) -> bool:
    # PreformattedString: comments, CDATA, doctype - never visible prices
    return bool(s) and not isinstance(s, PreformattedString) and bool(_CURRENCY_WORD_RE.search(s))


def _widen_split(el: Tag, root: Tag) -> Optional[tuple[Tag, Optional[float], str]]:
    """'99 Lei' whose integer part sits in a sibling element ("<span>2.599</span><span>99 Lei</span>"):
    the ancestor whose text is only digits/separators plus this text holds the whole price.

    Only a bare two-digit amount is widened: "<span>1.299</span> <span>999 lei</span>" (an old price without
    currency next to the new one) stays 999."""
    inner = element_price_text(el)
    if not _DECIMALS_ONLY_RE.match(inner):
        return None
    up = el
    for _ in range(3):
        if up is root or not isinstance(up.parent, Tag) or up.parent.name in ("body", "html", "[document]"):
            break
        outer = _short_price_text(up.parent)
        if not outer or not outer.endswith(inner):
            break
        prefix = outer[:-len(inner)]
        if not prefix.strip():           # a wrapper with the same text: look one level higher
            up = up.parent
            continue
        if not _SPLIT_PREFIX_RE.fullmatch(prefix):
            break
        return up.parent, parse_price(outer), outer
    return None


def _price_elements(root: Tag) -> Iterator[tuple[Tag, float, str]]:
    """(element, price, context text) for every 'NNN lei' price under root, also when the digits, the
    <sup> decimals and the currency sit in separate text nodes ("2.199<sup>99</sup> <span>Lei</span>",
    "<span>2.599</span><span>99 Lei</span>"). Each element is the smallest one holding the whole price."""
    seen: set[int] = set()
    for tn in root.find_all(string=_price_seed):
        par = tn.parent
        if not isinstance(par, Tag) or par.name in _NON_CONTENT_TAGS:
            continue
        if any(isinstance(p, Tag) and p.name in _NON_CONTENT_TAGS for p in par.parents):
            continue
        el: Optional[Tag] = None
        value: Optional[float] = None
        ctx = ""
        if PRICE_WITH_CURRENCY_RE.search(str(tn)):
            el = par
            text = element_price_text(par)
            matches = list(PRICE_WITH_CURRENCY_RE.finditer(text))
            if len(matches) == 1:
                value, ctx = parse_price(matches[0].group(0)), par.get_text(" ", strip=True)
                el, value, ctx = _widen_split(par, root) or (el, value, ctx)
            else:  # old and new price in one parent: this text node's own price
                value, ctx = parse_price(str(tn)), " ".join(str(tn).split())
        else:  # currency alone ("Lei", ",99 lei"): the digits are in sibling elements
            cand: Any = par
            for _ in range(3):
                if not isinstance(cand, Tag) or cand.name in ("body", "html", "[document]"):
                    break
                text = _short_price_text(cand)
                if text is None:
                    break
                if PRICE_WITH_CURRENCY_RE.search(text):
                    el, value, ctx = _widen_split(cand, root) or (cand, parse_price(text), text)
                    break
                if cand is root:
                    break
                cand = cand.parent
        if el is None or value is None or id(el) in seen:
            continue
        seen.add(id(el))
        yield el, value, ctx


def _fold(text: str) -> str:
    """Lower case, no diacritics, single spaces: 'Adaugă în coș' -> 'adauga in cos'."""
    t = unicodedata.normalize("NFKD", text.lower())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())


def _has_name_class(tag: Tag) -> bool:
    return any(w.lower() in _NAME_CLASS_WORDS for c in (tag.get("class") or []) for w in _CLASS_WORD_SPLIT.split(c))


_HEADING_RE = re.compile(r"^h[1-6]$")


def _name_elements(root: Tag, limit: int = 0) -> list[Tag]:
    """Headings and elements with a title / name class ('product-card-title', 'productName') under root."""
    out = [t for t in root.find_all(True) if _HEADING_RE.match(t.name) or _has_name_class(t)]
    return out[:limit] if limit else out


def _is_unit_price(ctx: str, value: float) -> bool:
    for m in PRICE_WITH_CURRENCY_RE.finditer(ctx):
        if parse_price(m.group(0)) == value:
            return bool(_UNIT_AFTER_RE.match(ctx, m.end()))
    return False


def _anchor_texts(a: Tag) -> list[str]:
    out = [a.get("title") or "", a.get("aria-label") or ""]
    out.extend(t.get_text(" ", strip=True) for t in _name_elements(a))
    out.append(a.get_text(" ", strip=True))
    out.extend((img.get("alt") or img.get("title") or "") for img in a.find_all("img"))
    return [" ".join(str(t).split()) for t in out if t and str(t).strip()]


def _usable_title(text: str) -> bool:
    return len(text) <= 300 and not PRICE_WITH_CURRENCY_RE.search(text) and not _NOT_TITLE_RE.match(_fold(text))


def _best_title(anchors: list[Tag], container: Tag) -> str:
    """Longest name among the product's links (title attribute, heading / name element, link text, image
    alt), never a call to action ("Vezi detalii", "Compară prețuri"); a heading or name element of the card
    when the links carry no usable name."""
    cands = [t for a in anchors for t in _anchor_texts(a)]
    best = max((t for t in cands if _usable_title(t)), key=len, default="")
    if len(best) < 12:
        heads = [" ".join(h.get_text(" ", strip=True).split()) for h in _name_elements(container, limit=6)]
        best = max([best, *(h for h in heads if h and _usable_title(h))], key=len)
    return best


def _link_key(url: str) -> str:
    """canonical_url, plus the non-tracking query for script URLs whose query names the product."""
    key = canonical_url(url)
    try:
        p = urlsplit(url)
    except ValueError:
        return key
    if p.query and _SCRIPT_PATH_RE.search(p.path):
        q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING_PARAM_RE.match(k))
        if q:
            key += "?" + urlencode(q)
    return key


def _link_groups(node: Tag, base_url: str, page_key: str, stop_after: int = 0) -> dict[str, list[Tag]]:
    """Product-link candidates under node (node itself when it is a link), grouped by canonical URL.
    With stop_after=N, scanning stops as soon as more than N distinct URLs were seen."""
    anchors = (t for t in node.descendants if isinstance(t, Tag) and t.name == "a" and t.has_attr("href"))
    if node.name == "a" and node.has_attr("href"):
        anchors = itertools.chain([node], anchors)
    groups: dict[str, list[Tag]] = {}
    for a in anchors:
        href = str(a.get("href") or "").strip()
        if not href or _SKIP_LINK.search(href):
            continue
        url = urljoin(base_url, href)
        if not url.lower().startswith(("http://", "https://")):
            continue
        key = _link_key(url)
        if key == page_key:          # back to this listing (sort, paging, "?add=..." buttons)
            continue
        if _CART_TEXT_RE.match(_fold(a.get_text(" ", strip=True) or str(a.get("title") or ""))):
            continue
        groups.setdefault(key, []).append(a)
        if stop_after and len(groups) > stop_after:
            break
    return groups


_MAX_CARD_LINKS = 4      # a card links its product and maybe a brand / seller / shop / "Info" page


def _dominant_link(groups: dict[str, list[Tag]]) -> Optional[tuple[str, list[Tag]]]:
    """A card whose links go to several URLs (product + brand / seller / category link): the one URL whose
    link has a real name (>= 12 chars), else the one URL with an image link."""
    if len(groups) > _MAX_CARD_LINKS:      # a block of many links (menu, footer, the whole grid) is not one card
        return None
    named = [k for k, anchors in groups.items()
             if any(len(t) >= 12 and _usable_title(t) for a in anchors for t in _anchor_texts(a))]
    if len(named) == 1:
        return named[0], groups[named[0]]
    if not named:
        imaged = [k for k, anchors in groups.items() if any(a.find("img") for a in anchors)]
        if len(imaged) == 1:
            return imaged[0], groups[imaged[0]]
    return None


def _card_for(el: Tag, base_url: str, page_key: str) -> Optional[tuple[Tag, str, list[Tag]]]:
    """Climb from a price element to the container of exactly one product: (container, url key, links).

    The container is never a level whose links point to more than one product URL: there the price could
    belong to a neighbouring card's product. Above the container the climb only looks for a better name
    of the same product (a card that also links a shop / brand / "Info" page)."""
    node: Any = el
    best: Optional[tuple[Tag, str, list[Tag]]] = None
    for _ in range(8):
        if not isinstance(node, Tag) or node.name in ("body", "html", "[document]"):
            break
        groups = _link_groups(node, base_url, page_key, stop_after=_MAX_CARD_LINKS)
        if len(groups) == 1:
            (key, anchors), = groups.items()
            best = (node, key, anchors)
            if len(_best_title(anchors, node)) >= 12:
                break
        elif len(groups) > 1:
            dom = _dominant_link(groups)
            if best is None:   # the first level with links already has several: card with extra links
                if dom:
                    best = (node, dom[0], dom[1])
                break
            if dom:
                if dom[0] == best[1]:   # same product, named better one level up: keep the inner container
                    best = (best[0], best[1], dom[1])
                break
            if len(groups) > _MAX_CARD_LINKS or len(_best_title(best[2], best[0])) >= 12:
                break
        node = node.parent
    return best


def heuristic_cards(soup: BeautifulSoup, base_url: str, store: str, min_cards: int = 3) -> list[Offer]:
    """Find repeated containers with a product link and a 'NNN lei' price."""
    found: dict[str, Offer] = {}
    seen_containers: set[int] = set()
    page_key = _link_key(base_url)
    for el, _, ctx in _price_elements(soup):
        if _NOT_PRICE_CONTEXT.search(ctx):   # "208 lei/luna", "livrare gratuita peste 200 lei": not a card price
            continue
        card = _card_for(el, base_url, page_key)
        if card is None:
            continue
        container, key, anchors = card
        if id(container) in seen_containers or key in found:
            continue
        seen_containers.add(id(container))
        title = _best_title(anchors, container)
        if len(title) < 4 or not re.search(r"[^\W\d_]", title):
            continue
        # prices inside this container
        cur: list[float] = []
        old: list[float] = []
        units: list[float] = []
        for par, p, ctx in _price_elements(container):
            if _NOT_PRICE_CONTEXT.search(ctx):
                continue
            if _is_unit_price(ctx, p):
                units.append(p)
                continue
            (old if _is_old_price_node(par, stop=container) else cur).append(p)
        cur = cur or units[:1]       # goods sold by length / area show only a unit price
        if not cur:
            continue
        price = cur[0]
        old_price = max(old) if old else (max(cur) if len(cur) > 1 and max(cur) > price * 1.02 else None)
        url = urldefrag(urljoin(base_url, str(anchors[0]["href"]).strip()))[0]   # "/p/1#reviews" -> "/p/1"
        ctext = container.get_text(" ", strip=True).lower()
        stock: Optional[bool] = None
        if any(x in ctext for x in ("stoc epuizat", "indisponibil", "nu este in stoc", "nu este în stoc", "out of stock")):
            stock = False
        elif any(x in ctext for x in ("în stoc", "in stoc", "disponibil", "stoc limitat", "ultimul produs")):
            stock = True
        found[key] = Offer(
            store=store, title=title, url=url, price=price,
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
    """Try every generic strategy and return the first that yields a plausible listing.

    When no strategy reaches min_listing, the first non-empty structured result (JSON-LD, microdata,
    embedded JSON) is returned: a search with a single hit is still a valid result."""
    soup = soup or make_soup(html)
    best_structured: list[Offer] = []
    for fn in (offers_from_jsonld, offers_from_microdata):
        offers = fn(soup, base_url, store)
        if len(offers) >= min_listing:
            return offers
        if offers and not best_structured:
            best_structured = offers
    best: list[Offer] = []
    for blob in embedded_json_blobs(html, soup):
        offers = offers_from_json_blob(blob, base_url, store)
        if len(offers) > len(best):
            best = offers
    if len(best) >= min_listing:
        return best
    if best and not best_structured:
        best_structured = best
    if allow_heuristic:
        offers = heuristic_cards(soup, base_url, store)
        if offers:
            return offers
    return best_structured


# --------------------------------------------------------------------------- product page

def _ld_page_products(soup: BeautifulSoup) -> tuple[list[dict], bool]:
    """Top-level JSON-LD Product / ProductGroup objects (also a WebPage's mainEntity), never ItemList /
    OfferCatalog members (related products, a category page we were redirected to).
    Second value: the page has an ItemList of products."""
    out: list[dict] = []
    has_list = False
    for obj in iter_jsonld(soup):
        types = _types(obj)
        if "product" in types or "productgroup" in types:
            out.append(obj)
        elif "itemlist" in types or "offercatalog" in types:
            has_list = has_list or bool(obj.get("itemListElement"))
        else:  # WebPage / ItemPage {"mainEntity": {"@type": "Product", ...}}
            ent = obj.get("mainEntity")
            if isinstance(ent, dict) and _types(ent) & {"product", "productgroup"}:
                out.append(ent)
    return out, has_list


def _ld_urls(obj: dict, *keys: str) -> list[str]:
    out = []
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            v = v.get("url") or v.get("@id")
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _same_page(a: str, b: str) -> bool:
    """Same URL including the query (Shopify-style ?variant=...)."""
    try:
        return _page_key(a, a) == _page_key(b, b) and urlsplit(a).query == urlsplit(b).query
    except ValueError:
        return False


def _ld_page_offer(obj: dict, url: str, store: str, page_urls: list[str]) -> Optional[Offer]:
    o = offer_from_ld_product(obj, url, store)
    if o is not None or "productgroup" not in _types(obj):
        return o
    # ProductGroup without its own offers: the variant this URL points to, or the common price of all variants
    variants = obj.get("hasVariant")
    variants = [v for v in (variants if isinstance(variants, list) else [variants]) if isinstance(v, dict)]
    priced = []
    for v in variants:
        vo = offer_from_ld_product({**v, "name": v.get("name") or obj.get("name")}, url, store)
        if vo is not None:
            priced.append((vo, v))
    for vo, v in priced:
        vu = _ld_urls(v, "url")
        if vu and any(_same_page(urljoin(url, vu[0]), p) for p in page_urls):
            return vo
    if priced and len({vo.price for vo, _ in priced}) == 1:
        return priced[0][0]
    return None


def _page_key(u: str, base: str) -> str:
    """Loose identity of a page URL: no scheme, www./m., query, fragment, trailing slash or %-encoding."""
    try:
        p = urlsplit(urljoin(base, u.strip()))
    except ValueError:
        return u
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    return host + (unquote(p.path).rstrip("/").lower() or "/")


def _page_keys(soup: BeautifulSoup, url: str, requested_url: Optional[str]) -> set[str]:
    keys = {_page_key(url, url)}
    if requested_url:
        keys.add(_page_key(requested_url, url))
    link = soup.find("link", rel="canonical", href=True)
    if link:
        keys.add(_page_key(str(link["href"]), url))
    og = _meta(soup, "og:url")
    if og:
        keys.add(_page_key(og, url))
    return keys


def _title_key(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", _fold(text)).split())


def _page_titles(soup: BeautifulSoup) -> list[str]:
    """Names this page gives itself: its <h1> headings, og:title and <title>."""
    out = [h.get_text(" ", strip=True) for h in soup.find_all("h1", limit=3)]
    out.append(_meta(soup, "og:title") or "")
    if soup.title:
        out.append(soup.title.get_text(" ", strip=True))
    return [k for k in (_title_key(t) for t in out) if k]


def _named_like_page(cands: list[Offer], titles: list[str]) -> Optional[Offer]:
    """The one candidate named like the page: equal to its h1 / og:title / <title>, else the longest name
    contained in one of them ('Apple iPhone 15 Pro 256GB' in 'Apple iPhone 15 Pro 256GB | Shop')."""
    keyed = [(o, _title_key(o.title)) for o in cands]
    exact = [o for o, k in keyed if k in titles]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    inside = [(o, k) for o, k in keyed if len(k) >= 8 and " " in k and any(f" {k} " in f" {t} " for t in titles)]
    if not inside:
        return None
    inside.sort(key=lambda ok: len(ok[1]), reverse=True)
    if len(inside) == 1 or len(inside[0][1]) > len(inside[1][1]):
        return inside[0][0]
    return None


def _pick_page_offer(cands: list[tuple[Offer, list[str], list[str]]], keys: set[str], url: str,
                     titles: list[str]) -> tuple[Optional[Offer], bool]:
    """Choose the page's own product among top-level candidates (offer, explicit urls, other ids).

    Returns (offer, ambiguous): a URL match wins; otherwise a single candidate, copies of one product, the
    one named like the page (h1 / og:title / <title>), or the only one that does not link to another page.
    Several different products none of which is this page's look like a listing -> (None, True)."""
    if not cands:
        return None, False
    for o, explicit, ids in cands:
        if {_page_key(u, url) for u in explicit + ids} & keys:
            return o, False
    if len(cands) == 1:
        return cands[0][0], False
    if len({_title_key(o.title) for o, _, _ in cands}) == 1:      # the same product described twice
        return cands[0][0], False
    named = _named_like_page([o for o, _, _ in cands], titles)
    if named is not None:
        return named, False
    unlinked = [o for o, explicit, _ in cands if not explicit]
    if len(unlinked) == 1:
        return unlinked[0], False
    return None, True


def _meta(soup: BeautifulSoup, prop: str) -> Optional[str]:
    m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    v = m.get("content") if m else None
    return str(v).strip() if v and str(v).strip() else None


def extract_product_page(html: str, url: str, store: str, *, requested_url: Optional[str] = None) -> Optional[Offer]:
    """Single product page -> Offer (JSON-LD, microdata, meta tags).

    Only the page's own product counts: top-level JSON-LD Product / ProductGroup and microdata Product
    scopes that are not inside a list or another product. When there are several, the one whose url / @id
    matches this page (url, requested_url, <link rel=canonical>, og:url) wins, then the one named like the
    page (h1 / og:title / <title>). Related-product lists and a category page we were redirected to never
    yield a product (requested_url: the URL the watch asked for, before redirects)."""
    soup = make_soup(html)
    keys = _page_keys(soup, url, requested_url)
    page_urls = [url] + ([requested_url] if requested_url else [])
    ld_objs, has_list = _ld_page_products(soup)
    cands = []
    for obj in ld_objs:
        o = _ld_page_offer(obj, url, store, page_urls)
        if o is not None:
            offers = obj.get("offers") if isinstance(obj.get("offers"), dict) else {}
            cands.append((o, _ld_urls(obj, "url") + _ld_urls(offers, "url"), _ld_urls(obj, "@id")))
    titles = _page_titles(soup)
    o, ambiguous = _pick_page_offer(cands, keys, url, titles)
    if o:
        o.url = url
        return o
    md = [(o, [u] if u else [], []) for o, u in _md_products(soup, url, store, top_level_only=True)]
    o, md_ambiguous = _pick_page_offer(md, keys, url, titles)
    if o:
        o.url = url
        return o
    listing_like = ambiguous or md_ambiguous or (has_list and not ld_objs)
    # meta tags: rejected when the page declares a currency other than RON
    currencies = [c.upper() for c in (_meta(soup, "product:price:currency"), _meta(soup, "og:price:currency")) if c]
    md_cur = soup.find(attrs={"itemprop": "priceCurrency"})
    if md_cur is not None:
        c = str(md_cur.get("content") or md_cur.get_text(strip=True) or "").upper()
        if c:
            currencies.append(c)
    foreign = bool(currencies) and not any(c in ("RON", "LEI") for c in currencies)
    price = None
    for prop, cur_prop in (("product:price:amount", "product:price:currency"),
                           ("og:price:amount", "og:price:currency"), ("twitter:data1", None)):
        cur = _meta(soup, cur_prop) if cur_prop else None
        # an amount whose own currency is foreign, or that has none on a page declaring only a foreign one
        if (cur and cur.upper() not in ("RON", "LEI")) or (not cur and foreign):
            continue
        content = _meta(soup, prop)
        if content:
            price = parse_price(content)
            if price:
                break
    if price is None and not foreign and not listing_like:
        # Magento price box / bare itemprop=price, never one inside a product list or a related-product scope
        el = next((t for t in itertools.chain(soup.find_all(attrs={"data-price-type": "finalPrice"}),
                                              soup.find_all(attrs={"itemprop": "price"}))
                   if not _md_in_list_scope(t)), None)
        if el is not None:
            price = parse_price(el.get("data-price-amount") or el.get("content") or element_price_text(el))
    if price is None:
        return None
    title = _meta(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else "") or url
    avail = soup.find("meta", attrs={"property": re.compile(r"(product|og):availability")})
    return Offer(store=store, title=title.strip(), url=url, price=price,
                 in_stock=availability_flag(avail.get("content")) if avail else None, source="meta")
