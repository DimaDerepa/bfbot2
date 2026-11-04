"""Price parsing for Romanian shop formats.

Handles: "1.299,99 Lei", "1299,99 RON", "1 299,99 lei", "1,299.99", "1299.99",
"de la 4.799 lei", numbers in JSON, and eMAG's "1.299<sup>99</sup>" rendered as text.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Optional

_NUM_RE = re.compile(r"\d[\d.,\s  ']*\d|\d")
# Price followed (or preceded) by a currency marker.
PRICE_WITH_CURRENCY_RE = re.compile(
    r"(?<![\d.,])("
    r"\d{1,3}(?:[.\s  ]\d{3})+(?:,\d{1,2})?"   # 1.299,99 / 1 299,99 / 12.999
    r"|\d{1,3}\.\d{5}"                                   # 4.59999 (eMAG <sup> flattened)
    r"|\d+(?:[.,]\d{1,2})?"                              # 1299,99 / 89
    r")\s*(?:lei|ron)\b",
    re.IGNORECASE,
)


def _clean_number(num: str) -> Optional[float]:
    num = re.sub(r"[\s  ']", "", num)
    if not num:
        return None
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")  # 1.299,99
        else:
            num = num.replace(",", "")  # 1,299.99
    elif "," in num:
        head, _, tail = num.rpartition(",")
        if num.count(",") == 1 and len(tail) in (1, 2):
            num = head + "." + tail  # 1299,99
        else:
            num = num.replace(",", "")  # 1,299 (thousands)
    elif "." in num:
        parts = num.split(".")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            pass  # 1299.99
        elif len(parts) == 2 and len(parts[1]) == 5 and parts[1].isdigit():
            # eMAG "1.299<sup>99</sup>" flattened to "1.29999"
            num = parts[0] + parts[1][:3] + "." + parts[1][3:]
        elif all(len(p) == 3 for p in parts[1:]):
            num = num.replace(".", "")  # 1.299 / 12.999.000
        elif len(parts) > 2 and all(len(p) == 3 for p in parts[1:-1]) and len(parts[-1]) in (1, 2):
            num = "".join(parts[:-1]) + "." + parts[-1]  # 1.299.99 (rare)
        else:
            num = num.replace(".", "")
    try:
        value = float(num)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_price(value: Any) -> Optional[float]:
    """Parse a price from a number or arbitrary text. Returns None if absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, dict):
        for k in ("value", "amount", "price", "current", "numeral"):
            if k in value:
                return parse_price(value[k])
        return None
    s = str(value).replace(" ", " ").replace(" ", " ")
    m = PRICE_WITH_CURRENCY_RE.search(s)
    if m:
        return _clean_number(m.group(1))
    m = _NUM_RE.search(s)
    if not m:
        return None
    return _clean_number(m.group(0).strip())


def parse_minor_units(value: Any, minor_unit: int = 2) -> Optional[float]:
    """WooCommerce Store API style: "129999" with currency_minor_unit=2 -> 1299.99."""
    try:
        v = int(str(value))
    except (TypeError, ValueError):
        return parse_price(value)
    if v <= 0:
        return None
    return v / (10 ** int(minor_unit or 0))


def element_price_text(el) -> str:
    """Text of a BeautifulSoup element with <sup>NN</sup> decimals turned into ",NN"."""
    if el is None:
        return ""
    el = copy.copy(el)
    for sup in el.find_all("sup"):
        t = sup.get_text("", strip=True)
        sup.replace_with("," + t if t.isdigit() and len(t) <= 2 else " " + t)
    return el.get_text(" ", strip=True).replace(" ,", ",")


def element_price(el) -> Optional[float]:
    if el is None:
        return None
    for attr in ("data-price-amount", "data-price", "content", "data-value"):
        v = el.get(attr) if hasattr(el, "get") else None
        if v:
            p = parse_price(v)
            if p:
                return p
    return parse_price(element_price_text(el))


def fmt_price(value: Optional[float]) -> str:
    """4599.99 -> '4 599,99'; 4599.0 -> '4 599'."""
    if value is None:
        return "?"
    whole = int(value)
    cents = round((value - whole) * 100)
    if cents == 100:
        whole, cents = whole + 1, 0
    s = f"{whole:,}".replace(",", " ")
    return f"{s},{cents:02d}" if cents else s
