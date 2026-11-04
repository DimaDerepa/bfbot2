"""Currency conversion to RON using the National Bank of Romania daily rates (fallback: fixed rates)."""
from __future__ import annotations

import re
import time
from typing import Optional

FALLBACK = {"RON": 1.0, "USD": 4.45, "EUR": 5.08, "GBP": 5.85}
_cache: dict = {"t": 0.0, "rates": dict(FALLBACK)}

ALIASES = {
    "$": "USD", "usd": "USD", "дол": "USD", "долл": "USD", "доллар": "USD", "долларов": "USD", "доллара": "USD",
    "бакс": "USD", "баксов": "USD", "€": "EUR", "eur": "EUR", "euro": "EUR", "евро": "EUR", "£": "GBP",
    "gbp": "GBP", "лей": "RON", "леев": "RON", "лея": "RON", "lei": "RON", "ron": "RON", "leu": "RON",
}


def currency_of(token: str) -> Optional[str]:
    t = (token or "").strip().lower().rstrip(".")
    if t in ALIASES:
        return ALIASES[t]
    for k, v in ALIASES.items():
        if len(k) > 2 and t.startswith(k):
            return v
    return None


def rates() -> dict:
    if time.time() - _cache["t"] < 6 * 3600:
        return _cache["rates"]
    try:
        import requests

        r = requests.get("https://www.bnr.ro/nbrfxrates.xml", timeout=10)
        found = {c: float(v) / float(m or 1) for c, m, v in
                 re.findall(r'<Rate currency="([A-Z]{3})"(?: multiplier="(\d+)")?>([\d.]+)</Rate>', r.text)}
        if found.get("USD") and found.get("EUR"):
            _cache["rates"] = {**FALLBACK, **found, "RON": 1.0}
    except Exception:
        pass
    _cache["t"] = time.time()
    return _cache["rates"]


def to_ron(amount: float, currency: Optional[str]) -> float:
    if not currency or currency == "RON":
        return amount
    return round(amount * rates().get(currency, FALLBACK.get(currency, 1.0)), 0)
