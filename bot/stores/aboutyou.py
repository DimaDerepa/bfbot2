"""ABOUT YOU Romania (aboutyou.ro) - fashion & beauty; SCAYLE / ABOUT YOU backend API ("BAPI").

DISABLED BY DEFAULT: there is no working search route to rely on. The HTML search path is unknown (libergent's
German /suche?term= returned 5 raw items and 0 usable offers on 2026-09-28), the site sits behind Cloudflare plus an
hCaptcha reference, and the JSON API needs the RO shop id, which the research could not find.
When enabled (stores: [aboutyou] in a watch, or default_stores), it works like this:
  1. RO shop id: env var ABOUTYOU_SHOP_ID, else discovered once per process from the home page HTML
     ("shopId": 123 / shopId=123 / x-shop-id). No id -> [] (diagnostics show "bapi:no-shop-id").
  2. GET https://api-cloud.aboutyou.de/v1/products?with=attributes:key(brand|name),priceRange,variants,images
         &filters[term]={q}&sortScore=category_scores&sortChannel=web_default&page={N}&perPage=40&shopId={id}
     (the web frontend called it without a token in 2022; SCAYLE now documents an X-Access-Token, so it may 401/403).
Response (nabil-ak/monitor-service, tjaycodes4you/aboutYouM swagger):
  entities[]: id, attributes.brand.values.label, attributes.name.values.label,
    variants[].price.withTax (current, MINOR units: 19999 = 199.99 lei), price.appliedReductions[].amount
    .absoluteWithTax (original = withTax + reductions), stock.quantity / stock.isSellableWithoutStock,
    images[0].hash -> https://cdn.aboutstatic.com/file/{hash} ("images/" prefix added when missing)
  pagination{current, total, perPage}
  Product URL: https://www.aboutyou.ro/p/{brand-slug}/{name-slug}-{id} (slugs are not validated by the site).
Anti-bot: Cloudflare (__cf_bm, 302 for scripted clients) + hCaptcha on the site; api-cloud was behind Cloudflare
  but answered 200 in 2022 -> >= 4 s between requests, block pages -> BlockedError.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import quote, urlencode

from ..models import Offer
from ..net import BlockedError, FetchError
from .base import Store
from ._fashion import dedupe, dig, join_title, minor, norm, slugify

API_URL = "https://api-cloud.aboutyou.de/v1/products"
SHOP_ENV = "ABOUTYOU_SHOP_ID"
_SHOP_RE = re.compile(r"""(?:["']?shopId["']?\s*[:=]\s*["']?|shopId=|x-shop-id["']?\s*[:=]\s*["']?)(\d{1,6})""", re.I)
_SHOP_CACHE: dict[str, Optional[str]] = {}


def find_shop_id(html: str) -> Optional[str]:
    m = _SHOP_RE.search(html or "")
    return m.group(1) if m else None


def _label(attrs: Any, key: str) -> str:
    v = dig(attrs, key, "values")
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        return norm(v.get("label") or v.get("value"))
    return norm(v) if isinstance(v, str) else ""


class AboutYouStore(Store):
    key = "aboutyou"
    name = "ABOUT YOU"
    base_url = "https://www.aboutyou.ro"
    domains = ("aboutyou.ro",)
    categories = ("fashion", "beauty")
    enabled_by_default = False
    page_size = 40
    min_delay = 4.0
    extra_headers = {"Referer": "https://www.aboutyou.ro/", "Origin": "https://www.aboutyou.ro"}

    def shop_id(self) -> Optional[str]:
        env = norm(os.environ.get(SHOP_ENV))
        if env:
            return env
        if self.key in _SHOP_CACHE:
            return _SHOP_CACHE[self.key]
        sid: Optional[str] = None
        try:
            sid = find_shop_id(self.http.get(self.base_url + "/").text)
        except BlockedError:
            raise
        except FetchError:
            sid = None
        _SHOP_CACHE[self.key] = sid
        return sid

    def search_url(self, query: str, shop_id: str, page: int = 1) -> str:
        params = [
            ("with", "attributes:key(brand|name),priceRange,variants,images"),
            ("filters[term]", query.strip()),
            ("sortScore", "category_scores"),
            ("sortChannel", "web_default"),
            ("page", str(page)),
            ("perPage", str(self.page_size)),
            ("shopId", shop_id),
        ]
        return API_URL + "?" + urlencode(params, quote_via=quote, safe="(|):,")

    def search_page(self, query: str, page: int) -> list[Offer]:
        sid = self.shop_id()
        if not sid:
            self.last_strategy = "bapi:no-shop-id"
            return []
        data = self.get_json(self.search_url(query, sid, page), headers={"Accept": "application/json"})
        entities = data.get("entities") if isinstance(data, dict) else None
        out = dedupe(self._entity(e) for e in entities or [] if isinstance(e, dict))
        self.last_strategy = "bapi"
        return out

    def _entity(self, e: dict) -> Optional[Offer]:
        pid = e.get("id")
        attrs = e.get("attributes") or {}
        brand, name = _label(attrs, "brand"), _label(attrs, "name")
        title = join_title(brand, name)
        if not pid or not title:
            return None
        best: Optional[tuple[bool, float, Optional[float]]] = None
        any_variant = False
        for v in e.get("variants") or []:
            if not isinstance(v, dict):
                continue
            pr = v.get("price") or {}
            cur = str(pr.get("currencyCode") or "RON").upper()
            price = minor(pr.get("withTax"))
            if not price or cur not in ("RON", "LEI"):
                continue
            any_variant = True
            red = 0
            for r in pr.get("appliedReductions") or []:
                amt = dig(r, "amount", "absoluteWithTax")
                if isinstance(amt, int) and not isinstance(amt, bool) and amt > 0:
                    red += amt
            orig = minor(pr.get("originalWithTax")) or (minor(int(pr["withTax"]) + red) if red else None)
            st = v.get("stock") or {}
            qty = st.get("quantity")
            sellable = (isinstance(qty, (int, float)) and qty > 0) or bool(st.get("isSellableWithoutStock"))
            cand = (sellable, price, orig)
            if best is None or (cand[0], -cand[1]) > (best[0], -best[1]):
                best = cand
        if best is None:
            price = minor(dig(e, "priceRange", "min", "withTax"))
            cur = str(dig(e, "priceRange", "min", "currencyCode") or "RON").upper()
            if not price or cur not in ("RON", "LEI"):
                return None
            best = (False, price, None)
        sellable, price, orig = best
        h = dig(e, "images", 0, "hash")
        image = None
        if isinstance(h, str) and h:
            image = "https://cdn.aboutstatic.com/file/" + (h if h.startswith("images/") else "images/" + h)
        url = f"{self.base_url}/p/{slugify(brand) or 'x'}/{slugify(name) or 'x'}-{pid}"
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=orig if orig and orig > price else None,
            in_stock=sellable if any_variant else None, offer_id=str(pid), image=image, source="bapi",
        )


def _reset_shop_cache() -> None:  # tests
    _SHOP_CACHE.clear()
