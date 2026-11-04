"""ITGalaxy (itgalaxy.ro) - custom shop, server-rendered GET search form (no JS needed).

Search: GET https://www.itgalaxy.ro/cauta/?search={quote_plus(q)}   e.g. /cauta/?search=ssd+1tb
        (bf-price-monitor live 2026-10-28; libergent 2026-09-28 direct fetch returned matching offers).
        Page N > 1: only the pager's next link (search pagination undocumented).
Cards (REAL markup captured 2026-10-28, 36 per page):  #products-list article.product-box
    form#add_main_product_<id>[action=".../adaugaincos-<id>?is_ajax=1"]
    h2.name a[href][data-ecpid]     text = full name ("SSD WD SN7100 1TB PCIe M.2"), a[title] is SHORTER
    span[id="price-<id>"]           "1.115,99 lei" (aria-label "Pret 1.115,99 lei")
    button.addtocart-button[data-pprice="1115.99"][data-prod][data-pname]   price incl. VAT
        (the onclick ecAddToCart(847426, 922.3058, 1) value is WITHOUT VAT - never used)
    div.availability.instock ("in stoc suficient" / "in stoc limitat") | .insupplierstock ("in stoc
        depozit/furnizor") | out of stock never observed (guess: .outofstock / "epuizat"/"indisponibil")
  No schema.org JSON-LD on listings. URL https://www.itgalaxy.ro/<cat>/<brand>/<slug>-<id>/.
Product page: span#price-<id> / button[data-prod=<id>][data-pprice] of THIS product first (related products sit on
  the same page), generic JSON-LD/microdata of the same response only when those are absent.
Anti-bot: nothing documented (bf-price-monitor ran from a residential IP); Cloudflare markers are checked.
"""
from __future__ import annotations

import re
from typing import Optional

from bs4 import Tag

from ..extract import extract_product_page, make_soup
from ..models import Offer
from ..prices import parse_price
from .base import Store
from ._itshops import (
    ShopMixin, clean_url, current_price, dedupe, enrich_pdp, image_of, landed_on_home, longest, next_page_url,
    num_attr, old_price_in, outermost, pdp_offer, pdp_title, pick_price, raise_if_challenge, stock_of_el,
)

_URL_ID_RE = re.compile(r"-(\d{3,})/?$")
_FORM_ID_RE = re.compile(r"(?:add_main_product_|adaugaincos-)(\d+)")


def _url_id(url: str) -> Optional[str]:
    m = _URL_ID_RE.search(url.split("?", 1)[0])
    return m.group(1) if m else None


class ItGalaxyStore(ShopMixin, Store):
    key = "itgalaxy"
    name = "ITGalaxy"
    base_url = "https://www.itgalaxy.ro"
    domains = ("itgalaxy.ro",)
    categories = ("pc", "electronics")
    page_size = 36
    min_delay = 5.0          # bf-price-monitor keeps ~5 s between requests to this domain
    extra_headers = {"Referer": "https://www.itgalaxy.ro/", "Upgrade-Insecure-Requests": "1"}

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/cauta/?search={self.q_plus(query)}"

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
            self.last_strategy = "redirect:home"     # home-page promotions are not search results
            return []
        root = soup.select_one("#products-list") or soup
        cards = outermost(root.select("article.product-box"))
        out = dedupe(self._card(c, final) for c in cards)
        if out:
            self.last_strategy = "cards"
            self._pager_set(page + 1, next_page_url(soup, final, page))
            return out
        raise_if_challenge(html, soup, final)
        if page > 1:
            return []
        return self.fallback(html, final, soup)

    def _card(self, c: Tag, base: str) -> Optional[Offer]:
        a = c.select_one("h2.name a[href]") or c.select_one(".name a[href]") or c.select_one("a[data-ecpid][href]")
        if a is None:
            return None
        url = clean_url(a.get("href"), base)
        btn = c.select_one("button[data-pprice]") or c.select_one(".addtocart-button")
        title = longest(a.get_text(" ", strip=True), btn.get("data-pname") if btn is not None else "", a.get("title"))
        if not url or not title:
            return None
        price_el, price = pick_price(c, 'span[id^="price-"]', ".info.price span", ".price")
        if not price:
            labelled = c.select_one('span[id^="price-"][aria-label]')
            if labelled is not None:
                price_el, price = labelled, current_price_label(labelled.get("aria-label"))
        if not price and btn is not None:
            price = num_attr(btn.get("data-pprice"))
        if not price:
            return None
        old = old_price_in(c.select_one(".info.price") or c, price, exclude=[price_el])
        pid = None
        link_id = c.select_one("[data-ecpid]")
        if link_id is not None and str(link_id.get("data-ecpid") or "").isdigit():
            pid = link_id["data-ecpid"]
        elif btn is not None and str(btn.get("data-prod") or "").isdigit():
            pid = btn["data-prod"]
        else:
            form = c.select_one("form[id^='add_main_product_'], form[action*='adaugaincos-']")
            ref = f"{form.get('id') or ''} {form.get('action') or ''}" if form is not None else ""
            m = _FORM_ID_RE.search(ref)
            pid = m.group(1) if m else _url_id(url)
        return Offer(
            store=self.key, title=title, url=url, price=price, old_price=old if old and old > price else None,
            in_stock=stock_of_el(c.select_one(".availability")), offer_id=str(pid) if pid else None,
            image=image_of(c, base, ".image img, img"), source="itgalaxy",
        )

    def product(self, url: str) -> Optional[Offer]:
        """Product page. The page also lists related products, so the markup tagged with THIS product's id
        (span#price-<id> / button[data-prod=<id>][data-pprice], verified on listings 2026-10-28) is read first;
        generic JSON-LD/microdata (no JSON-LD on listings; product pages unverified) only when that is absent -
        otherwise a related product's microdata could become the watched product's price."""
        resp = self.http.get(url)
        raw = resp.text or ""
        self.last_raw = raw
        soup = make_soup(raw)
        pid = _url_id(url)
        o: Optional[Offer] = None
        if pid:
            o = pdp_offer(self, soup, url, price_sels=(f'span[id="price-{pid}"]',), stock_sels=(".availability",),
                          offer_id=pid, source="itgalaxy-pdp")
            btn = soup.select_one(f'button[data-prod="{pid}"][data-pprice]')
            if o is None and btn is not None:
                price = num_attr(btn.get("data-pprice"))
                title = pdp_title(soup) or str(btn.get("data-pname") or "")
                if price and title:
                    o = Offer(store=self.key, title=title, url=url, price=price, offer_id=pid,
                              in_stock=stock_of_el(soup.select_one(".availability")), store_name=self.name,
                              source="itgalaxy-pdp")
            if o is not None:
                if btn is not None and btn.get("data-pname") and len(str(btn["data-pname"])) > len(o.title):
                    o.title = " ".join(str(btn["data-pname"]).split())
                return o
        o = extract_product_page(raw, resp.url, self.key)     # same page, no second request
        if o is not None:
            o.url = url
            o.store_name = self.name
            o.offer_id = pid or o.offer_id
            return enrich_pdp(o, soup, stock_sels=(".availability",))
        raise_if_challenge(raw, soup, url)
        return None


def current_price_label(label: Optional[str]) -> Optional[float]:
    """aria-label "Pret 1.115,99 lei" -> 1115.99"""
    return parse_price(label) if label else None
