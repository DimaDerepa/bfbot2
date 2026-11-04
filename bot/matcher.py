"""Decide whether an offer satisfies a watch's criteria."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Offer, Watch
from .text import has_term, norm, query_terms

# Accessory / spare-part titles are skipped by default unless the word is part of the query.
# Words in ACCESSORY_PREFIX only count when they are among the first 3 words of the title
# ("Husa pentru iPhone 16", "Cablu USB-C ..."), so "Aspirator ... cu suport" is not affected.
ACCESSORY_PREFIX = [
    "husa", "huse", "folie", "folii", "carcasa", "bumper", "protectie", "sticla", "cablu", "incarcator",
    "adaptor", "suport", "skin", "stylus", "curea", "geanta", "rucsac", "case", "cover", "charger",
    "display", "ecran", "touchscreen", "baterie", "acumulator", "tastatura", "piese", "inlocuire",
    "garantie", "asigurare", "abonament", "set folii", "kit",
]
ACCESSORY_ANYWHERE = [
    "pentru iphone", "pentru samsung", "pentru apple", "pentru galaxy", "pentru xiaomi",
    "compatibil cu iphone", "compatibila cu iphone", "compatibil iphone", "screen protector",
    "tempered glass", "sticla securizata", "garantie extinsa", "extra garantie",
]
ACCESSORY_WORDS = ACCESSORY_PREFIX + ACCESSORY_ANYWHERE


@dataclass
class MatchResult:
    title_ok: bool
    ok: bool
    reasons: list[str] = field(default_factory=list)
    discount: Optional[float] = None


def title_matches(watch: Watch, title: str) -> tuple[bool, str]:
    t = norm(title)
    if watch.must_include:
        for term in watch.must_include:
            if not has_term(t, term):
                return False, f"нет «{term}»"
    elif watch.strict and watch.query:
        for term in query_terms(watch.query):
            if not has_term(t, term):
                return False, f"нет «{term}»"
    if watch.any_of and not any(has_term(t, term) for term in watch.any_of):
        return False, "нет ни одного из any_of"
    for term in watch.exclude:
        if has_term(t, term):
            return False, f"исключено «{term}»"
    if watch.exclude_accessories:
        q = norm(" ".join([watch.query or "", *watch.must_include, *watch.any_of]))
        head = " ".join(t.replace("-", " ").split()[:3])
        for term in ACCESSORY_PREFIX:
            if has_term(head, term) and not has_term(q, term):
                return False, f"аксессуар «{term}»"
        for term in ACCESSORY_ANYWHERE:
            if has_term(t, term) and not has_term(q, term):
                return False, f"аксессуар «{term}»"
    return True, ""


def evaluate(watch: Watch, offer: Offer, reference_price: Optional[float] = None,
             check_title: bool = True) -> MatchResult:
    """`reference_price`: the highest price we observed for this offer recently (for fake-discount-proof %).
    `check_title=False` for product-URL watches: the user picked the exact product, only price rules apply."""
    if check_title:
        ok_title, why = title_matches(watch, offer.title)
        if not ok_title:
            return MatchResult(False, False, [why])
    reasons: list[str] = []
    store_disc = offer.discount_pct
    hist_disc = None
    if reference_price and reference_price > offer.price:
        hist_disc = round((reference_price - offer.price) / reference_price * 100, 1)
    discount = max([d for d in (store_disc, hist_disc) if d is not None], default=None)
    if watch.max_price is not None and offer.price > watch.max_price:
        reasons.append("дороже максимума")
    if watch.min_price is not None and offer.price < watch.min_price:
        reasons.append("дешевле минимума")
    if watch.min_discount:
        if discount is None or discount < watch.min_discount:
            reasons.append("скидка мала")
    if watch.in_stock and offer.in_stock is False:
        reasons.append("нет в наличии")
    return MatchResult(True, not reasons, reasons, discount)
