"""Free-text product requests in Russian (or mixed) -> watch spec.

  "телефон 12 гб оперативки 2025 и позже не моторола до 1000 долларов скидка больше 20%"
  -> query "telefon 12gb ram", must_include [re:telefon|smartphone], ram_min 12, year_min 2025,
     exclude [motorola], max_price 4450 (RON), min_discount 20
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .fx import currency_of, to_ron
from .text import has_cyrillic

# category word (Russian stem regex) -> (search query words, title regex the product must match, store category)
CATEGORIES: list[tuple[str, str, str, str]] = [
    (r"робот\w*[- ]?пылесос\w*", "aspirator robot", r"re:aspirator\w* robot|robot\w* (de )?aspir", "appliances"),
    (r"(смартфон|телефон|мобильн)\w*", "telefon", r"re:telefon|smartphone", "electronics"),
    (r"ноутбук\w*|лэптоп\w*|лептоп\w*", "laptop", r"re:laptop|notebook|ultrabook|macbook", "pc"),
    (r"планшет\w*", "tableta", r"re:tablet|ipad", "electronics"),
    (r"телевизор\w*|телек\w*", "televizor", r"re:televizor|smart tv|\btv\b", "electronics"),
    (r"монитор\w*", "monitor", r"re:monitor", "pc"),
    (r"видеокарт\w*", "placa video", r"re:placa video|geforce|radeon|rtx|\brx ?\d", "pc"),
    (r"процессор\w*", "procesor", r"re:procesor|ryzen|core (i|ultra)", "pc"),
    (r"наушник\w*|аирподс\w*", "casti", r"re:casti|headphones|earbuds|airpods", "electronics"),
    (r"(смарт[- ]?)?часы|смартвотч\w*", "smartwatch", r"re:smartwatch|ceas inteligent|watch", "electronics"),
    (r"приставк\w*|консол\w*", "consola", r"re:consola|playstation|xbox|nintendo", "electronics"),
    (r"пылесос\w*", "aspirator", r"re:aspirator", "appliances"),
    (r"кофемашин\w*|эспрессо", "espressor", r"re:espressor|expresor|cafetier", "appliances"),
    (r"холодильник\w*", "frigider", r"re:frigider|combina frigorifica", "appliances"),
    (r"стиральн\w* машин\w*|стиралк\w*", "masina de spalat", r"re:masina de spalat", "appliances"),
    (r"посудомо\w*", "masina de spalat vase", r"re:spalat vase", "appliances"),
    (r"кондиционер\w*", "aer conditionat", r"re:aer conditionat", "appliances"),
    (r"фотоаппарат\w*|камер\w*", "aparat foto", r"re:aparat foto|camera", "electronics"),
    (r"кроссовк\w*", "adidasi", r"re:adidasi|sneakers|pantofi sport", "fashion"),
    (r"куртк\w*", "geaca", r"re:geaca|jacheta", "fashion"),
    (r"духи|парфюм\w*|туалетн\w* вод\w*", "parfum", r"re:parfum|eau de|edp|edt", "beauty"),
]

BRANDS = {
    "моторол": "motorola", "самсунг": "samsung", "сяоми": "xiaomi", "ксиоми": "xiaomi", "шаоми": "xiaomi",
    "редми": "redmi", "поко": "poco", "айфон": "iphone", "эпл": "apple", "апл": "apple", "хуавей": "huawei",
    "хуавэй": "huawei", "хонор": "honor", "реалми": "realme", "оппо": "oppo", "виво": "vivo", "нокиа": "nokia",
    "гугл": "google", "пиксель": "pixel", "ванплас": "oneplus", "сони": "sony", "леново": "lenovo",
    "асус": "asus", "асер": "acer", "делл": "dell", "техно": "tecno", "инфиникс": "infinix", "нотинг": "nothing",
    "lg": "lg", "элджи": "lg", "филипс": "philips", "дайсон": "dyson", "ровента": "rowenta", "бош": "bosch",
    "делонги": "delonghi", "найк": "nike", "адидас": "adidas", "пума": "puma", "zte": "zte", "блэквью": "blackview",
}

_NUM = r"(\d[\d\s.,]*\d|\d)"


def _brand(word: str) -> Optional[str]:
    w = word.lower().strip(".,;:!?\"'«»()")
    if not w:
        return None
    if re.fullmatch(r"[a-z][a-z0-9+\-]*", w):
        return w
    for stem, brand in BRANDS.items():
        if w.startswith(stem):
            return brand
    return None


def _amount(num: str, mult: str = "") -> Optional[float]:
    s = num.replace(" ", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    if mult and re.match(r"(к|k|тыс)", mult.lower()):
        v *= 1000
    return v


_OPERATOR_WORDS = re.compile(r"\b(до|от|скидк\w*|лей|леев|лея|гб|тб|и|или)\b", re.I)


def anchored(rx_term: str) -> str:
    """'re:televizor|tv' -> product-type regex that must match within the first three title words."""
    body = rx_term[3:] if rx_term.startswith("re:") else rx_term
    return "re:^(?:\\S+\\s+){0,2}(?:" + body + ")"


def looks_natural(text: str) -> bool:
    """Russian description (needs this parser) vs. compact syntax that only uses до/от/скидка as operators."""
    return has_cyrillic(_OPERATOR_WORDS.sub(" ", text or ""))


def parse(text: str) -> dict[str, Any]:
    t = " " + re.sub(r"\s+", " ", text.strip()) + " "
    low = t.lower()
    name = " ".join(text.split())
    if len(name) > 60:
        name = name[:60].rsplit(" ", 1)[0] + "…"
    spec: dict[str, Any] = {"name": name}
    used: list[tuple[int, int]] = []

    def take(m: re.Match) -> None:
        used.append(m.span())

    # --- prices (with currency)
    cur_re = r"(\$|€|£|usd|eur|euro|евро|долл\w*|доллар\w*|бакс\w*|лей|леев|лея|lei|ron)?"
    for m in re.finditer(rf"(?:до|не дороже|дешевле|меньше|максимум|<)\s*{_NUM}\s*(к|тыс\.?|k)?\s*{cur_re}", low):
        v = _amount(m.group(1), m.group(2) or "")
        if v:
            spec["max_price"] = to_ron(v, currency_of(m.group(3) or "") or "RON")
            take(m)
    for m in re.finditer(rf"(?:от|дороже|больше|>)\s*{_NUM}\s*(к|тыс\.?|k)?\s*{cur_re}(?!\s*(?:гб|gb|%|год|г\.))", low):
        if re.search(r"скидк\w*\s*$", low[:m.start()]):
            continue
        v = _amount(m.group(1), m.group(2) or "")
        if v and (m.group(3) or v >= 50) and not (2015 <= v <= 2030 and not m.group(3)):
            spec["min_price"] = to_ron(v, currency_of(m.group(3) or "") or "RON")
            take(m)
    # --- discount
    m = re.search(r"скидк\w*\s*(?:больше|от|не меньше|минимум|более|>|>=)?\s*(\d{1,2})\s*%?", low) or \
        re.search(r"(\d{1,2})\s*%\s*скидк\w*", low)
    if m:
        spec["min_discount"] = float(m.group(1))
        take(m)
    # --- screen size: 55 дюймов / 55" / 55 inch
    m = re.search(r"(\d{2,3})\s*(?:дюйм\w*|\"|”|inch\w*|in\b)", low)
    if m:
        spec["screen_inch"] = int(m.group(1))
        take(m)
    # --- RAM
    m = re.search(r"(\d{1,2})\s*(?:гб|gb|гиг\w*)?\s*(?:оперативк\w*|оперативной памяти|озу|ram)", low) or \
        re.search(r"(?:оперативк\w*|озу|ram)\s*(?:от|не меньше|минимум)?\s*(\d{1,2})\s*(?:гб|gb)?", low)
    if m:
        spec["ram_min"] = int(m.group(1))
        take(m)
    # --- storage ("256 гб", "1 тб")
    for m in re.finditer(r"(\d{2,4}\s*(?:гб|gb)|\d{1,2}\s*(?:тб|tb))(?!\s*(?:оперативк|озу|ram))", low):
        if any(a <= m.start() < b for a, b in used):
            continue
        num = re.match(r"\d+", m.group(1)).group(0)
        unit = "tb" if re.search(r"(тб|tb)$", m.group(1)) else "gb"
        spec.setdefault("must_include", []).append(f"{num}{unit}")
        take(m)
    # --- year
    m = re.search(r"(?:от|не старше|с|начиная с|выпуска)?\s*(20[12]\d)\s*(?:г\.?|год\w*)?\s*(?:и\s*)?(?:позже|новее|выше|\+|и далее)", low) or \
        re.search(r"(?:год\w* выпуска|выпуска|от|не старше|с)\s*(20[12]\d)", low) or \
        re.search(r"(20[12]\d)\s*(?:г\.?|год\w*)", low)
    if m:
        spec["year_min"] = int(m.group(1))
        take(m)
    # --- excluded brands / words: "не моторола", "без xiaomi", "кроме samsung и apple"
    for m in re.finditer(r"(?:\bне\b|\bбез\b|\bкроме\b|\bисключ\w*)\s+([^\s,]+)((?:\s*(?:,|и|или)\s*[^\s,]+)*)", low):
        words = [m.group(1)] + re.split(r"\s*(?:,|\bи\b|\bили\b)\s*", m.group(2) or "")
        brands = [b for b in (_brand(w) for w in words) if b]
        if brands:
            spec.setdefault("exclude", []).extend(brands)
            take(m)
    # --- compact-syntax tokens also work here: -word excludes, +word is required
    for m in re.finditer(r"(?<!\S)([+\-])([a-zа-яё0-9][\w\-]*)", low):
        if m.group(2).isdigit():
            continue
        spec.setdefault("exclude" if m.group(1) == "-" else "must_include", []).append(m.group(2))
        take(m)
    # --- category
    category = None
    for pat, q, title_rx, cat in CATEGORIES:
        m = re.search(pat, low)
        if m:
            category = (q, title_rx, cat)
            take(m)
            break
    # --- remaining words: positive brands -> any_of, latin words -> query
    rest = low
    for a, b in sorted(used, reverse=True):
        rest = rest[:a] + " " * (b - a) + rest[b:]
    latin: list[str] = []
    brands_pos: list[str] = []
    # "asus или lenovo" / "samsung, xiaomi или poco" -> any of these brands
    for m in re.finditer(r"([a-zа-яё0-9+\-]+)((?:\s*(?:,|\bили\b)\s*[a-zа-яё0-9+\-]+)+)", rest):
        if "или" not in m.group(0):
            continue
        words = [m.group(1)] + [w for w in re.split(r"\s*(?:,|\bили\b)\s*", m.group(2)) if w]
        bs = [_brand(w) for w in words]
        if all(bs):
            brands_pos.extend(bs)
            rest = rest[:m.start()] + " " * (m.end() - m.start()) + rest[m.end():]
    for w in re.findall(r"[a-zа-яё0-9+\-]+", rest):
        if re.fullmatch(r"[a-z0-9+\-]+", w):
            latin.append(w)
            continue
        b = _brand(w)
        if b and b not in spec.get("exclude", []) and w not in ("или", "и"):
            brands_pos.append(b)
    query_words: list[str] = []
    if category:
        query_words.append(category[0])
        # the product type must be how the title STARTS ("Televizor ...", "Telefon mobil ...") — this is what
        # keeps "Suport TV", "Telecomanda TV", "Husa telefon" out
        spec.setdefault("must_include", []).insert(0, anchored(category[1]))
        spec["categories"] = [category[2]]
        if category[0] in ("televizor", "monitor") and not spec.get("screen_inch"):
            nums = [w for w in latin if w.isdigit() and 19 <= int(w) <= 100]
            if nums:
                spec["screen_inch"] = int(nums[0])
                latin = [w for w in latin if w != nums[0]]
    query_words += latin
    # every Latin word the user typed (oled, rtx, 5070, s25 ...) must appear in the title
    for w in latin:
        if w not in spec.get("must_include", []) and len(w) >= 2:
            spec.setdefault("must_include", []).append(w)
    if spec.get("screen_inch") and category and category[0] in ("televizor", "monitor"):
        query_words.append(str(spec["screen_inch"]))
    if brands_pos:
        if len(brands_pos) == 1 and not latin:
            query_words.append(brands_pos[0])
            spec.setdefault("must_include", []).append(brands_pos[0])
        else:
            spec["any_of"] = brands_pos
    if spec.get("ram_min"):
        query_words.append(f"{spec['ram_min']}gb ram")
    for s in spec.get("must_include", []):
        if not s.startswith("re:") and s not in query_words:
            query_words.append(s)
    if not query_words:
        raise ValueError("Не понял, что искать. Пример: <code>телефон 12 гб оперативки до 1000$</code> или <code>rtx 5070 до 3000</code>")
    spec["query"] = " ".join(dict.fromkeys(query_words))
    spec["intent"] = " ".join(text.split())[:300]
    if category or spec.get("ram_min") or spec.get("year_min"):
        spec["pages"] = 3          # parameter search: look deeper than the first result page
    return spec
