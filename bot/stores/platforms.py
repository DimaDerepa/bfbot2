"""Generic adapters for common e-commerce platforms.

These power (a) built-in stores that run on a known platform and (b) any custom shop the
user adds in config.yaml under settings.shops:

    shops:
      - key: myshop
        name: My Shop
        platform: shopify | woocommerce | vtex | magento2 | html
        base_url: https://www.myshop.ro
        search_url: https://www.myshop.ro/cautare?q={q}     # only for platform: html (optional for magento2)
        categories: [electronics]
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urljoin

from ..models import Offer
from ..prices import element_price, element_price_text, parse_minor_units, parse_price
from .base import Store


class ShopifyStore(Store):
    """Shopify predictive search: /search/suggest.json (falls back to HTML search page)."""

    page_size = 10

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            return []
        url = f"{self.base_url.rstrip('/')}/search/suggest.json"
        data = self.get_json(url, params={
            "q": query, "resources[type]": "product", "resources[limit]": "10",
            "resources[options][unavailable_products]": "last",
        })
        products = (((data or {}).get("resources") or {}).get("results") or {}).get("products") or []
        out: list[Offer] = []
        for p in products:
            price = parse_price(p.get("price") or p.get("price_min"))
            if not price:
                continue
            old = parse_price(p.get("compare_at_price_max") or p.get("compare_at_price_min"))
            out.append(Offer(
                store=self.key, title=p.get("title") or "", url=self.abs((p.get("url") or "").split("?")[0]),
                price=price, old_price=old if old and old > price else None,
                in_stock=p.get("available") if isinstance(p.get("available"), bool) else None,
                offer_id=str(p.get("id") or p.get("handle") or "") or None,
                image=p.get("image") or (p.get("featured_image") or {}).get("url"),
                source="shopify",
            ))
        self.last_strategy = "shopify"
        if not out:
            html, soup, final = self.get_html(f"{self.base_url.rstrip('/')}/search", params={"q": query, "type": "product"})
            out = self.fallback(html, final, soup)
        return out


class WooCommerceStore(Store):
    """WooCommerce Store API (public, no auth): /wp-json/wc/store/v1/products?search="""

    page_size = 50

    def search_page(self, query: str, page: int) -> list[Offer]:
        url = f"{self.base_url.rstrip('/')}/wp-json/wc/store/v1/products"
        try:
            data = self.get_json(url, params={"search": query, "per_page": str(self.page_size), "page": str(page)})
        except Exception:
            if page > 1:
                return []
            html, soup, final = self.get_html(self.base_url.rstrip("/") + "/", params={"s": query, "post_type": "product"})
            return self.fallback(html, final, soup)
        out: list[Offer] = []
        for p in data if isinstance(data, list) else []:
            prices = p.get("prices") or {}
            mu = prices.get("currency_minor_unit", 2)
            price = parse_minor_units(prices.get("price"), mu)
            if not price:
                continue
            regular = parse_minor_units(prices.get("regular_price"), mu)
            images = p.get("images") or []
            out.append(Offer(
                store=self.key, title=_unescape(p.get("name") or ""), url=p.get("permalink") or "",
                price=price, old_price=regular if regular and regular > price else None,
                in_stock=p.get("is_in_stock") if isinstance(p.get("is_in_stock"), bool) else None,
                offer_id=str(p.get("id")) if p.get("id") else None,
                image=images[0].get("src") if images else None, source="woocommerce",
            ))
        self.last_strategy = "woocommerce"
        return out


class VtexStore(Store):
    """VTEX legacy catalog search: /api/catalog_system/pub/products/search?ft="""

    page_size = 50

    def search_page(self, query: str, page: int) -> list[Offer]:
        frm = (page - 1) * self.page_size
        url = f"{self.base_url.rstrip('/')}/api/catalog_system/pub/products/search"
        data = self.get_json(url, params={"ft": query, "_from": str(frm), "_to": str(frm + self.page_size - 1)})
        out: list[Offer] = []
        for p in data if isinstance(data, list) else []:
            best: Optional[tuple[float, Optional[float], bool]] = None
            for item in p.get("items") or []:
                for seller in item.get("sellers") or []:
                    co = seller.get("commertialOffer") or {}
                    price = parse_price(co.get("Price"))
                    if not price:
                        continue
                    avail = (co.get("AvailableQuantity") or 0) > 0 or bool(co.get("IsAvailable"))
                    cand = (price, parse_price(co.get("ListPrice")), avail)
                    if best is None or (cand[2], -cand[0]) > (best[2], -best[0]):
                        best = cand
            if not best:
                continue
            price, lst, avail = best
            link = p.get("link") or (f"{self.base_url.rstrip('/')}/{p.get('linkText')}/p" if p.get("linkText") else "")
            out.append(Offer(
                store=self.key, title=p.get("productName") or p.get("productTitle") or "", url=link,
                price=price, old_price=lst if lst and lst > price else None, in_stock=avail,
                offer_id=str(p.get("productId") or "") or None, source="vtex",
            ))
        self.last_strategy = "vtex"
        return out


class Magento2Store(Store):
    """Magento 2 HTML search results (data-price-amount attributes), generic fallback."""

    search_path = "/catalogsearch/result/"

    def search_page(self, query: str, page: int) -> list[Offer]:
        params = {"q": query}
        if page > 1:
            params["p"] = str(page)
        html, soup, final = self.get_html(self.base_url.rstrip("/") + self.search_path, params=params)
        out = parse_magento_listing(soup, final, self.key)
        if out:
            self.last_strategy = "magento2"
            return out
        return self.fallback(html, final, soup)


def parse_magento_listing(soup, base_url: str, store: str) -> list[Offer]:
    out: list[Offer] = []
    for item in soup.select("li.product-item, div.product-item, .product-item-info"):
        a = item.select_one("a.product-item-link") or item.select_one(".product-item-name a, .product-name a, h2 a, h3 a")
        if not a or not a.get("href"):
            continue
        fin = item.select_one('[data-price-type="finalPrice"]')
        old = item.select_one('[data-price-type="oldPrice"]')
        price = element_price(fin) if fin else None
        if not price:
            p = item.select_one(".special-price .price, .price-final_price .price, .price")
            price = parse_price(element_price_text(p)) if p else None
        if not price:
            continue
        old_price = element_price(old) if old else None
        pid = item.get("data-product-id")
        if not pid:
            pid_el = item.select_one("[data-product-id]")
            pid = pid_el.get("data-product-id") if pid_el else None
        txt = item.get_text(" ", strip=True).lower()
        stock = False if ("stoc epuizat" in txt or "indisponibil" in txt or "out of stock" in txt) else None
        out.append(Offer(
            store=store, title=a.get_text(" ", strip=True), url=urljoin(base_url, a["href"]), price=price,
            old_price=old_price if old_price and old_price > price else None, in_stock=stock,
            offer_id=str(pid) if pid else None, source="magento2",
        ))
    # Magento renders the same product in several nested .product-item* nodes; dedupe by URL
    seen: set[str] = set()
    uniq = []
    for o in out:
        if o.url in seen:
            continue
        seen.add(o.url)
        uniq.append(o)
    return uniq


class HtmlSearchStore(Store):
    """Any shop: fetch `search_url` (with {q} / {q_plus} / {q_path}) and use generic extraction."""

    search_url: str = ""

    def search_page(self, query: str, page: int) -> list[Offer]:
        if page > 1:
            return []
        url = (self.search_url
               .replace("{q_plus}", self.q_plus(query))
               .replace("{q_path}", self.q_path(query))
               .replace("{q}", self.q_plus(query)))
        html, soup, final = self.get_html(url)
        return self.fallback(html, final, soup)


def _unescape(s: str) -> str:
    import html as _html

    return _html.unescape(s)


PLATFORMS: dict[str, type[Store]] = {
    "shopify": ShopifyStore,
    "woocommerce": WooCommerceStore,
    "vtex": VtexStore,
    "magento2": Magento2Store,
    "magento": Magento2Store,
    "html": HtmlSearchStore,
    "generic": HtmlSearchStore,
}


def make_custom_store(spec: dict) -> type[Store]:
    """Build a Store subclass from a config.yaml `shops:` entry."""
    platform = str(spec.get("platform", "html")).lower()
    base_cls = PLATFORMS.get(platform)
    if base_cls is None:
        raise ValueError(f"shop {spec.get('key')}: неизвестная платформа {platform!r}; варианты: {sorted(PLATFORMS)}")
    base_url = str(spec.get("base_url") or "").rstrip("/")
    search_url = spec.get("search_url") or ""
    if not base_url and search_url:
        from urllib.parse import urlsplit

        u = urlsplit(search_url)
        base_url = f"{u.scheme}://{u.netloc}"
    if not base_url:
        raise ValueError(f"shop {spec.get('key')}: нужен base_url")
    key = str(spec.get("key") or base_url.split("//", 1)[-1].split("/")[0].removeprefix("www.").split(".")[0]).lower()
    attrs: dict[str, Any] = {
        "key": key,
        "name": spec.get("name") or key,
        "base_url": base_url,
        "domains": (base_url.split("//", 1)[-1].split("/")[0].removeprefix("www."),),
        "categories": tuple(spec.get("categories") or ("general",)),
        "enabled_by_default": bool(spec.get("enabled", True)),
    }
    if platform in ("html", "generic"):
        if not search_url:
            raise ValueError(f"shop {key}: для platform: html нужен search_url с {{q}}")
        attrs["search_url"] = search_url
    if platform in ("magento2", "magento") and spec.get("search_path"):
        attrs["search_path"] = spec["search_path"]
    return type(f"Custom_{key}", (base_cls,), attrs)
