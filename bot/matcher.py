"""Decide whether an offer satisfies a watch's criteria."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .models import Offer, Watch
from .text import STOPWORDS, UNITS, has_cyrillic, has_term, norm, query_terms, split_glued, tokens

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
    "seniori", "varstnici", "bunici", "elevi", "studenti", "incepatori", "profesionisti", "profesional",
    # what the product works on: "Espressor pentru cafea boabe De'Longhi", "Cana filtranta pentru apa Brita"
    "apa", "cafea", "ceai", "paine", "alimente", "rufe", "vase", "haine", "desen", "scris", "gatit", "curatenie",
    "masaj", "dormit", "motor",
}
# "Filtru HEPA pentru aspirator Dyson V15": the searched words follow "pentru" at once (within 2 words); in
# "Capsule pentru masina de spalat vase Finish Quantum" the brand comes later, so the product itself is meant
_DEPENDENCY_WINDOW = 2
# generic device nouns do not use up that window: "Incarcator pentru telefon Apple iPhone 16"
DEVICE_NOUNS = {
    "telefon", "telefoane", "telefonul", "smartphone", "smartphoneuri", "mobil", "laptop", "laptopuri", "notebook",
    "tableta", "tablete", "consola", "console", "ceas", "ceasuri", "smartwatch", "televizor", "televizoare", "tv",
    "monitor", "monitoare", "aspirator", "aspiratoare", "robot", "camera", "drona", "imprimanta", "router",
    "controller", "controllere", "casti", "boxa", "pc", "calculator", "device", "dispozitiv", "dispozitive",
    "phone", "phones", "tablet", "console", "watch",
}
_CATEGORY_WINDOW = 4          # "Incarcator retea Samsung 25W pentru Galaxy S24": "pentru" within 4 words of the noun
_GLUED_RE = re.compile(r"([a-z]{3,})(\d[0-9a-z.+]*)")
_DIGIT_SUFFIX_RE = re.compile(r"(?<=\d)([a-z]{2,})(?![a-z])")      # "5070ti" -> "5070 ti", not "16e" / "256gb"


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
    if term.lower().startswith("re:"):
        return False
    nt = norm(term)
    # "rtx5070ti" / "7800xt" ~ "RTX 5070 Ti" / "RX 7800 XT": a model suffix of 2+ letters may stand apart
    alt = _DIGIT_SUFFIX_RE.sub(lambda m: m.group(1) if m.group(1) in UNITS else " " + m.group(1), nt)
    if alt != nt and has_term(t, alt):
        return True
    m = _GLUED_RE.fullmatch(nt)
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


def _dependency_positions(toks: list[str]) -> list[tuple[int, int]]:
    """(index of "pentru"/"for"/"compatibil", index of the first word it governs), skipping audience/use words."""
    out = []
    for i, w in enumerate(toks):
        if w not in DEPENDENCY_WORDS:
            continue
        k = i + 1
        if k < len(toks) and toks[k] in ("cu", "with"):
            k += 1
        if k < len(toks) and toks[k] in AUDIENCE_WORDS:
            continue
        out.append((i, k))
    return out


def _term_words(terms: list[str]) -> set[str]:
    """Single words of the query terms, also the letter part of glued ones ("iphone16" -> "iphone")."""
    out: set[str] = set()
    for x in terms:
        if x.lower().startswith("re:"):
            continue
        for w in norm(x).replace("-", " ").split():
            out.add(w)
            head = split_glued(w).split(" ")[0]
            if len(head) >= 3:          # not the "s" of "s24"
                out.add(head)
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
        words = _term_words(terms)
        regex_terms = [x for x in terms if x.lower().startswith("re:")]

        def governs(k: int, n: int) -> bool:
            """A query word is among the first n significant words from toks[k] on (device nouns not counted)."""
            near: list[str] = []
            for w in toks[k:]:
                if w in words:
                    return True
                if w in STOPWORDS or w in DEVICE_NOUNS:
                    continue
                near.append(w)
                if len(near) >= n:
                    break
            return any(has_term(" ".join(near), x) for x in terms)

        def lead(k: int) -> bool:
            """The word right after "pentru" is a query word: "Incarcator Samsung pentru Galaxy S24"."""
            return k < len(toks) and (toks[k] in words or any(has_term(toks[k], x) for x in regex_terms))

        for i, k in deps:
            before = " ".join(toks[:i])
            if any(has_term(before, x) for x in terms):
                break                       # the searched product is named before "pentru": it is the item itself
            if governs(k, _DEPENDENCY_WINDOW):
                return f"{toks[0]} … {toks[i]} …"
        for j in sig:
            if toks[j] in ACCESSORY_CATEGORY and not has_term(q, toks[j]):
                # "Incarcator retea Samsung 25W pentru Galaxy S24" (not "Geanta Samsonite pentru laptop Spectrolite")
                if any(j < i <= j + _CATEGORY_WINDOW and lead(k) for i, k in deps):
                    return toks[j]
    for term in ACCESSORY_ANYWHERE:
        if has_term(t, term) and not has_term(q, term):
            return term
    if not has_term(t, "android"):
        tt = t.replace("-", " ")
        for term in ACCESSORY_DEVICE_PHRASES:
            if has_term(t, term) and not has_term(q, term):
                m = re.search(rf"(?<![0-9a-z]){re.escape(term)}(?![0-9a-z])", tt)
                before = tt[:m.start()] if m else ""
                if (terms and all(has_term(before, x) for x in terms)
                        and not any(has_term(before, a) and not has_term(q, a) for a in ACCESSORY_PREFIX)):
                    continue                # "Apple Watch Series 10 GPS 46mm, compatibil cu iPhone" is the watch
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
        if not terms or (has_cyrillic(watch.query) and not any(c.isalpha() for x in terms for c in x)):
            # e.g. an all-Russian query: norm() drops Cyrillic, and "no terms" must not mean "everything matches";
            # "айфон 16 про" (stored before /add refused Cyrillic) would otherwise match anything with "16"
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
