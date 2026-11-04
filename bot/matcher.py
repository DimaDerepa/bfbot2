"""Decide whether an offer satisfies a watch's criteria."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .models import Offer, Watch
from .text import STOPWORDS, has_term, norm, query_terms, tokens

# Accessory / spare-part titles are skipped by default unless the word is part of the query.
# ACCESSORY_PREFIX: always an accessory when among the first 3 significant words of the title
# ("Husa pentru iPhone 16", "Folie sticla ..."), so "Aspirator ... cu suport" is not affected.
ACCESSORY_PREFIX = [
    "husa", "huse", "folie", "folii", "bumper", "protectie", "sticla", "skin", "stylus", "cover", "set folii",
    "garantie", "asigurare", "abonament", "piese", "inlocuire",
]
# Category nouns that are real products too (keyboards, PC cases, power banks, chargers, bags...): an accessory only
# when the title says it is for the searched device, i.e. "pentru"/"compatibil"/"for" follows within a few words
# and the query words come after it ("Incarcator retea pentru iPhone 16"; not "Tastatura Logitech MX Keys").
ACCESSORY_CATEGORY = [
    "tastatura", "carcasa", "case", "baterie", "acumulator", "incarcator", "charger", "adaptor", "cablu", "suport",
    "curea", "geanta", "rucsac", "display", "ecran", "touchscreen", "kit",
]
ACCESSORY_ANYWHERE = [
    "screen protector", "tempered glass", "sticla securizata", "garantie extinsa", "extra garantie",
    "clear case", "silicone case", "leather case", "case with magsafe", "folio case", "smart folio", "wallet case",
]
# "...compatibil cu iPhone" is an accessory, but "Smartwatch ..., compatibil cu iPhone si Android" is a product
ACCESSORY_DEVICE_PHRASES = [
    "pentru iphone", "pentru samsung", "pentru apple", "pentru galaxy", "pentru xiaomi",
    "compatibil cu iphone", "compatibila cu iphone", "compatibil iphone",
]
ACCESSORY_WORDS = ACCESSORY_PREFIX + ACCESSORY_CATEGORY + ACCESSORY_ANYWHERE + ACCESSORY_DEVICE_PHRASES

# "X pentru <device>": the item is made for the searched product (filter, brush, controller, game...)
DEPENDENCY_WORDS = {"pentru", "for", "compatibil", "compatibila", "compatibile", "compatibili"}
# ...unless "pentru" names an audience or a use: "Parfum pentru barbati Dior Sauvage", "Cuptor pentru incastrare Bosch"
AUDIENCE_WORDS = {
    "barbati", "barbat", "femei", "femeie", "dama", "doamne", "domni", "copii", "copil", "baieti", "baiat", "fete",
    "fetite", "fata", "bebelusi", "bebe", "nou", "adulti", "adolescenti", "unisex", "el", "ea", "ei", "ele",
    "gaming", "gameri", "birou", "casa", "acasa", "bucatarie", "baie", "gradina", "terasa", "exterior", "interior",
    "incastrare", "incorporare", "uz", "par", "ten", "corp", "unghii", "buze", "ochi", "gene", "picioare", "maini",
    "piele", "barba", "sport", "sportivi", "alergare", "fitness", "antrenament", "scoala", "calatorie", "calatorii",
    "voiaj", "iarna", "vara", "munte", "drumetie", "drumetii", "ciclism", "oras", "caini", "catei", "pisici",
    "animale", "toate", "orice", "zi", "noapte", "men", "women", "kids", "boys", "girls", "him", "her", "home",
    "office", "outdoor", "indoor", "running", "travel",
}
_CATEGORY_WINDOW = 4          # "Incarcator retea Samsung 25W pentru Galaxy S24": "pentru" within 4 words of the noun
_GLUED_RE = re.compile(r"([a-z]{3,})(\d[0-9a-z.+]*)")


@dataclass
class MatchResult:
    title_ok: bool
    ok: bool
    reasons: list[str] = field(default_factory=list)
    discount: Optional[float] = None


def _term_ok(t: str, term: str) -> bool:
    """has_term, plus: a glued word + model code also matches when its parts are apart ("oled55c46la" ~
    "OLED evo 55C46LA"). A plain number stays attached ("iphone16" needs "iPhone 16", not "iPhone 15, 16GB")."""
    if has_term(t, term):
        return True
    m = _GLUED_RE.fullmatch(norm(term))
    return (bool(m) and any(c.isalpha() for c in m.group(2))
            and has_term(t, m.group(1)) and has_term(t, m.group(2)))


def _strict_terms(query: str) -> list[str]:
    """Query terms; a word followed by a number ("rtx", "5070") is also accepted glued in the title ("RTX5070")."""
    terms = query_terms(query)
    out: list[str] = []
    i = 0
    while i < len(terms):
        a = terms[i]
        if i + 1 < len(terms) and a.isalpha() and terms[i + 1][:1].isdigit():
            out.append(f"{a} {terms[i + 1]}")
            i += 2
        else:
            out.append(a)
            i += 1
    return out


def _pair_ok(t: str, term: str) -> tuple[bool, str]:
    """Check a strict term; for a merged pair "rtx 5070": glued/adjacent in the title, or both words anywhere."""
    if " " not in term:
        return _term_ok(t, term), term
    if has_term(t, term):
        return True, term
    for part in term.split(" "):
        if not _term_ok(t, part):
            return False, part
    return True, term


def _dependency_positions(toks: list[str]) -> list[int]:
    out = []
    for i, w in enumerate(toks):
        if w not in DEPENDENCY_WORDS:
            continue
        k = i + 1
        if k < len(toks) and toks[k] in ("cu", "with"):
            k += 1
        if k < len(toks) and toks[k] in AUDIENCE_WORDS:
            continue
        out.append(i)
    return out


def _accessory(watch: Watch, t: str, toks: list[str], q: str) -> Optional[str]:
    """Why the (normalised) title looks like an accessory for the searched product, or None."""
    sig = [i for i, w in enumerate(toks) if w not in STOPWORDS][:3]     # "Bormasina cu acumulator": skip "cu"
    head = " ".join(toks[i] for i in sig)
    for term in ACCESSORY_PREFIX:
        if has_term(head, term) and not has_term(q, term):
            return term
    terms = [*query_terms(watch.query or ""), *watch.must_include]
    if terms and not any(w in DEPENDENCY_WORDS for w in q.split()):
        deps = _dependency_positions(toks)

        def after(i: int) -> bool:
            rest = " ".join(toks[i + 1:])
            return any(has_term(rest, x) for x in terms)

        if deps:
            i = deps[0]
            before = " ".join(toks[:i])
            if not any(has_term(before, x) for x in terms) and after(i):
                return f"{toks[0]} … {toks[i]} …"
        for j in sig:
            if toks[j] in ACCESSORY_CATEGORY and not has_term(q, toks[j]):
                if any(j < i <= j + _CATEGORY_WINDOW and after(i) for i in deps):
                    return toks[j]
    for term in ACCESSORY_ANYWHERE:
        if has_term(t, term) and not has_term(q, term):
            return term
    if not has_term(t, "android"):
        for term in ACCESSORY_DEVICE_PHRASES:
            if has_term(t, term) and not has_term(q, term):
                return term
    return None


def title_matches(watch: Watch, title: str) -> tuple[bool, str]:
    t = norm(title)
    if watch.must_include:
        for term in watch.must_include:
            if not _term_ok(t, term):
                return False, f"нет «{term}»"
    elif watch.strict and watch.query:
        terms = _strict_terms(watch.query)
        if not terms:
            # e.g. an all-Russian query: norm() drops Cyrillic, and "no terms" must not mean "everything matches"
            return False, "в запросе нет латинских слов"
        for term in terms:
            ok, missing = _pair_ok(t, term)
            if not ok:
                return False, f"нет «{missing}»"
    if watch.any_of and not any(has_term(t, term) for term in watch.any_of):
        return False, "нет ни одного из any_of"
    for term in watch.exclude:
        if has_term(t, term):
            return False, f"исключено «{term}»"
    if watch.exclude_accessories:
        q = norm(" ".join([watch.query or "", *watch.must_include, *watch.any_of]))
        why = _accessory(watch, t, tokens(title), q)
        if why:
            return False, f"аксессуар «{why}»"
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
    if check_title:
        from .specs import spec_reasons

        spec_fail = spec_reasons(watch, offer.title)
        if spec_fail:
            # wrong RAM / too old model: not the kind of product the user wants -> treat like a title mismatch
            return MatchResult(False, False, spec_fail)
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
