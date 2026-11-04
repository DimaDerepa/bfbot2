"""Altex + Media Galaxy (same platform: fenrir JSON API, Next.js storefront, Akamai).

API: GET https://fenrir.altex.ro/v2/catalog/search/{q}?size=48&page=N  (q = path segment, %20 for spaces)
products[]: name, sku, url_key, price (current), regular_price, stock_status (1/0), pickup_is_in_stock,
            price_seller_name, small_image.  URL: https://altex.ro/{url_key}/cpd/{sku}/
Note: Akamai may block datacenter / non-RO IPs -> BlockedError (handled by the engine).
"""
from __future__ import annotations

import math
from typing import Optional

from ..models import Offer
from ..prices import element_price_text, parse_price
from .base import Store


class AltexStore(Store):
    key = "altex"
    name = "Altex"
    base_url = "https://altex.ro"
    domains = ("altex.ro",)
    categories = ("electronics", "pc", "appliances", "home", "diy", "toys")
    page_size = 48
    api_host = "https://fenrir.altex.ro"
    brand_header: Optional[str] = None
    image_host = "https://lcdn.altex.ro"

    def _headers(self, q: str) -> dict:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/cauta/?q={q}",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "x-client-type": "web",
        }
        if self.brand_header:
            h["x-brand"] = self.brand_header
        return h

    def search_page(self, query: str, page: int) -> list[Offer]:
        q = self.q_path(query)
        data = self.get_json(f"{self.api_host}/v2/catalog/search/{q}",
                             params={"size": str(self.page_size), "page": str(page)}, headers=self._headers(q))
        products = (data or {}).get("products") or []
        out = [o for o in (self._offer(p) for p in products) if o]
        self.last_strategy = "fenrir-api"
        meta = (data or {}).get("meta") or {}
        try:
            total_pages = int(meta.get("total_pages") or math.ceil(int(meta.get("items", 0)) / max(1, int(meta.get("size") or self.page_size))))
        except (TypeError, ValueError):
            total_pages = 1
        if page >= total_pages:
            self._last_page = True
        return out

    def search(self, query: str) -> list[Offer]:
        self._last_page = False
        out: list[Offer] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            offers = self.search_page(query, page)
            for o in offers:
                if o.key not in seen:
                    seen.add(o.key)
                    o.store_name = self.name
                    out.append(o)
            if self._last_page or not offers:
                break
        return out

    def _image(self, p: dict) -> Optional[str]:
        img = p.get("small_image") or p.get("image") or p.get("thumbnail")
        if not img or not isinstance(img, str):
            return None
        if img.startswith("http"):
            return img
        img = img.lstrip("/")
        if not img.startswith("media/"):
            img = "media/catalog/product/" + img
        return f"{self.image_host}/{img}"

    def _offer(self, p: dict) -> Optional[Offer]:
        price = parse_price(p.get("price"))
        name = p.get("name")
        sku = p.get("sku")
        if not price or not name:
            return None
        old = parse_price(p.get("regular_price"))
        url_key = p.get("url_key")
        url = f"{self.base_url}/{url_key}/cpd/{sku}/" if url_key and sku else self.base_url
        stock = None
        if "stock_status" in p or "pickup_is_in_stock" in p:
            stock = p.get("stock_status") in (1, "1", True) or p.get("pickup_is_in_stock") in (1, "1", True)
        seller = p.get("price_seller_name")
        if seller and seller.strip().lower() in (self.name.lower(), "altex", "media galaxy"):
            seller = None
        return Offer(
            store=self.key, title=str(name).strip(), url=url, price=price,
            old_price=old if old and old > price else None, in_stock=stock,
            offer_id=str(sku) if sku else (str(p.get("id")) if p.get("id") else None),
            image=self._image(p), seller=seller, source="fenrir-api",
        )

    def product(self, url: str) -> Optional[Offer]:
        # /{url_key}/cpd/{SKU}/  -> per-SKU availability endpoint, fallback to page JSON-LD
        if "/cpd/" in url:
            sku = url.split("/cpd/", 1)[1].strip("/")
            try:
                data = self.get_json(f"{self.api_host}/catalog/product/store_availability/{sku}/",
                                     headers=self._headers(""))
                prod = (data or {}).get("product") or {}
                if prod.get("price"):
                    prod.setdefault("sku", sku)
                    o = self._offer(prod)
                    if o:
                        o.url = url
                        o.store_name = self.name
                        return o
            except Exception:
                pass
        o = super().product(url)
        if o is None and self.last_raw:
            # Next.js PDP: .Price-int + sup
            from ..extract import make_soup

            soup = make_soup(self.last_raw)
            el = soup.select_one(".text-red-brand .Price-int") or soup.select_one(".Price-int")
            h1 = soup.find("h1")
            if el is not None and h1 is not None:
                price = parse_price(element_price_text(el.parent))
                if price:
                    o = Offer(store=self.key, title=h1.get_text(" ", strip=True), url=url, price=price,
                              store_name=self.name, source="html")
        return o


class MediaGalaxyStore(AltexStore):
    key = "mediagalaxy"
    name = "Media Galaxy"
    base_url = "https://mediagalaxy.ro"
    domains = ("mediagalaxy.ro",)
    categories = ("electronics", "pc", "appliances", "home")
    api_host = "https://fenrir.altex.ro"   # same gateway, brand selected by header (unverified; diag will tell)
    brand_header = "mediagalaxy"
    image_host = "https://lcdn.mediagalaxy.ro"
