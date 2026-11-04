"""MODIVO S.A. (ex-eobuwie) storefronts: MODIVO (modivo.ro, fashion) and ePantofi (epantofi.ro, shoes & bags).

Both shops run the same MODIVO e-commerce platform as eobuwie.pl / modivo.pl (Nuxt storefront behind Cloudflare),
so one adapter class serves both; the two registered subclasses only set host / name / categories.

Search: GET https://{host}/s/{q}?q={q}   q = encodeURIComponent(query): spaces -> %20, ' ( ) ! * left as they are
        (libergent's direct-HTML adapters use this route for both shops; live checks 2026-09-15: 4/3 accepted
        epantofi and 9/8 modivo offers for "adidas samba" / "nike air force"; 2026-10-14: 72 epantofi cards).
        Page N>1 only through the page's rel=next / pager link (NextPager), never a guessed template.
Cards (libergent src/parsers/epantofi.js, 2026-10): <li data-product-card="..."> holding
    a.product-card-link[href^="/p/"]  https://epantofi.ro/p/{slug}[-{13-digit sku}]   (sku suffix -> offer id)
    img[alt]                          "adidas Sneakers adidas Samba Og JR0909 Alb"   (brand + product name; the
                                      leading brand repeated in the name is dropped -> "Sneakers adidas Samba ...")
    div.price-wrapper > span          "406,90" + "Lei"   (current price, Romanian format)
  Brand / name elements are used when a card has them. Old price: struck / "old"-class element or labelled
  Omnibus line ("Cel mai mic preț din ultimele 30 de zile") in the card - selector unverified, diagnostics will tell.
Embedded state (when a page carries no cards): products of the platform's search API (eobuwie/modivo
  /t-api/rest/search/{channel}/v4|v5/search*, see ShoeUp eobuwie.py and StyleMeAPI's modivo client):
  products[].values{nazwa_wyswietlana(_front), manufacturer, final_price.value[locale][currency].amount, price,
  omnibus_price, url_key.value[locale], images}, read from a Nuxt 3 __NUXT_DATA__ payload (devalue) or a JSON
  state script. The API itself is not called: its free-text query parameter is undocumented.
Then the generic JSON-LD / embedded-JSON / heuristic extraction (Store.fallback).
Product page: JSON-LD / meta (generic). Anti-bot: Cloudflare (modivo.pl sets _cfuvid) - a challenge page served
with HTTP 200 raises BlockedError.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup, Tag

from ..extract import embedded_json_blobs, extract_product_page, make_soup, offers_from_json_blob
from ..models import Offer
from .base import Store
from ._fashion import (
    NextPager, best_old, clean_url, dedupe, first_el, fold, higher_money_in, image_of, join_title, json_at,
    labeled_prices, landed_on_home, money_values, next_page_url, norm, num, old_price_in, outermost, pick_price,
    raise_if_challenge, stock_from_text, text_of,
)

_SKU_RE = re.compile(r"-(\d{9,})/?$")
_LOCALE_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_NUXT_ASSIGN_RE = re.compile(r"(?:window\.)?__NUXT__\s*=\s*(?=\{)")
CARD_SELECTOR = ("[data-product-card], [data-test-id='product-card'], li.product-card, article.product-card, "
                 "div.product-card")
_PRICE_SELECTORS = (".price-wrapper", "[class*='final-price']", "[class*='price-final']", "[data-test-id*='price']",
                    "[class*='price']")
_BOX_SELECTORS = ("[class*='prices']", "[class*='price-box']", "[class*='price-container']",
                  "[class*='price-section']")
_NOT_NAME_RE = re.compile(r"brand|badge|label|flag|tag|promo|discount|size|color|colour")
_NAME_KEYS = ("nazwa_wyswietlana_front", "nazwa_wyswietlana", "name", "short_name", "model")
_BRAND_KEYS = ("manufacturer", "brand", "producent", "marka")


def modivo_sku(url: Optional[str]) -> Optional[str]:
    """13-digit sku at the end of a /p/ URL (".../sneakers-adidas-samba-og-jr0909-alb-0000305636416")."""
    m = _SKU_RE.search(urlsplit(url or "").path)
    return m.group(1) if m else None


def drop_repeated_prefix(title: str) -> str:
    """"adidas Sneakers adidas Samba Og" -> "Sneakers adidas Samba Og": the card alt is brand + a product name that
    already names the brand. Only a leading 1-3 word group that recurs later as whole words is dropped."""
    words = norm(title).split()
    for k in (3, 2, 1):
        if len(words) <= k + 1:
            continue
        head, rest = fold(" ".join(words[:k])), fold(" ".join(words[k:]))
        if re.search(r"(?<![a-z0-9])" + re.escape(head) + r"(?![a-z0-9])", rest):
            return " ".join(words[k:])
    return norm(title)


# --------------------------------------------------------------------------- Nuxt 3 payload

def nuxt_unflatten(values: Any) -> Any:
    """Decode a Nuxt 3 __NUXT_DATA__ payload (devalue: a flat array whose containers hold indices into it)."""
    if not isinstance(values, list) or not values:
        return None
    memo: dict[int, Any] = {}

    def hyd(i: Any, depth: int) -> Any:
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(values) or depth > 150:
            return None
        if i in memo:
            return memo[i]
        v = values[i]
        if isinstance(v, dict):
            out: Any = {}
            memo[i] = out
            for k, x in v.items():
                out[k] = hyd(x, depth + 1)
            return out
        if isinstance(v, list):
            if v and isinstance(v[0], str):          # ["Reactive", i], ["Set", ...], ["Map", k, v, ...], ["Date", s]
                tag = v[0]
                if tag == "Set":
                    out = []
                    memo[i] = out
                    out.extend(hyd(x, depth + 1) for x in v[1:])
                    return out
                if tag in ("Map", "null"):
                    out = {}
                    memo[i] = out
                    for k, x in zip(v[1::2], v[2::2]):
                        out[str(hyd(k, depth + 1) if tag == "Map" else k)] = hyd(x, depth + 1)
                    return out
                if tag in ("Date", "BigInt", "RegExp", "URL"):
                    memo[i] = v[1] if len(v) > 1 else None
                    return memo[i]
                memo[i] = hyd(v[1], depth + 1) if len(v) > 1 else None   # Reactive / Ref / ShallowReactive ...
                return memo[i]
            out = []
            memo[i] = out
            out.extend(hyd(x, depth + 1) for x in v)
            return out
        memo[i] = v
        return v

    try:
        return hyd(0, 0)
    except RecursionError:
        return None


def state_blobs(html: str, soup: BeautifulSoup) -> list[Any]:
    """JSON state of the page: decoded __NUXT_DATA__, a JSON-literal window.__NUXT__ and other JSON scripts."""
    blobs: list[Any] = []
    for sc in soup.find_all("script"):
        if sc.get("id") == "__NUXT_DATA__" or sc.has_attr("data-nuxt-data"):
            try:
                data = json.loads(sc.string or "", strict=False)
            except ValueError:
                continue
            blobs.append(nuxt_unflatten(data) if isinstance(data, list) else data)
    for m in _NUXT_ASSIGN_RE.finditer(html or ""):
        v = json_at(html, m.end())
        if v is not None:
            blobs.append(v)
    blobs.extend(embedded_json_blobs(html, soup))
    return [b for b in blobs if isinstance(b, (dict, list))]


def api_products(obj: Any) -> Iterator[dict]:
    """Search-API shaped products ({"values": {"url_key": ..., "final_price": ...}}) anywhere in `obj`."""
    seen: set[int] = set()
    stack = [(obj, 0)]
    while stack:
        cur, depth = stack.pop()
        if not isinstance(cur, (dict, list)) or id(cur) in seen or depth > 60:
            continue
        seen.add(id(cur))
        if isinstance(cur, dict):
            vals = cur.get("values")
            if isinstance(vals, dict) and "url_key" in vals and ("final_price" in vals or "price" in vals):
                yield cur
                continue
            children = list(cur.values())
        else:
            children = list(cur)
        stack.extend((c, depth + 1) for c in reversed(children))


def _unwrap(v: Any, locale: str, currency: str) -> Any:
    """{"value": {"ro_RO": {"RON": {"amount": 406.9}}}} -> 406.9;  {"value": {"label": "adidas"}} -> "adidas"."""
    for _ in range(8):
        if isinstance(v, list):
            v = v[0] if v else None
            continue
        if not isinstance(v, dict):
            return v
        if "value" in v:
            v = v["value"]
        elif locale in v:
            v = v[locale]
        elif any(_LOCALE_RE.match(str(k)) for k in v):
            v = next(v[k] for k in v if _LOCALE_RE.match(str(k)))
        elif any(_CURRENCY_RE.match(str(k)) for k in v):
            v = v.get(currency)                     # an amount in another currency is not a RON price
        elif "amount" in v:
            v = v["amount"]
        elif "label" in v or "name" in v:
            v = v.get("label") or v.get("name")
        else:
            return None
    return None


# --------------------------------------------------------------------------- store

class ModivoPlatformStore(NextPager, Store):
    """Shared adapter of the MODIVO platform shops (abstract: no key, not registered)."""

    page_size = 60
    min_delay = 2.0
    locale = "ro_RO"
    currency = "RON"

    def search_url(self, query: str) -> str:
        q = quote(query.strip(), safe="!*'()")      # encodeURIComponent
        return f"{self.base_url}/s/{q}?q={q}"

    def _own(self, url: str) -> bool:
        host = urlsplit(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page == 1:
            self._pager_reset()
            url = self.search_url(query)
        else:
            url = self._pager_get(page)
            if not url:
                return []
        html, soup, final = self.get_html(url)
        if landed_on_home(url, final):
            raise_if_challenge(html, soup, final)
            self.last_strategy = "redirect:home"
            return []
        if page == 1 and urlsplit(final).path.startswith("/p/"):
            o = self.product_from_html(html, final)    # exact query -> straight to the product page
            if o is not None:
                self.last_strategy = "product-page"
                return [o]
        out = self.parse_listing(html, soup, final)
        if out:
            nxt = next_page_url(soup, final)
            if nxt and self._own(nxt):
                self._pager_set(page + 1, nxt)
            return out
        raise_if_challenge(html, soup, final)
        return self.fallback(html, final, soup)

    def parse_listing(self, html: str, soup: BeautifulSoup, url: str) -> list[Offer]:
        out = dedupe(self._card(c) for c in outermost(soup.select(CARD_SELECTOR)))
        if out:
            self.last_strategy = "cards"
            return out
        blobs = state_blobs(html, soup)
        out = dedupe(self._api_product(p) for b in blobs for p in api_products(b))
        if out:
            self.last_strategy = "nuxt-state"
            return out
        for b in blobs:
            out = [o for o in offers_from_json_blob(b, url, self.key) if self._own(o.url)]
            if out:
                for o in out:
                    o.url = clean_url(o.url, url) or o.url
                    o.offer_id = modivo_sku(o.url) or o.offer_id
                self.last_strategy = "state-json"
                return dedupe(out)
        return []

    # ------------------------------------------------------------------ cards
    def _card(self, c: Tag) -> Optional[Offer]:
        url, link = "", None
        for a in ([c] if c.name == "a" else []) + c.select("a.product-card-link[href], a[href]"):
            u = clean_url(a.get("href"), self.base_url)
            if u and self._own(u) and urlsplit(u).path.startswith("/p/"):
                url, link = u, a
                break
        if not url:
            return None                               # banner / category tile
        brand = text_of(first_el(c, "[data-test-id*='brand']", "[class*='brand']", "[class*='manufacturer']"))
        name = ""
        for el in c.select("[data-test-id*='name'], [class*='name'], [class*='title']"):
            marks = (" ".join(el.get("class") or []) + " " + (el.get("data-test-id") or "")).lower()
            t = text_of(el)
            if el.name != "img" and not _NOT_NAME_RE.search(marks) and t and not money_values(t):
                name = t
                break
        img = c.select_one("img[alt]")
        alt = norm(img.get("alt")) if img is not None else ""
        # epantofi cards carry the title only in the image alt (brand + product name; libergent's parser reads only
        # it); a visible name element of 2+ words wins unless the alt contains it (the alt then says more)
        title = join_title(brand, name) if len(name.split()) >= 2 else ""
        if len(alt.split()) >= 2 and (not title or join_title(title, alt) == alt.strip(" -–|,;")):
            title = join_title(brand, alt)
        if not title:
            title = join_title(brand, name)
        if not title:
            title = join_title(brand, alt or norm(link.get("title") if link is not None else ""))
        title = drop_repeated_prefix(title)
        if not title:
            return None
        price_el, price = pick_price(c, *_PRICE_SELECTORS)
        if not price:
            return None
        box = first_el(c, *_BOX_SELECTORS)
        if box is None or not any(p is box for p in price_el.parents):
            box = price_el.parent if price_el.parent is not None and price_el.parent is not c else price_el
        old = old_price_in(c, price, exclude=[price_el]) or higher_money_in(box, price)
        orig_lbl, low = labeled_prices(text_of(el) for el in c.find_all(True) if el.find(["div", "p", "li"]) is None)
        if orig_lbl or low:
            old = best_old(price, orig_lbl or old, low)
        stock = False if stock_from_text(text_of(c)) is False else None   # cards list sellable products only
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=modivo_sku(url), image=image_of(c, self.base_url), source=self.key,
        )

    # ------------------------------------------------------------------ embedded search-API products
    def _api_product(self, p: dict) -> Optional[Offer]:
        vals = p.get("values") or {}

        def val(*keys: str) -> Any:
            for k in keys:
                v = _unwrap(vals.get(k), self.locale, self.currency)
                if v not in (None, "", [], {}):
                    return v
            return None

        slug = val("url_key")
        if not isinstance(slug, str) or not slug.strip():
            return None
        slug = slug.strip()
        if "://" in slug:
            url = clean_url(slug, self.base_url)
        else:
            path = slug.lstrip("/")
            url = clean_url(f"/{path}" if path.startswith("p/") else f"/p/{path}", self.base_url)
        if not url or not self._own(url):
            return None
        name = val(*_NAME_KEYS)
        brand = val(*_BRAND_KEYS)
        title = drop_repeated_prefix(join_title(brand if isinstance(brand, str) else "",
                                                name if isinstance(name, str) else ""))
        price = num(val("final_price"))
        if not title or not price:
            return None
        old = best_old(price, num(val("price")), num(val("omnibus_price")))
        image = None
        imgs = vals.get("images")
        imgs = imgs.get("value") if isinstance(imgs, dict) else imgs
        for im in imgs if isinstance(imgs, list) else [imgs]:
            cand = im if isinstance(im, str) else (im.get("url") or im.get("src")) if isinstance(im, dict) else None
            if isinstance(cand, str) and cand.startswith(("http", "//")):
                image = "https:" + cand if cand.startswith("//") else cand
                break
        return Offer(
            # id from the URL only, like the cards: the same product keeps one Offer.key whichever path found it
            store=self.key, title=title, url=url, price=price, old_price=old, in_stock=None,
            offer_id=modivo_sku(url), image=image, source="nuxt-state",
        )

    # ------------------------------------------------------------------ product page
    def product_from_html(self, html: str, url: str) -> Optional[Offer]:
        o = extract_product_page(html, url, self.key)
        if o is not None:
            o.url = clean_url(url, self.base_url) or url
            o.offer_id = modivo_sku(o.url) or o.offer_id
            o.store_name = self.name
        return o

    def product(self, url: str) -> Optional[Offer]:
        resp = self.http.get(url)
        self.last_raw = resp.text
        o = self.product_from_html(resp.text, url)
        if o is None:
            raise_if_challenge(resp.text, make_soup(resp.text), url)
            return None
        o.url = url
        return o


class ModivoStore(ModivoPlatformStore):
    key = "modivo"
    name = "MODIVO"
    base_url = "https://modivo.ro"
    domains = ("modivo.ro",)
    categories = ("fashion", "sport")
    extra_headers = {"Referer": "https://modivo.ro/", "Upgrade-Insecure-Requests": "1"}


class EpantofiStore(ModivoPlatformStore):
    key = "epantofi"
    name = "ePantofi"
    base_url = "https://epantofi.ro"
    domains = ("epantofi.ro",)
    categories = ("fashion", "sport")
    extra_headers = {"Referer": "https://epantofi.ro/", "Upgrade-Insecure-Requests": "1"}
