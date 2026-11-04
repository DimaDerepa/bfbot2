"""Price parsing for Romanian shop formats.

Handles: "1.299,99 Lei", "1299,99 RON", "1 299,99 lei", "1,299.99", "1299.99",
"de la 4.799 lei", numbers in JSON, and eMAG's "1.299<sup>99</sup>" rendered as text.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Optional

_NUM_RE = re.compile(r"\d[\d.,\s\u00a0\u202f']*\d|\d")
# Price followed by a currency marker. The lookbehind refuses to start inside a word, so the digits of
# a model name are never glued onto the price ("Galaxy S24 999 lei" -> 999, "OLED55C4 899 lei" -> 899).
# Thousands groups use one kind of separator: "1.299.999" or "1 299 999", never "1.299 999" (an old price
# without currency next to the new one: "1.299 999 lei" is 999 lei).
_THOUSANDS = r"\d{1,3}(?:\.\d{3})+|\d{1,3}(?:[\s\u00a0\u202f]\d{3})+"
PRICE_WITH_CURRENCY_RE = re.compile(
    r"(?<![\w.,])("
    r"(?:" + _THOUSANDS + r")(?:,\d{1,2})?"                # 1.299,99 / 1 299,99 / 12.999
    r"|\d{1,3}\.\d{5}"                                   # 4.59999 (eMAG <sup> flattened)
    r"|\d+(?:[.,]\d{1,2})?"                              # 1299,99 / 89
    r"|(?:" + _THOUSANDS + r"|\d+)\s+\d{2}"               # 2.599 99 / 49 99 (decimals in their own element)
    r")\s*(?:lei|ron)\b",
    re.IGNORECASE,
)
# The last alternative above: integer part, whitespace, exactly two decimals (Carrefour "2.599 99 Lei").
_SPLIT_DECIMALS_RE = re.compile(r"(" + _THOUSANDS + r"|\d+)\s+(\d{2})")
_SEP_END_RE = re.compile(r"\d[.,]$")
_DECIMALS_START_RE = re.compile(r"\d{2}(?!\d)")      # "99", "99 lei": the decimals after a "2.999," element
_DOUBLE_COMMA_RE = re.compile(r"(\d)\s*,\s*,\s*(\d)")


def _clean_number(num: str) -> Optional[float]:
    num = re.sub(r"[\s\u00a0\u202f']", "", num)
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
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            head, tail = parts
            if len(tail) in (1, 2):
                pass  # 1299.99
            elif len(head) <= 3 and len(tail) == 5:
                # eMAG "1.299<sup>99</sup>" flattened to "1.29999"
                num = head + tail[:3] + "." + tail[3:]
            elif len(head) <= 3 and len(tail) == 3:
                num = head + tail  # 1.299 / 12.999 (thousands)
            # else a machine decimal that cannot be a thousands group: "1299.9900", "2499.000000",
            # "1599.000", "12.5000" -> leave the dot, float() reads it
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


def _currency_number(num: str) -> Optional[float]:
    """Number captured by PRICE_WITH_CURRENCY_RE (group 1)."""
    sm = _SPLIT_DECIMALS_RE.fullmatch(num)
    if sm:  # "2.599 99" -> 2599.99, "49 99" -> 49.99
        whole = re.sub(r"\D", "", sm.group(1))
        try:
            value = float(f"{whole}.{sm.group(2)}")
        except ValueError:
            return None
        return value if value > 0 else None
    return _clean_number(num)


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
    s = str(value).replace("\u00a0", " ").replace("\u202f", " ")
    s = _DOUBLE_COMMA_RE.sub(r"\1,\2", s)   # "2.999,,99 lei" (separator typed before a <sup>)
    m = PRICE_WITH_CURRENCY_RE.search(s)
    if m:
        return _currency_number(m.group(1))
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
    """Text of a BeautifulSoup element with <sup>NN</sup> decimals turned into ",NN".

    "4.599<sup>99</sup> Lei" -> "4.599,99 Lei"; "2.999,<sup>99</sup> lei" -> "2.999,99 lei" (the separator
    is already there); "2.199<sup>,99</sup>" -> "2.199,99".
    """
    if el is None:
        return ""
    el = copy.copy(el)
    for sup in el.find_all("sup"):
        t = sup.get_text("", strip=True)
        if t.isdigit() and len(t) <= 2:
            prev = sup.find_previous(string=True)
            head = str(prev).rstrip() if prev is not None else ""
            if _SEP_END_RE.search(head):
                prev.replace_with(head + t)   # "2.999," + "99"
                sup.decompose()
            else:
                sup.replace_with("," + t)
        else:
            sup.replace_with(" " + t)
    text = ""
    for s in el.stripped_strings:
        if text and _SEP_END_RE.search(text) and _DECIMALS_START_RE.match(s):
            text += s                     # "2.999," | "99" from separate elements (not "Model 2024." | "1.299 lei")
        elif text and s in (",", ".") and text[-1:].isdigit():
            text += s                     # "1.299" | "," | "99": the separator in its own element
        else:
            text = f"{text} {s}" if text else s
    return _DOUBLE_COMMA_RE.sub(r"\1,\2", text.replace(" ,", ","))


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
