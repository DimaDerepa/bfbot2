"""Base class for store adapters.

A store adapter turns a text query into a list of `Offer`s. Implement `search_page(query, page)`
(or override `search`). Use `self.http` for requests (browser TLS fingerprint, retries, throttling)
and `self.fallback(html, url)` to fall back to generic extraction when site-specific parsing fails.
"""
from __future__ import annotations

import logging
from typing import ClassVar, Optional
from urllib.parse import quote, quote_plus, urljoin

from bs4 import BeautifulSoup

from ..extract import extract_offers, extract_product_page, make_soup
from ..models import Offer
from ..net import Http

log = logging.getLogger("bot.stores")


class Store:
    key: ClassVar[str] = ""
    name: ClassVar[str] = ""
    base_url: ClassVar[str] = ""
    domains: ClassVar[tuple[str, ...]] = ()       # for matching product URLs to this store
    categories: ClassVar[tuple[str, ...]] = ()    # electronics, pc, home, appliances, fashion, beauty, general, diy, toys, books
    aggregator: ClassVar[bool] = False            # price comparison site (covers many shops)
    enabled_by_default: ClassVar[bool] = True
    page_size: ClassVar[int] = 40
    min_delay: ClassVar[float] = 0.0              # store-specific minimum pause between requests (s)
    extra_headers: ClassVar[dict] = {}

    def __init__(self, http: Http, *, max_pages: int = 1) -> None:
        self.http = http
        self.max_pages = max(1, int(max_pages))
        self.last_strategy = ""
        self.last_url = ""
        self.last_raw: Optional[str] = None      # raw body of the last search response (for diagnostics)

    # ------------------------------------------------------------------ API
    def search(self, query: str) -> list[Offer]:
        out: list[Offer] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            offers = self.search_page(query, page)
            new = [o for o in offers if o.key not in seen]
            for o in new:
                seen.add(o.key)
                if not o.store_name:
                    o.store_name = self.name
            out.extend(new)
            if len(offers) < max(5, self.page_size // 2) or not new:
                break
        return out

    def search_page(self, query: str, page: int) -> list[Offer]:  # pragma: no cover - abstract
        raise NotImplementedError

    def product(self, url: str) -> Optional[Offer]:
        """Price of a single product page (URL watches). Default: JSON-LD / microdata / meta."""
        resp = self.http.get(url)
        self.last_raw = resp.text
        o = extract_product_page(resp.text, resp.url, self.key)
        if o:
            o.url = url
            o.store_name = self.name
        return o

    # ------------------------------------------------------------------ helpers
    def get_html(self, url: str, **kw) -> tuple[str, BeautifulSoup, str]:
        resp = self.http.get(url, **kw)
        self.last_url = resp.url
        self.last_raw = resp.text
        return resp.text, make_soup(resp.text), resp.url

    def get_json(self, url: str, **kw):
        self.last_url = url
        data = self.http.get_json(url, **kw)
        try:
            import json as _json

            self.last_raw = _json.dumps(data, ensure_ascii=False)[:3_000_000]
        except (TypeError, ValueError):
            self.last_raw = None
        return data

    def abs(self, href: Optional[str]) -> str:
        return urljoin(self.base_url, href or "")

    def fallback(self, html: str, url: str, soup: Optional[BeautifulSoup] = None) -> list[Offer]:
        offers = extract_offers(html, url, self.key, soup=soup)
        if offers:
            self.last_strategy = "generic:" + offers[0].source
        return offers

    @staticmethod
    def q_path(query: str) -> str:
        """Query for use inside a URL path: spaces -> %20."""
        return quote(query.strip(), safe="")

    @staticmethod
    def q_plus(query: str) -> str:
        """Query for use in a query-string: spaces -> +."""
        return quote_plus(query.strip())

    def __repr__(self) -> str:
        return f"<Store {self.key}>"
