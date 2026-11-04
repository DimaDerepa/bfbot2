"""Vexio (vexio.ro) - While1 platform shop (Bootstrap 3 markup), Cloudflare in front.

Search (real captures from GitHub Actions, 2026-11-04): the header form is
  <form action="/search/" id="search_form" method="get"><input type="text" name="q">
so https://www.vexio.ro/search/?q={quote_plus(q)} is the route (/cauta/?q=X only redirects to /search?q=&q=X).
That page is a SHELL: title "X. Rezultate cautare X", <h1>Cautare - X</h1>, a help alert, the category mega-menu
and footer - not one product card, no JSON-LD/embedded product data. The inline config says
  let search_frontend_provider = 'luigisbox';      + <script src="https://scripts.luigisbox.tech/LBX-784749.js">
i.e. results are rendered client-side by Luigi's Box. The adapter therefore queries the shop's Luigi's Box index:
  GET https://live.luigisbox.com/search?tracker_id={TRACKER_ID}&q={q}&f[]=type:{item|product}&size=24&page={N}
  TRACKER_ID ("111111-222222" shape) is NOT in the page: it is read from the LBX-<n>.js loader named by the page
  (once per process; retried after an hour if it failed) or taken from env VEXIO_LUIGISBOX_TRACKER_ID.
  The index type is taken from the loader when it names one, else "item" then "product" are tried.
  Hit fields vary per index, several names are tried: url | attributes.web_url, attributes.title, price_amount |
  price ("1.299,99 lei"), price_old_amount | price_old (old price only when > price), availability 0/1 |
  availability text, image_link, id.  Hits of another type (category/brand/article) or without price are skipped.
  No tracker id -> [] with last_strategy "luigisbox:no-tracker-id" (the shell's menu links are never offers).
Route discovery (kept for robustness): the remembered route, /search/?q=, the home page's own GET search <form>,
  /cauta/?q=. A 403/429/challenge stops immediately (BlockedError).
Server-rendered cards, if a page ever has them (Pretzi 2026-03 markup): article.product-box
    heading link / a.text-center[href]; name from text, a[title], img alt
    .price.clearfix .pull-left.discounted   or   .price.clearfix .pull-left > strong      current price
    old price: .price.clearfix del / .old_price;  stock: .instock / .availability text
  URL https://www.vexio.ro/<cat>/<brand>/<id>-<slug>/ -> id.
Product page: #price-value / .price-box .h3.price-value span (never .pvalue), old .price-box .old_price del,
  JSON "price": <plain decimal> in scripts.
"""
from __future__ import annotations

import html as _html
import os
import re
import time
from typing import Any, ClassVar, Optional
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..extract import make_soup
from ..models import Offer
from ..net import BlockedError, FetchError
from .base import Store
from ._itshops import (
    ShopMixin, clean_url, current_price, dedupe, enrich_pdp, image_of, landed_on_home, longest, next_page_url,
    norm, num_attr, old_price_in, outermost, pdp_offer, pdp_title, pick_price, raise_if_challenge, stock_from,
    stock_of_el, text_of,
)

_ID_RE = re.compile(r"^/[^/]+/[^/]+/(\d+)-[^/]*/?$")
_QUERY_INPUT = re.compile(r"^(?:q|search|keyword|keywords|term|s|query|cauta|text|searchword)$", re.I)
_NO_RESULTS_RE = re.compile(
    r"nu\s+(?:am|au)\s+(?:fost\s+)?g[aă]sit|niciun\s+(?:produs|rezultat)|nu\s+exist[aă]\s+(?:produse|rezultate)|"
    r"0\s+(?:produse|rezultate)\s+(?:g[aă]sit|pentru)",
    re.I,
)
_JSON_PRICE_RE = re.compile(r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?')

LUIGIS_SEARCH = "https://live.luigisbox.com/search"
TRACKER_ENV = "VEXIO_LUIGISBOX_TRACKER_ID"
_PROVIDER_RE = re.compile(r"""search_frontend_provider\s*=\s*['"]luigisbox['"]""", re.I)
_LBX_RE = re.compile(r"""(?:https?:)?//scripts\.luigisbox\.(com|tech)/LBX-(\d+)\.js""", re.I)
_TRACKER_CTX_RE = re.compile(r"""tracker[_-]?id["']?\s*[:=]\s*["'](\d{1,9}-\d{1,9})["']""", re.I)
_TRACKER_ANY_RE = re.compile(r"(?<![\w.-])(\d{3,9}-\d{3,9})(?![\w.-])")
_LB_TYPE_RE = re.compile(r"""\btype["']?\s*:\s*["'](item|product)["']""")
_LB_TYPES = ("item", "product")
_TRACKER_RETRY = 3600.0
_TAG_RE = re.compile(r"<[^>]+>")
_PRICE_KEYS = ("price_amount", "price_sale_amount", "sale_price_amount", "price", "sale_price", "final_price")
_OLD_KEYS = ("price_old_amount", "price_old", "old_price_amount", "old_price", "price_full_amount", "price_full",
             "original_price", "regular_price", "price_before_discount")
_STOCK_TEXT_KEYS = ("availability_text", "availability_rank_text", "stock_text", "stock_status", "stoc")
_ID_KEYS = ("id", "product_id", "item_id", "pid")
_IMG_KEYS = ("image_link", "image_link_m", "image_link_l", "image_link_s", "image", "image_url")


def _url_id(url: str) -> Optional[str]:
    m = _ID_RE.search(urlsplit(url).path)
    return m.group(1) if m else None


def find_tracker_id(text: str, lbx: Optional[str] = None) -> Optional[str]:
    """Luigi's Box tracker id ("12345-67890") from page HTML / the LBX-<n>.js loader."""
    text = text or ""
    m = _TRACKER_CTX_RE.search(text)
    if m:
        return m.group(1)
    if lbx:
        m = re.search(r"(?<![\w.-])(" + re.escape(lbx) + r"-\d{2,9})(?![\w.-])", text)
        if m:
            return m.group(1)
    low = text.lower()
    for m in _TRACKER_ANY_RE.finditer(text):
        ctx = low[max(0, m.start() - 160):m.end() + 40]
        if "luigi" in ctx or "tracker" in ctx:
            return m.group(1)
    return None


def _first(v: Any) -> Any:
    """Luigi's Box attribute values are often one-element lists."""
    if isinstance(v, (list, tuple)):
        return next((x for x in v if x not in (None, "", [])), None)
    return v


def _plain(v: Any) -> str:
    v = _first(v)
    return norm(_html.unescape(_TAG_RE.sub(" ", v))) if isinstance(v, str) else ""


def _amount(attrs: dict, keys) -> Optional[float]:
    for k in keys:
        v = _first(attrs.get(k))
        if isinstance(v, dict):
            v = v.get("amount") or v.get("value")
        p = num_attr(v)
        if p:
            return p
    return None


class VexioStore(ShopMixin, Store):
    key = "vexio"
    name = "Vexio"
    base_url = "https://www.vexio.ro"
    domains = ("vexio.ro",)
    categories = ("electronics", "pc", "appliances", "home", "diy", "general")
    page_size = 24
    min_delay = 4.0          # 403/429/challenge observed on direct requests
    extra_headers = {"Referer": "https://www.vexio.ro/", "Upgrade-Insecure-Requests": "1"}

    primary_route = "/search/?q={q}"              # header <form id="search_form" action="/search/"> name="q"
    other_routes = ("/cauta/?q={q}",)
    _route: ClassVar[Optional[str]] = None        # template that worked in this process
    _probed: ClassVar[bool] = False               # discovery + alternative routes already tried once
    _tracker: ClassVar[Optional[str]] = None      # Luigi's Box tracker id found in this process
    _tracker_at: ClassVar[float] = 0.0            # when the tracker lookup last ran (0 = never)
    _lb_type: ClassVar[Optional[str]] = None      # Luigi's Box index type that returned hits

    def search_url(self, query: str) -> str:
        return self._fill(self.base_url + self.primary_route, query)

    def _fill(self, template: str, query: str) -> str:
        return template.replace("{q}", self.q_plus(query))

    # ------------------------------------------------------------------ search
    def search_page(self, query: str, page: int) -> list[Offer]:
        tracker = self._known_tracker()
        if tracker:
            return self._luigis(query, page, tracker)
        if page > 1:
            url = self._pager_get(page)
            if not url:
                return []
            html, soup, final = self.get_html(url)
            if landed_on_home(url, final):
                return []
            out = self._parse(soup, final)
            if out:
                self._pager_set(page + 1, next_page_url(soup, final, page))
            return out
        self._pager_reset()
        tried: list[str] = []
        first: Optional[tuple[str, BeautifulSoup, str]] = None
        missing: Optional[FetchError] = None
        went_home = False
        templates: list[Optional[str]] = []
        if type(self)._route:
            templates.append(type(self)._route)
        templates.append(self.base_url + self.primary_route)
        if not type(self)._probed:   # once per process: home-page form (None) + the other guesses
            templates += [None] + [self.base_url + r for r in self.other_routes]
        for tmpl in templates:
            if tmpl is None:
                type(self)._probed = True
                tmpl = self._discover_route()
                if not tmpl:
                    continue
            if tmpl in tried:
                continue
            tried.append(tmpl)
            req_url = self._fill(tmpl, query)
            try:
                html, soup, final = self.get_html(req_url)
            except BlockedError:
                raise
            except FetchError as e:
                if e.status in (400, 404, 405, 410):
                    missing = e
                    continue
                raise
            if landed_on_home(req_url, final):
                # unknown route redirected to the home page: its promotion cards are not results, and the
                # route must neither be remembered nor feed the generic fallback
                raise_if_challenge(html, soup, final)
                went_home = True
                continue
            out = self._parse(soup, final)
            if out:
                type(self)._route = tmpl
                self.last_strategy = "cards"
                self._pager_set(2, next_page_url(soup, final, 1))
                return out
            raise_if_challenge(html, soup, final)
            if _PROVIDER_RE.search(html) or _LBX_RE.search(html):
                # the real search page: an empty shell filled client-side by Luigi's Box
                if self._is_search_page(soup, query):
                    type(self)._route = tmpl
                tracker = self._discover_tracker(html)
                if tracker:
                    return self._luigis(query, 1, tracker)
                self.last_strategy = "luigisbox:no-tracker-id"
                return []
            if self._is_search_page(soup, query):
                type(self)._route = tmpl
                if _NO_RESULTS_RE.search(text_of(soup.body or soup)):
                    self.last_strategy = "no-results"
                    return []
                return self.fallback(html, final, soup)
            if first is None:
                first = (html, soup, final)
        if first is None:
            if went_home:
                self.last_strategy = "redirect:home"
                return []
            raise missing or FetchError("no search route answered", None, self.search_url(query))
        html, soup, final = first
        return self.fallback(html, final, soup)

    # ------------------------------------------------------------------ Luigi's Box
    def _known_tracker(self) -> Optional[str]:
        return norm(os.environ.get(TRACKER_ENV)) or type(self)._tracker

    def _discover_tracker(self, html: str) -> Optional[str]:
        cls = type(self)
        if cls._tracker or (cls._tracker_at and time.time() - cls._tracker_at < _TRACKER_RETRY):
            return cls._tracker
        cls._tracker_at = time.time()
        tid = find_tracker_id(html)
        m = _LBX_RE.search(html)
        if not tid and m:
            dom, num = m.group(1).lower(), m.group(2)
            for d in (dom, "com" if dom == "tech" else "tech"):
                try:
                    js = self.http.get(f"https://scripts.luigisbox.{d}/LBX-{num}.js",
                                       headers={"Accept": "*/*", "Referer": self.base_url + "/"}).text or ""
                except FetchError:      # also BlockedError: the CDN refusing is not the shop blocking us
                    continue
                tid = find_tracker_id(js, num)
                t = _LB_TYPE_RE.search(js)
                if t and not cls._lb_type:
                    cls._lb_type = t.group(1)
                break
        cls._tracker = tid
        return tid

    def lb_url(self, query: str, tracker: str, typ: str, page: int = 1) -> str:
        return LUIGIS_SEARCH + "?" + urlencode([
            ("tracker_id", tracker), ("q", query.strip()), ("f[]", f"type:{typ}"),
            ("size", str(self.page_size)), ("page", str(page)),
        ])

    def _luigis(self, query: str, page: int, tracker: str) -> list[Offer]:
        if page == 1:
            self._lb_done = False
        elif getattr(self, "_lb_done", False):
            return []
        cls = type(self)
        types = [cls._lb_type] if cls._lb_type else list(_LB_TYPES)
        hits: list = []
        total = 0
        for typ in types:
            data = self.get_json(self.lb_url(query, tracker, typ, page),
                                 headers={"Accept": "application/json", "Origin": self.base_url,
                                          "Referer": self.base_url + "/"})
            results = data.get("results") if isinstance(data, dict) else None
            results = results if isinstance(results, dict) else {}
            hits = [h for h in results.get("hits") or [] if isinstance(h, dict)]
            try:
                total = int(results.get("total_hits") or 0)
            except (TypeError, ValueError):
                total = 0
            if hits or total:
                cls._lb_type = typ
                break
        self.last_strategy = "luigisbox"
        out = dedupe(self._hit(h) for h in hits)
        self._lb_done = not hits or bool(total and page * self.page_size >= total)
        return out

    def _hit(self, h: dict) -> Optional[Offer]:
        typ = norm(h.get("type")).lower()
        if typ and typ not in _LB_TYPES:
            return None                         # category / brand / article / query suggestion
        a = h.get("attributes") if isinstance(h.get("attributes"), dict) else {}
        href = next((v for v in (_first(h.get("url")), _first(a.get("web_url")), _first(a.get("url")))
                     if isinstance(v, str) and v.strip()), "")
        url = clean_url(href, self.base_url)
        if not url or urlsplit(url).netloc.removeprefix("www.") != "vexio.ro":
            return None
        title = _plain(a.get("title")) or _plain(a.get("name"))
        price = _amount(a, _PRICE_KEYS)
        if not title or not price:
            return None
        old = _amount(a, _OLD_KEYS)
        stock: Optional[bool] = None
        av = _first(a.get("availability"))
        if isinstance(av, bool):
            stock = av
        elif isinstance(av, (int, float)) or (isinstance(av, str) and av.strip().isdigit()):
            stock = int(av) > 0
        if stock is None:
            stock = stock_from((), " ".join(_plain(a.get(k)) for k in _STOCK_TEXT_KEYS) + " " + _plain(av))
        pid = next((str(_first(a.get(k))) for k in _ID_KEYS if _first(a.get(k)) not in (None, "")), None)
        img = next((v for v in (_first(a.get(k)) for k in _IMG_KEYS) if isinstance(v, str) and v.strip()), None)
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=_url_id(url) or pid, image=urljoin(self.base_url, img) if img else None,
            source="luigisbox",
        )

    def _discover_route(self) -> Optional[str]:
        """Build a search URL template from the home page's GET search form."""
        try:
            _html, soup, final = self.get_html(self.base_url + "/")
        except BlockedError:
            raise
        except FetchError:
            return None
        for form in soup.find_all("form"):
            if (form.get("method") or "get").strip().lower() != "get":
                continue
            inp = form.select_one("input[type='search'][name]")
            if inp is None:
                inp = next((i for i in form.find_all("input", attrs={"name": True})
                            if _QUERY_INPUT.match(i["name"])
                            and (i.get("type") or "text").lower() in ("text", "search")), None)
            if inp is None:
                continue
            action = urljoin(final, form.get("action") or "/")
            if urlsplit(action).netloc.lower().removeprefix("www.") != "vexio.ro":
                continue
            hidden = [(h["name"], h.get("value") or "") for h in form.select("input[type='hidden'][name]")]
            extra = urlencode(hidden)
            sep = "&" if "?" in action else "?"
            return f"{action}{sep}{extra + '&' if extra else ''}{inp['name']}={{q}}"
        return None

    @staticmethod
    def _is_search_page(soup: BeautifulSoup, query: str) -> bool:
        q = norm(query).lower()
        if not q:
            return False
        for inp in soup.find_all("input", attrs={"name": _QUERY_INPUT}):
            if norm(inp.get("value")).lower() == q:
                return True
        heads = [text_of(soup.title)] + [text_of(h) for h in soup.find_all(["h1", "h2"], limit=3)]
        return any(q in h.lower() and re.search(r"cauta|c[aă]utare|rezultat|search", h, re.I) for h in heads)

    # ------------------------------------------------------------------ parsing
    def _parse(self, soup: BeautifulSoup, base: str) -> list[Offer]:
        cards = outermost(soup.select("article.product-box"))
        return dedupe(self._card(c, base) for c in cards)

    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        head = c.select_one("h2 a[href], h3 a[href], h4 a[href], .name a[href], .title a[href], .product-title a[href]")
        anchor = c.select_one("a.text-center[href]")
        link = head or anchor
        if link is None:
            link = next((a for a in c.select("a[href]") if _url_id(clean_url(a.get("href"), base))), None)
        if link is None:
            return None
        url = clean_url(link.get("href"), base)
        img = c.select_one("img[alt]")
        title = longest(text_of(head), head.get("title") if head is not None else "",
                        anchor.get("title") if anchor is not None else "", text_of(anchor))
        if not title and img is not None:
            title = norm(img.get("alt"))
        if not url or not title:
            return None
        box = c.select_one(".price.clearfix") or c.select_one(".price") or c
        price_el, price = pick_price(box, ".pull-left.discounted", ".pull-left > strong", ".discounted", "strong")
        if not price and box is not c:
            price_el, price = box, current_price(box)
        if not price:
            return None
        old = old_price_in(c, price, exclude=[price_el])
        stock = stock_of_el(c.select_one(".availability, .instock, .outofstock, [class*='stoc']"))
        if stock is None:
            stock = stock_from((), text_of(c))
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock, offer_id=_url_id(url), image=image_of(c, base), source="vexio",
        )

    # ------------------------------------------------------------------ product page
    def product(self, url: str) -> Optional[Offer]:
        o = super().product(url)
        raw = self.last_raw or ""
        soup = make_soup(raw) if raw else None
        old_sels = (".price-box .old_price del", ".price-box del")
        if o is not None:
            o.offer_id = _url_id(url) or o.offer_id
            return enrich_pdp(o, soup, stock_sels=(".availability", ".instock", ".outofstock"), old_sels=old_sels)
        if soup is None:
            return None
        raise_if_challenge(raw, soup, url)
        for el in soup.select(".pvalue"):
            el.extract()  # a different value, not the selling price
        o = pdp_offer(self, soup, url, price_sels=("#price-value", ".price-box .h3.price-value span",
                                                   ".price-box .price-value"),
                      old_sels=old_sels, stock_sels=(".availability", ".instock", ".outofstock"),
                      offer_id=_url_id(url), source="vexio-pdp")
        if o is None:
            m = _JSON_PRICE_RE.search(raw)
            price = num_attr(m.group(1)) if m else None
            title = pdp_title(soup)
            if price and title:
                o = Offer(store=self.key, title=title, url=url, price=price, offer_id=_url_id(url),
                          store_name=self.name, source="vexio-pdp-json")
        return o
