"""eMAG (emag.ro) — biggest RO marketplace.

Search: GET https://www.emag.ro/search/{quote(q, safe='')}   page N: /search/{q}/p{N}/c
Data, in order of preference:
  1. EM.listingGlobals.items = [...]  (JSON: id, name, url.path, offer.price.current,
     offer.price.lowest_price_30_days.amount, offer.vendor.name.default, offer.availability.text)
  2. cards div.card-item.js-product-data: data-product-id, data-name, data-url, data-availability-id
     (3 = in stock, 2 = last item), button.add-to-favorites[data-product] JSON {"price": 248, "pnk": ...},
     p.product-new-price "3.999<sup><small>,</small>99</sup> Lei", struck old price in p.pricing s
  3. generic extraction
Anti-bot: own profiling (AID cookie), reported to block GitHub-hosted runner IPs intermittently.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..extract import make_soup
from ..models import Offer
from ..net import BlockedError
from ..prices import element_price, parse_price
from .base import Store

_PNK_RE = re.compile(r"/(?:pd|fd)/([A-Z0-9]{5,20})(?:/|$)")


def _clean_url(url: str) -> str:
    p = urlsplit(urljoin("https://www.emag.ro/", url))
    return urlunsplit((p.scheme or "https", p.netloc or "www.emag.ro", p.path, "", ""))


def _pnk(url: str) -> Optional[str]:
    m = _PNK_RE.search(url or "")
    return m.group(1) if m else None


def _listing_items(html: str) -> Optional[list]:
    i = html.find("EM.listingGlobals.items")
    if i < 0:
        return None
    j = html.find("=", i)
    k = html.find("[", j)
    if j < 0 or k < 0 or k - j > 20:
        return None
    try:
        items, _ = json.JSONDecoder().raw_decode(html, k)
    except ValueError:
        return None
    return items if isinstance(items, list) else None


def _dig(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


class EmagStore(Store):
    key = "emag"
    name = "eMAG"
    base_url = "https://www.emag.ro"
    domains = ("emag.ro",)
    categories = ("general", "electronics", "pc", "home", "appliances", "fashion", "beauty", "diy", "toys", "books")
    page_size = 60
    min_delay = 4.0
    extra_headers = {"Referer": "https://www.emag.ro/", "Upgrade-Insecure-Requests": "1"}

    def __init__(self, http, *, max_pages: int = 1) -> None:
        super().__init__(http, max_pages=max_pages)
        self._warm = False

    def _warmup(self) -> None:
        if self._warm:
            return
        self._warm = True
        try:  # sets EMAGVISITOR / AID cookies like a real visit
            self.http.get(self.base_url + "/")
        except BlockedError:
            raise
        except Exception:
            pass

    def search_page(self, query: str, page: int) -> list[Offer]:
        self._warmup()
        q = self.q_path(query)
        url = f"{self.base_url}/search/{q}" if page == 1 else f"{self.base_url}/search/{q}/p{page}/c"
        html, soup, final = self.get_html(url)
        if page > 1 and final.rstrip("/") != url.rstrip("/") and "/p" not in final:
            return []  # past the last page eMAG redirects
        items = _listing_items(html)
        by_id: dict[str, dict] = {}
        for it in items or []:
            if isinstance(it, dict) and it.get("id") is not None:
                by_id[str(it["id"])] = it

        cards = [c for c in soup.select("div.card-item[data-product-id]")
                 if "rec-card-item" not in (c.get("class") or [])]
        if not cards and items is None:
            if "card-v2" not in html and "listingGlobals" not in html:
                title = soup.title.get_text(strip=True) if soup.title else ""
                if any(w in title.lower() for w in ("robot", "verify", "captcha", "acces")):
                    raise BlockedError(f"blocked page: {title[:60]}", 200, url)
            out = self.fallback(html, final, soup)
            return out

        out: list[Offer] = []
        seen: set[str] = set()
        for c in cards:
            o = self._from_card(c, by_id.get(str(c.get("data-product-id"))))
            if o and o.key not in seen:
                seen.add(o.key)
                out.append(o)
        if not out and by_id:
            for it in by_id.values():
                o = self._from_item(it)
                if o and o.key not in seen:
                    seen.add(o.key)
                    out.append(o)
            self.last_strategy = "listing-json"
        else:
            self.last_strategy = "cards+json" if by_id else "cards"
        if not out and not cards and not by_id:
            out = self.fallback(html, final, soup)
        return out

    # ------------------------------------------------------------------ parsing
    def _from_item(self, it: dict, base: Optional[Offer] = None) -> Optional[Offer]:
        offer = it.get("offer") or {}
        price = parse_price(_dig(offer, "price", "current"))
        name = it.get("name")
        path = _dig(it, "url", "path") or _dig(it, "url", "desktop_base")
        if not price or not name or not path:
            return None
        url = _clean_url(path)
        return self._make(name, url, price, offer, it)

    def _make(self, name: str, url: str, price: float, offer: dict, it: Optional[dict],
              stock: Optional[bool] = None, old: Optional[float] = None) -> Offer:
        if offer:
            lp = _dig(offer, "price", "lowest_price_30_days")
            if isinstance(lp, dict) and lp.get("is_visible", True):
                lp_amount = parse_price(lp.get("amount"))
                if lp_amount and lp_amount > price:
                    old = lp_amount
            avail = _dig(offer, "availability", "code") or _dig(offer, "availability", "text") or ""
            a = str(avail).lower()
            if any(x in a for x in ("out_of_stock", "epuizat", "indisponibil", "unavailable")):
                stock = False
            elif any(x in a for x in ("in_stock", "în stoc", "in stoc", "limited", "ultimul", "ultimele")):
                stock = True
        seller = _dig(offer, "vendor", "name", "default") if offer else None
        if isinstance(seller, str) and seller.strip().lower() == "emag":
            seller = None
        pnk = _pnk(url) or (it or {}).get("part_number_key")
        return Offer(
            store=self.key, title=" ".join(str(name).split()), url=url, price=price,
            old_price=old if old and old > price else None, in_stock=stock,
            offer_id=str(pnk) if pnk else (str(it.get("id")) if it and it.get("id") else None),
            seller=seller if isinstance(seller, str) else None, source="emag",
        )

    def _from_card(self, c, item: Optional[dict]) -> Optional[Offer]:
        a = c.select_one("a.card-v2-title") or c.select_one("a.js-product-url[href]")
        name = (a.get_text(" ", strip=True) if a else "") or c.get("data-name") or ""
        href = c.get("data-url") or (a.get("href") if a else None)
        if not name or not href:
            return None
        url = _clean_url(href)
        price: Optional[float] = None
        fav = c.select_one("button.add-to-favorites[data-product]")
        if fav is not None:
            try:
                price = parse_price(json.loads(fav["data-product"]).get("price"))
            except (ValueError, TypeError, AttributeError):
                price = None
        if not price:
            price = element_price(c.select_one("p.product-new-price"))
        if not price and item:
            price = parse_price(_dig(item, "offer", "price", "current"))
        if not price:
            return None
        old = None
        for s in c.select("p.pricing s, p.product-old-price s, .rrp-lp30d-content s"):
            v = element_price(s)
            if v and v > price:
                old = v
                break
        stock: Optional[bool] = None
        av = c.get("data-availability-id")
        if av in ("2", "3"):
            stock = True
        txt_el = c.select_one("[class*='text-availability-']")
        if txt_el is not None:
            cls = " ".join(txt_el.get("class") or [])
            if "out_of_stock" in cls:
                stock = False
            elif "in_stock" in cls or "limited_stock" in cls:
                stock = True
        return self._make(name, url, price, (item or {}).get("offer") or {}, item, stock=stock, old=old)

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        o = super().product(url)
        if o is not None:
            o.offer_id = _pnk(url) or o.offer_id
            raw = self.last_raw or ""
            m = re.search(r"EM\.productDiscountedPrice\s*=\s*([\d.]+)", raw)
            full = re.search(r"EM\.productFullPrice\s*=\s*([\d.]+)", raw)
            if m and full:
                cur, fp = float(m.group(1)), float(full.group(1))
                if fp > cur and abs(cur - o.price) < 0.5:
                    o.old_price = fp
            return o
        if not self.last_raw:
            return None
        soup = make_soup(self.last_raw)
        price = element_price(soup.select_one("p.product-new-price"))
        h1 = soup.select_one("h1.page-title") or soup.find("h1")
        if price and h1:
            return Offer(store=self.key, title=h1.get_text(" ", strip=True), url=url, price=price,
                         offer_id=_pnk(url), store_name=self.name, source="emag-pdp")
        return None
