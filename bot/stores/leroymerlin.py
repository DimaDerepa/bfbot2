"""Leroy Merlin Romania (leroymerlin.ro) - DIY / home, own catalog + Mirakl marketplace offers.

Search: GET https://www.leroymerlin.ro/produse/search/{quote(q, safe='')}   (query = PATH segment, %20)
        page N: ?page=N.  Title "Cauti {q}? Avem {N} de produse"; empty: "0 rezultate pentru: {q}".
No verified selectors, so parsing is layered:
  1. schema.org JSON-LD ItemList / Product (if the page has it)
  2. product anchors: own catalog /produse/{cat-slug}/{catId}/{slug}/{productId}
     or marketplace /produse/{slug}-{id}.html (older links use /products/), grouped into their card;
     price = first "829,00 lei" in the card that is not struck/old and not a per-l/kg/mp unit price
     ("829,00 lei / unitate TVA inclus" is the main price); old price from <del>/<s>/class*=old.
     "Indisponibil" = out of stock; marketplace "Vandut de X" -> seller.
  3. generic extraction.
Anti-bot: sister sites (FR/IT/PT) use DataDome -> captcha-delivery.com page / 403 -> BlockedError.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlsplit

from bs4 import Tag

from ..extract import offers_from_jsonld
from ..models import Offer
from ..prices import PRICE_WITH_CURRENCY_RE, element_price_text, parse_price
from ._ro_retail import (clean_url, glue_separators, image_of, norm_text, price_text, raise_if_challenge,
                         seller_of)
from .base import Store

_OWN_RE = re.compile(r"^/(?:produse|products)/[^/]+/\d+/[^/]+/(\d+)/?$")
_MKP_RE = re.compile(r"^/(?:produse|products)/[^/]+-(\d{5,})\.html$")
_UNIT_AFTER_RE = re.compile(r"^\s*/\s*(?:l|kg|g|mp|m2|m²|ml|m|mc|m3|litru|metru|kilogram)\b", re.I)
_NOT_PRICE_RE = re.compile(r"(rate|/\s*lun|lunar|economis|cashback|voucher|transport|livrare|reducere de)", re.I)
_OLD_WORDS = {"old", "strike", "strikethrough", "through", "crossed", "barred", "before", "initial", "was", "prp",
              "previous", "vechi"}
_DISCOUNT_RE = re.compile(r"-\s?\d{1,2}\s?%")
_OOS = ("indisponibil", "stoc epuizat", "nu este disponibil", "nu mai este disponibil", "fara stoc", "fără stoc")
_INSTOCK = ("în stoc", "in stoc")
_OUTAGE = ("probleme tehnice",)


def product_id(url: str) -> Optional[str]:
    p = urlsplit(url)
    if p.netloc and not p.netloc.lower().endswith("leroymerlin.ro"):
        return None
    m = _OWN_RE.match(p.path) or _MKP_RE.match(p.path)
    return m.group(1) if m else None


class LeroyMerlinStore(Store):
    key = "leroymerlin"
    name = "Leroy Merlin"
    base_url = "https://www.leroymerlin.ro"
    domains = ("leroymerlin.ro",)
    categories = ("diy", "home", "appliances")
    page_size = 24
    min_delay = 3.0

    def search_page(self, query: str, page: int) -> list[Offer]:
        url = f"{self.base_url}/produse/search/{self.q_path(query)}"
        html, soup, final = self.get_html(url, params={"page": str(page)} if page > 1 else None)

        lds = [o for o in offers_from_jsonld(soup, final, self.key) if product_id(clean_url(o.url, final))]
        if len(lds) >= 2:
            for o in lds:
                o.url = clean_url(o.url, final)
                o.offer_id = product_id(o.url) or o.offer_id
            self.last_strategy = "jsonld"
            return _dedupe(lds)

        out = self.parse_cards(soup, final)
        if out:
            self.last_strategy = "anchors"
            return out
        raise_if_challenge(soup, html, final)
        txt = norm_text(soup.body or soup).lower()
        if re.search(r"(?<!\d)0 rezultate", txt):
            self.last_strategy = "no-results"
            return []
        out = self.fallback(html, final, soup)
        for o in out:
            o.url = clean_url(o.url, final)
            o.offer_id = product_id(o.url) or o.offer_id
        if not out and any(m in txt for m in _OUTAGE):
            self.last_strategy = "outage-banner"
        return _dedupe(out)

    # ------------------------------------------------------------------ anchors -> cards
    def parse_cards(self, soup, page_url: str) -> list[Offer]:
        groups: dict[str, list[Tag]] = {}
        for a in soup.find_all("a", href=True):
            u = clean_url(a["href"], page_url)
            if product_id(u):
                groups.setdefault(u, []).append(a)
        out: list[Offer] = []
        seen: set[str] = set()
        for u, anchors in groups.items():
            card = _card(anchors[0], u, page_url)
            if card is None:
                continue
            o = self._card_offer(card, u, anchors, page_url)
            if o is not None and o.key not in seen:
                seen.add(o.key)
                out.append(o)
        return out

    def _card_offer(self, card: Tag, url: str, anchors: list[Tag], page_url: str) -> Optional[Offer]:
        title = _title(card, anchors)
        if not title:
            return None
        olds: list[float] = []
        for el in card.find_all(True):
            if el.name in ("del", "s", "strike") or _is_old_class(el.get("class") or []):
                t = norm_text(el)
                if t and len(t) < 40:
                    v = price_text(element_price_text(el))
                    if v:
                        olds.append(v)
        text = glue_separators(element_price_text(card))
        mains: list[float] = []
        for m in PRICE_WITH_CURRENCY_RE.finditer(text):
            v = parse_price(m.group(0))
            if not v:
                continue
            before, after = text[max(0, m.start() - 14):m.start()], text[m.end():m.end() + 25]
            if _UNIT_AFTER_RE.match(after) or _NOT_PRICE_RE.search(before + " " + after[:12]):
                continue
            if any(abs(v - o) < 0.005 for o in olds):
                continue
            mains.append(v)
        if not mains:
            return None
        price = mains[0]
        old = max(olds) if olds else None
        if old is None and len(set(mains)) >= 2 and _DISCOUNT_RE.search(text):
            price, old = min(mains), max(mains)
        low = norm_text(card).lower()
        stock: Optional[bool] = None
        if any(x in low for x in _OOS):
            stock = False
        elif any(x in low for x in _INSTOCK):
            stock = True
        return Offer(
            store=self.key, title=title, url=url, price=price,
            old_price=old if old and old > price else None, in_stock=stock,
            offer_id=product_id(url), image=image_of(card, page_url), seller=seller_of(card, ("leroy merlin", "leroymerlin")),
            source="leroymerlin-cards",
        )


def _card(a: Tag, url: str, page_url: str) -> Optional[Tag]:
    """Largest ancestor of `a` that holds exactly one product (and a price)."""
    node: Tag = a
    best: Optional[Tag] = None
    for _ in range(9):
        parent = node.parent
        if not isinstance(parent, Tag) or parent.name in ("body", "html", "main"):
            break
        others = {clean_url(x["href"], page_url) for x in parent.find_all("a", href=True)}
        if len({u for u in others if product_id(u)} - {url}) > 0:
            break
        if len(parent.get_text(" ", strip=True)) > 2500:
            break
        node = parent
        if re.search(r"\d\s*(?:lei|ron)\b", node.get_text(" ", strip=True), re.I):
            best = node
    return best


def _is_old_class(classes: list[str]) -> bool:
    for tok in classes:
        parts = re.split(r"[-_]+|(?<=[a-z])(?=[A-Z])", tok)
        if any(p.lower() in _OLD_WORDS for p in parts):
            return True
    return False


def _no_price(t: str) -> bool:
    return len(t) >= 5 and not re.search(r"\d\s*(?:lei|ron)\b", t, re.I)


def _title(card: Tag, anchors: list[Tag]) -> str:
    for el in card.select("h2, h3, h4"):
        t = norm_text(el)
        if _no_price(t):
            return t
    for a in anchors:
        t = " ".join((a.get("title") or "").split())
        if len(t) >= 5:
            return t
    texts = [t for t in (norm_text(a) for a in anchors) if _no_price(t)]
    if texts:
        return max(texts, key=len)
    for el in card.select("[class*='title'], [class*='name']"):
        t = norm_text(el)
        if _no_price(t):
            return t
    img = card.find("img", alt=True)
    return " ".join((img.get("alt") or "").split()) if img else ""


def _dedupe(offers: list[Offer]) -> list[Offer]:
    seen: set[str] = set()
    out: list[Offer] = []
    for o in offers:
        if o.key not in seen:
            seen.add(o.key)
            out.append(o)
    return out
