"""config.yaml loading + parsing of the compact watch syntax used by Telegram /add."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Optional
from urllib.parse import urlsplit

import yaml

from .models import Watch
from .text import has_cyrillic

_WATCH_FIELDS = {f.name for f in fields(Watch)}

LATIN_HINT = "Пишите название латиницей, как на сайте магазина (например: aspirator dyson, iphone 16 pro)"


@dataclass
class Settings:
    default_stores: Any = "all"            # "all" or list of store keys
    default_categories: Optional[list[str]] = None
    disabled_stores: list[str] = field(default_factory=list)
    notify_drop_pct: float = 2.0           # re-alert when price drops >= N% below last alerted price
    first_run_digest: int = 8              # new watch: send the N cheapest matches in one message
    max_alerts_per_watch: int = 12         # per run
    max_pages: int = 1
    request_delay: float = 1.5             # seconds between requests to the same host
    timeout: float = 25.0
    workers: int = 8
    store_fail_alert_after: int = 6        # consecutive failed runs before a warning message
    history_days: int = 45
    interval_minutes: int = 10             # daemon mode only
    store_every: dict = field(default_factory=dict)   # {store_key: N} -> query that store only every N-th run
    # shops that block GitHub's cloud: checked by the home runner (watch-home.yml) when HOME_RUNNER=true
    home_stores: list[str] = field(default_factory=lambda: [
        "emag", "pcgarage", "evomag", "cel", "forit", "vexio", "leroymerlin", "compari", "pricero"])
    dedupe_models: bool = True             # one model = one offer (the cheapest shop), others listed under it
    quiet_hours: Optional[str] = "23-8"    # Bucharest time: alerts still arrive, but without sound; null = always loud
    shops: list[dict] = field(default_factory=list)   # custom shops (platform adapters)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Config:
    settings: Settings
    watches: list[Watch]


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def check_latin(d: dict) -> None:
    """Raise ValueError when the words matched against shop titles (query, must_include, any_of, exclude) contain
    Cyrillic: titles are Romanian/English, norm() drops Cyrillic, so such a word never matches anything and an
    all-Russian query used to match every search result."""
    bad: list[str] = []
    q = d.get("query")
    if isinstance(q, str):
        bad += [w for w in q.split() if has_cyrillic(w)]
    for k in ("must_include", "any_of", "exclude"):
        v = d.get(k) or []
        for term in ([v] if isinstance(v, str) else v):
            if has_cyrillic(str(term)):
                bad.append(str(term))
    if bad:
        raise ValueError(f"{LATIN_HINT}. На кириллице: {', '.join(dict.fromkeys(bad))}")


def check_urls(urls: list[str]) -> None:
    """Raise ValueError for a link that is not a full http(s) address with a host (a stray '[' from copy-paste
    used to make every later run crash in urlsplit)."""
    for u in urls:
        try:
            p = urlsplit(u)
            ok = p.scheme.lower() in ("http", "https") and bool(p.hostname)
            p.port  # noqa: B018 - raises ValueError for a bad port ("https://shop.ro:abc/")
        except ValueError:
            ok = False
        if not ok:
            shown = u if len(u) <= 80 else u[:77] + "..."
            raise ValueError(f"Некорректная ссылка: {shown} — нужна полная ссылка на страницу товара, "
                             "например https://www.emag.ro/...")


def watch_from_dict(d: dict, origin: str = "config", *, validate: bool = True) -> Watch:
    """Build a Watch from config.yaml / Telegram data.
    `validate` (default) also rejects Cyrillic words and malformed links. Pass validate=False when re-loading a watch
    that was already accepted and stored earlier, so state written by older versions keeps loading."""
    if not isinstance(d, dict):
        raise ValueError(f"пункт watch должен быть словарём (name: ..., query: ...), а не {d!r}")
    d = dict(d)
    aliases = {"include": "must_include", "must": "must_include", "not": "exclude", "max": "max_price",
               "min": "min_price", "discount": "min_discount", "url": "urls", "skip": "skip_stores"}
    for a, real in aliases.items():
        if a in d and real not in d:
            d[real] = d.pop(a)
    for k in ("must_include", "any_of", "exclude", "urls", "skip_stores", "extra_queries"):
        if k in d:
            d[k] = _as_list(d[k])
    for k in ("stores", "categories"):
        if k in d and d[k] is not None:
            d[k] = _as_list(d[k]) or None
    for k in ("min_price", "max_price", "min_discount", "drop_pct"):
        v = d.get(k)
        if v is None:
            continue
        n = _num(v.rstrip().rstrip("%")) if isinstance(v, str) else float(v)   # "4 800", "5к", "4800 lei"
        if n is None:
            raise ValueError(f"«{k}»: нужно число, например 4800 (было: {v})")
        d[k] = n
    if d.get("query") is not None:
        d["query"] = str(d["query"])
    if d.get("id") is not None:
        d["id"] = str(d["id"])
    if not d.get("name"):
        d["name"] = d.get("query") or (d.get("urls") or ["watch"])[0]
    d["name"] = str(d["name"])
    unknown = set(d) - _WATCH_FIELDS
    if unknown:
        raise ValueError(f"watch '{d.get('name')}': неизвестные поля {sorted(unknown)}")
    if validate:
        if isinstance(d.get("query"), str) and not d["query"].strip():
            d["query"] = None          # an empty query used to match every offer
        check_latin(d)
        check_urls(d.get("urls") or [])
    d["origin"] = origin
    return Watch(**d)


def load_config(path: str) -> Config:
    if not os.path.exists(path):
        return Config(Settings(), [])
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    settings = Settings.from_dict(raw.get("settings") or {})
    watches: list[Watch] = []
    explicit: list[bool] = []          # the watch has its own `id:` in config.yaml
    for n, wd in enumerate(raw.get("watches") or [], 1):
        try:
            watches.append(watch_from_dict(wd))
        except ValueError as e:
            raise ValueError(f"config.yaml, пункт {n} в watches: {e}") from e
        explicit.append(isinstance(wd, dict) and wd.get("id") not in (None, ""))
    ids = [w.id for w, ex in zip(watches, explicit) if ex]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"config.yaml: повторяющиеся id у watch: {dup}")
    # ids made from names may coincide ("iPhone 16" / "iphone-16"): number them like /add does, in file order
    taken = set(ids)
    for w, ex in zip(watches, explicit):
        if ex:
            continue
        base, k = w.id, 2
        while w.id in taken:
            w.id = f"{base}-{k}"
            k += 1
        taken.add(w.id)
    return Config(settings, watches)


# --------------------------------------------------------------------------- compact syntax
# /add iphone 16 pro 256gb <4800 >3000 -max -resigilat +256gb %15 @emag,altex #electronics !all
#   <N / до N / max=N        max price          >N / от N / min=N   min price
#   %N / скидка N            min discount %     -word / -"two words"  exclude
#   +word / +"two words"     must include       @a,b  only these stores   ^a,b skip stores
#   #cat                     categories         !all  include out-of-stock   ~  loose title match
#   ?a|b|c                   any of             http(s)://...  product URL to watch
# Prices as people type them: 4800, 4.800, 4,800, "до 4 800", 3200,50, 5к / 5k / "5 тыс", with "lei"/"лей" after.
# Bare "max"/"min" are NOT price words: "iphone 16 pro max 256" is a model name ("max=4800" is the English form).

_TOKEN_RE = re.compile(r'([+\-]?"[^"]+"|\S+)')
_QUOTES_RE = re.compile(r"[“”„‟«»]")          # iOS smart quotes / Russian keyboard quotes -> '"'
_CURRENCY = {"lei", "ron", "lej", "лей", "леев", "лея", "леи"}
_CYR_CURRENCY = {"лей", "леев", "лея", "леи"}
_CURRENCY_SUFFIX_RE = re.compile(r"(?:lei|ron|lej|лей|леев|лея|леи)\.?$", re.IGNORECASE)
_MULT_RE = re.compile(r"(?:к|k|тыс\.?|тысяч[аи]?)$", re.IGNORECASE)
_MULT_WORDS = {"к", "k", "тыс", "тыс.", "тысяч", "тысячи", "тысяча"}
_MAX_WORDS = {"до", "<", "<=", "≤"}
_MIN_WORDS = {"от", ">", ">=", "≥"}
_DISCOUNT_WORDS = {"скидка", "скидкой", "скидки", "скидку", "discount"}
_GLUED_DISCOUNT_RE = re.compile(r"(?:скидк[аиуой]{1,2}|discount)[:=]?(\d+(?:[.,]\d+)?)%?")   # "скидка20", "скидка:20%"
_LIST_CONT_RE = re.compile(r",*(?:[a-z][a-z0-9_.\-]*,*)*")           # store / category names after "@emag,"
_BARE_LINK_RE = re.compile(r"(?:www\.|[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.(?:ro|com|eu|net|org|md)/)\S*")
_GROUP_RE = re.compile(r"\d{3}(?:[.,]\d{1,2})?(?:lei|ron|lej|лей|леев|лея|леи)?\.?", re.IGNORECASE)
_BARE_PRICE_RE = re.compile(r"\d[\d.,]*(?:lei|ron|lej|лей|леев|лея|леи)\.?", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d.,]*")


def _num(s: str) -> Optional[float]:
    """Number as typed in a command: 4800, 4.800 / 4,800 (thousands), 3200,50 / 1.299,99 (decimals),
    5к / 5k / 5тыс (x1000), optionally followed by lei/ron/лей."""
    s = str(s).strip().lower().replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    s = _CURRENCY_SUFFIX_RE.sub("", s)
    mult = 1.0
    m = _MULT_RE.search(s)
    if m and m.start() > 0:
        mult, s = 1000.0, s[:m.start()]
    if not _NUMBER_RE.fullmatch(s):
        return None
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s):
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):          # "3,200" is 3200 lei, not 3.2
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s) * mult
    except ValueError:
        return None


def _take_number(toks: list[str], j: int, first: Optional[str] = None) -> tuple[Optional[float], int]:
    """Read a price that starts at toks[j] (`first` = the part after a glued '<'/'>'): a thousands group typed
    after a space is joined ('до 4 800' -> 4800, '<3 200' -> 3200), then a separate 'к'/'тыс' multiplier and a
    currency word are consumed. Only called right after a price operator, so query words are never merged.
    Returns (value, index of the next unread token); value None when it is not a number."""
    s = toks[j] if first is None else first
    k = j + 1
    # "до 500 512" is 500 lei and the word "512", so a group follows 1-2 digits only, or is "000" ("до 120 000")
    while (k < len(toks) and _GROUP_RE.fullmatch(toks[k]) and re.fullmatch(r"\d+", s)
           and (len(s) <= 2 or toks[k].startswith("000"))):
        s += toks[k]
        k += 1
    v = _num(s)
    if v is None:
        return None, j
    if k < len(toks) and toks[k].lower() in _MULT_WORDS and not _MULT_RE.search(s):
        v *= 1000
        k += 1
    if k < len(toks) and toks[k].lower() in _CURRENCY | {c + "." for c in _CURRENCY}:
        k += 1
    return v, k


def _price_error(op: str) -> ValueError:
    return ValueError(f"После «{op}» нужна цена числом, например: до 4800, от 3000, скидка 20")


def parse_watch_spec(text: str) -> dict:
    spec: dict[str, Any] = {}
    query_words: list[str] = []
    text = _QUOTES_RE.sub('"', text or "")
    toks = _TOKEN_RE.findall(text.strip())
    prev_numeric_word = False          # the previous token became a query word that is a plain number
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        key = low.rstrip(":")
        after_number_word, prev_numeric_word = prev_numeric_word, False
        if low.startswith(("http://", "https://")):
            spec.setdefault("urls", []).append(t)
        elif _BARE_LINK_RE.fullmatch(low):
            spec.setdefault("urls", []).append("https://" + t)        # "www.emag.ro/..." copied without https://
        elif "=" in t and t.split("=", 1)[0].lower() in {
            "max", "min", "discount", "stores", "skip", "exclude", "include", "any_of", "categories", "drop_pct", "name"
        }:
            k, v = t.split("=", 1)
            k = k.lower()
            if k in ("max", "min", "discount", "drop_pct"):
                fk = {"max": "max_price", "min": "min_price", "discount": "min_discount"}.get(k, k)
                if v.strip():
                    n, j = _take_number(toks, i, first=v.rstrip("%"))     # "max=4 800", "max=5k lei"
                    if n is None:
                        raise ValueError(f"Не понял число в «{t}». Пример: max=4800")
                    spec[fk] = n
                    i = j
                    continue
                n, j = _take_number(toks, i + 1) if i + 1 < len(toks) else (None, i)
                if n is not None:                                          # "max= 4800"
                    spec[fk] = n
                    i = j
                    continue
                spec[fk] = None                                            # "max=" clears the limit (/set)
            elif k in ("stores", "skip", "exclude", "include", "any_of", "categories"):
                fk = {"skip": "skip_stores", "include": "must_include"}.get(k, k)
                spec.setdefault(fk, []).extend(_as_list(v))
            else:
                spec["name"] = v
        elif key in _MAX_WORDS or key in _MIN_WORDS:
            if i + 1 >= len(toks):
                raise _price_error(t)
            nt = toks[i + 1]
            if key in _MIN_WORDS and nt.endswith("%") and _num(nt[:-1]) is not None:   # "от 25%" = discount
                spec["min_discount"] = _num(nt[:-1])
                i += 2
                continue
            v, j = _take_number(toks, i + 1)
            if v is None:
                raise _price_error(t)
            spec["max_price" if key in _MAX_WORDS else "min_price"] = v
            i = j
            continue
        elif _GLUED_DISCOUNT_RE.fullmatch(low):
            spec["min_discount"] = _num(_GLUED_DISCOUNT_RE.fullmatch(low).group(1))
        elif key in _DISCOUNT_WORDS:
            j = i + 1
            if j < len(toks) and toks[j].lower() in ("от", ">", ">=", "≥", "from"):   # "скидка от 25%"
                j += 1
            d = _num(toks[j].rstrip("%")) if j < len(toks) else None
            if d is None:
                raise ValueError(f"После «{t}» нужен процент скидки, например: скидка 20")
            spec["min_discount"] = d
            j += 1
            if j < len(toks) and toks[j] == "%":
                j += 1
            i = j
            continue
        elif t[0] in "<>≤≥" and len(t) > 1:
            is_max = t[0] in "<≤"
            rest = t[1:].lstrip("=")
            if not is_max and rest.endswith("%") and _num(rest[:-1]) is not None:     # ">20%" = discount
                spec["min_discount"] = _num(rest[:-1])
            else:
                v, j = _take_number(toks, i, first=rest)
                if v is None:
                    raise ValueError(f"Не понял цену «{t}». Пример: <4800 или >3000")
                spec["max_price" if is_max else "min_price"] = v
                i = j
                continue
        elif (t.startswith("%") and _num(t[1:]) is not None) or (t.endswith("%") and _num(t[:-1]) is not None):
            spec["min_discount"] = _num(t.strip("%"))
        elif re.fullmatch(r"-\d+(?:[.,]\d+)?%", t):                                     # "-20%" = discount
            spec["min_discount"] = _num(t[1:-1])
        elif t.startswith("-") and len(t) > 1:
            spec.setdefault("exclude", []).append(t[1:].strip('"'))                     # "-128" excludes 128 too
        elif t.startswith("+") and len(t) > 1:
            spec.setdefault("must_include", []).append(t[1:].strip('"'))
        elif t[0] in "@^#" and len(t) > 1:
            val = t[1:]
            # "@emag, altex" / "@emag ,altex": the comma continues the list over the space
            while (i + 1 < len(toks) and (val.endswith(",") or toks[i + 1].startswith(","))
                   and _LIST_CONT_RE.fullmatch(toks[i + 1].lower())):     # not "@emag, до 3000"
                i += 1
                val += toks[i]
            field_ = {"@": "stores", "^": "skip_stores", "#": "categories"}[t[0]]
            spec.setdefault(field_, []).extend(_as_list(val.lower()))
        elif t.startswith("?") and len(t) > 1:
            spec.setdefault("any_of", []).extend([x.strip('"') for x in t[1:].split("|") if x.strip('"')])
        elif low == "!all":
            spec["in_stock"] = False
        elif low == "~":
            spec["strict"] = False
        elif (after_number_word and low.rstrip(".") in _CURRENCY) or _BARE_PRICE_RE.fullmatch(low):
            # "4800 lei" / "4800 лей" without "до"/"от": the number would become a mandatory title word
            shown = f"{query_words[-1]} {t}" if after_number_word else t
            raise ValueError(f"Не понял «{shown}». Цену пишите так: до 4800 (максимальная) или от 3000 (минимальная)")
        elif low.rstrip(".") in _CYR_CURRENCY:
            pass                                                   # "лей" is never part of a product name
        else:
            w = t.strip('"')
            query_words.append(w)
            prev_numeric_word = bool(_NUMBER_RE.fullmatch(w))
        i += 1
    if query_words:
        spec["query"] = " ".join(query_words)
    check_latin(spec)
    return spec
