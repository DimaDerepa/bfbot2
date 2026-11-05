"""Product specs read from titles: RAM size and model release year (phones), for parameter-based watches.

Shops do not publish the launch year in listing titles, so years come from a table of model families.
Unknown -> None (the watch decides whether unknown passes: Watch.allow_unknown_specs).
"""
from __future__ import annotations

import re
from typing import Optional

from .text import norm

# ------------------------------------------------------------------ RAM
_RAM_EXPLICIT = [
    re.compile(r"(?<!\d)(\d{1,2})\s*gb\s*(?:de\s*)?(?:ram|memorie ram|lpddr\d*x?|ddr\d)"),
    re.compile(r"(?:ram|memorie)\s*:?\s*(\d{1,2})\s*gb"),
    re.compile(r"(?<!\d)(\d{1,2})\s*(?:gb)?\s*[/+]\s*\d{2,4}\s*(?:gb|tb)"),   # 12/256GB, 12GB/512GB, 12+256GB
]
_GB_NUM = re.compile(r"(?<![\d.])(\d{1,4})\s*(gb|tb)(?![a-z])")


# models whose titles never state RAM (Apple)
_MODEL_RAM = [(re.compile(r"(?<![a-z0-9])" + p), r) for p, r in [
    (r"iphone 17 pro", 12), (r"iphone air", 12), (r"iphone 17e", 8), (r"iphone 17", 8), (r"iphone 16", 8),
    (r"iphone 15 pro", 8), (r"iphone 15", 6), (r"iphone 14", 6),
]]


def ram_gb(title: str) -> Optional[int]:
    r = _ram_from_text(title)
    if r is None:
        t = norm(title or "")
        for rx, v in _MODEL_RAM:
            if rx.search(t):
                return v
    return r


def _ram_from_text(title: str) -> Optional[int]:
    t = (title or "").lower().replace(" ", " ")
    for rx in _RAM_EXPLICIT:
        m = rx.search(t)
        if m:
            v = int(m.group(1))
            if 1 <= v <= 64:
                return v
    # "12GB, 256GB": the small number is RAM when a storage-sized number is also present
    nums = [int(n) * (1024 if u == "tb" else 1) for n, u in _GB_NUM.findall(t)]
    if len(nums) >= 2:
        small, big = min(nums), max(nums)
        if small <= 24 and big >= 64:
            return small
    return None


# ------------------------------------------------------------------ release year
# (pattern on normalised title, year). Most specific first. Phones sold in Romania, global launch year.
_MODEL_YEARS: list[tuple[str, int]] = [
    # Samsung
    (r"galaxy s26", 2026), (r"galaxy z (fold|flip) ?8", 2026), (r"galaxy a(57|37|27)", 2026),
    (r"galaxy s25", 2025), (r"galaxy z (fold|flip) ?7", 2025), (r"galaxy a(56|36|26|17|07)(?!\d)", 2025),
    (r"galaxy m(56|36|16|06)(?!\d)", 2025), (r"galaxy xcover ?7 pro", 2025), (r"galaxy tab s11", 2025),
    (r"galaxy s24", 2024), (r"galaxy z (fold|flip) ?6", 2024), (r"galaxy a(55|35|25|16|15|06|05)(?!\d)", 2024),
    (r"galaxy s23", 2023), (r"galaxy a(54|34|24|14)(?!\d)", 2023),
    # Apple
    (r"iphone 18", 2026), (r"iphone 17e", 2026), (r"iphone (17|air)(?![0-9])", 2025), (r"iphone 16e", 2025),
    (r"iphone 16", 2024), (r"iphone 15", 2023), (r"iphone 14", 2022), (r"iphone 13", 2021),
    # Google
    (r"pixel 11", 2026), (r"pixel 10a", 2026), (r"pixel 10", 2025), (r"pixel 9a", 2025), (r"pixel 9", 2024),
    (r"pixel 8", 2023),
    # Xiaomi / Redmi / POCO
    (r"xiaomi 17", 2025), (r"xiaomi 15", 2025), (r"xiaomi 14", 2024), (r"xiaomi 13", 2023),
    (r"redmi note 15", 2025), (r"redmi note 14", 2025), (r"redmi note 13", 2024), (r"redmi note 12", 2023),
    (r"redmi 15", 2025), (r"redmi a5", 2025), (r"redmi 14c", 2024), (r"redmi 13", 2024),
    (r"poco f8", 2025), (r"poco (f7|x7|m7)", 2025), (r"poco (f6|x6|m6)", 2024),
    # OnePlus / Nothing
    (r"oneplus 15", 2025), (r"oneplus 13", 2025), (r"oneplus nord ?(5|ce ?5)(?!\d)", 2025), (r"oneplus 12", 2024),
    (r"oneplus nord ?(4|ce ?4)(?!\d)", 2024),
    (r"nothing phone ?\(?3", 2025), (r"cmf phone ?2", 2025), (r"nothing phone ?\(?2a", 2024),
    # Honor / Huawei
    (r"honor magic ?8", 2025), (r"honor magic ?7", 2025), (r"honor 400", 2025), (r"honor x(9c|8c|7c|6c)", 2025),
    (r"honor 200", 2024), (r"honor magic ?6", 2024), (r"honor x(9b|8b|7b)", 2024),
    (r"huawei (pura ?80|nova ?14|mate ?x7)", 2025), (r"huawei (pura ?70|nova ?13|mate ?70|mate ?x6)", 2024),
    # Oppo / realme / vivo
    (r"oppo (reno ?1[45]|find x9|find n5|a5 pro|a5x?)(?![0-9])", 2025), (r"oppo (reno ?1[23]|find x8)", 2024),
    (r"realme (gt ?8|16)(?![0-9])", 2026), (r"realme (gt ?7|15|14|c75|c71|note ?60)(?![0-9])", 2025),
    (r"realme (gt ?6|13|12|c67|c65)(?![0-9])", 2024),
    (r"vivo (x300|v60|x200|v50|y29)(?![0-9])", 2025), (r"vivo (x100|v40|v30)(?![0-9])", 2024),
    # Sony / Asus / Motorola
    (r"xperia (1|10) vii", 2025), (r"xperia (1|10) vi(?!i)", 2024),
    (r"zenfone ?12", 2025), (r"rog phone ?9", 2024),
    (r"(edge ?60|moto g ?(56|86|06|15|05)|razr ?60)", 2025), (r"(edge ?50|moto g ?(55|85|35|24|04)|razr ?50)", 2024),
]
_MODEL_RX = [(re.compile(r"(?<![a-z0-9])" + p), y) for p, y in _MODEL_YEARS]
_YEAR_IN_TITLE = re.compile(r"(?<![\d])(20[12]\d)(?![\d])")


def release_year(title: str) -> Optional[int]:
    t = norm(title or "")
    for rx, y in _MODEL_RX:
        if rx.search(t):
            return y
    m = _YEAR_IN_TITLE.search(t)
    if m:
        y = int(m.group(1))
        if 2015 <= y <= 2030:
            return y
    return None


_INCH_RX = [re.compile(r"(?<![\d.])(\d{2,3}(?:[.,]\d)?)\s*(?:inch|inchi|\"|”|″|''|in\b|toli)", re.I)]
_CM_RX = re.compile(r"(?<![\d.])(\d{2,3})\s*cm\b", re.I)
_CODE_RX = re.compile(r"(?<![0-9a-z])(?:oled|qled|qned|ue|qe|kd|xr|tx|tq|tu|un)?(\d{2})(?=[a-z]{1,6}\d)", re.I)


def screen_inches(title: str) -> Optional[float]:
    t = (title or "").replace("\u00a0", " ")
    for rx in _INCH_RX:
        m = rx.search(t)
        if m:
            v = float(m.group(1).replace(",", "."))
            if 10 <= v <= 120:
                return v
    m = _CM_RX.search(t)
    if m:
        v = int(m.group(1)) / 2.54
        if 20 <= v <= 120:
            return round(v, 1)
    m = _CODE_RX.search(t.lower())
    if m:
        v = int(m.group(1))
        if 24 <= v <= 100:
            return float(v)
    return None


def spec_reasons(watch, title: str) -> list[str]:
    """Reasons why `title` fails the watch's spec criteria (empty list = passes)."""
    reasons: list[str] = []
    unknown_ok = bool(getattr(watch, "allow_unknown_specs", False))
    ram_min = getattr(watch, "ram_min", None)
    if ram_min:
        r = ram_gb(title)
        if r is None:
            if not unknown_ok:
                reasons.append("ОЗУ не указана")
        elif r < ram_min:
            reasons.append(f"ОЗУ {r} ГБ")
    inch = getattr(watch, "screen_inch", None)
    if inch:
        v = screen_inches(title)
        if v is None:
            if not unknown_ok:
                reasons.append("диагональ не указана")
        elif abs(v - inch) > 1.5:
            reasons.append(f'диагональ {v:g}"')
    year_min = getattr(watch, "year_min", None)
    if year_min:
        y = release_year(title)
        if y is None:
            if not unknown_ok:
                reasons.append("год неизвестен")
        elif y < year_min:
            reasons.append(f"модель {y} года")
    return reasons


# ------------------------------------------------------------------ model identity (dedupe across shops/colours)
_COLOURS = set("""
black negru neagra negre white alb alba albe blue albastru albastra navy silver argintiu argintie gold auriu aurie
gray grey gri green verde red rosu rosie pink roz purple mov violet yellow galben orange portocaliu bronze copper
beige cream crem lavender mint coral graphite midnight starlight titanium natural desert cosmic deep space jet onyx
cobalt icy icyblue phantom obsidian porcelain hazel peony iris frost moonstone aloe lemongrass ultramarine teal
mystic sky sand ocean aurora arctic charcoal ink amber jade lime plum rose sage slate stone storm sunset tidal
glazed matte mat lucios glossy shadow light dark marble pebble titan carbon nebula lunar solar polar velvet pearl
""".split())
_GENERIC = set("""
telefon mobil smartphone smart televizor tv led laptop notebook ultrabook tableta monitor 5g 4g lte dual sim esim nano
ram rom memorie stocare interna cm inch inchi hz clasa model nou noua new cu si de din pentru la in the and with
full hd uhd fhd qhd hdr hdr10 wifi wi-fi bluetooth nfc android ios google tizen webos vidaa sistem operare garantie
oferta pret versiune international eu ro edition editie global resigilat placa video procesor sm
""".split())
_STORAGE = re.compile(r"^\d+(gb|tb)$")


# free extras ("+ Woven Strap Ring Stand", "cadou husa") are not part of the model; "+ 2 controllere" is
_BUNDLE = re.compile(r"\s(\+(?![^\n]*\d)|\bcu cadou\b|\bcadou\b|\bbonus\b).*$", re.I)


def model_key(title: str) -> str:
    """Same product from different shops / in different colours -> same key (best effort without Claude)."""
    t = norm(_BUNDLE.sub("", title or ""))
    t = re.sub(r"\b(ultra|full)\s+hd\b|\bhd\s+ready\b|\b4k\b|\b8k\b", " ", t)
    t = re.sub(r"\b\d{2,3}\s*cm\b|\b\d{2,3}\s*hz\b|\bclasa\s+[a-g]\b|\b\d{2,4}\s*-?\s*bit\b", " ", t)
    toks = []
    for tok in re.findall(r"[0-9a-z]+(?:[.+][0-9a-z]+)*", t):
        if tok in _COLOURS or tok in _GENERIC:
            continue
        toks.append(tok)
    # TV / monitor model codes ("55c46la", "oled55c46la", "ue55du7172") identify the product on their own
    codes = sorted({re.sub(r"^(oled|qled|qned|ue|qe|kd|xr|tx|tq|tu|un)(?=\d)", "", x) for x in toks
                    if re.fullmatch(r"(oled|qled|qned|ue|qe|kd|xr|tx|tq|tu|un)?\d{2}[a-z]+\d*[a-z0-9]*", x) and len(x) >= 5})
    if codes:
        return "code:" + codes[0]
    # shop part numbers ("mymj3zd", "sms931bzbdeue") differ between shops: drop them
    toks = [x for x in toks if (len(x) > 1 or x.isdigit()) and not (len(x) >= 6 and re.search(r"[a-z]", x) and re.search(r"\d", x)
                                                    and not _STORAGE.match(x))]
    storage = sorted({x for x in toks if _STORAGE.match(x)}, key=lambda x: (x.endswith("tb"), int(re.match(r"\d+", x).group())))
    rest = [x for x in toks if not _STORAGE.match(x)]
    return " ".join(sorted(set(rest)) + storage)[:120]
