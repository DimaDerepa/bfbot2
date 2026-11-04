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
    for k in ("must_include", "any_of", "exclude", "urls", "skip_stores"):
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

_TOKEN_RE = re.compile(r'([+\-]?"[^"]+"|\S+)')


def parse_watch_spec(text: str) -> dict:
    spec: dict[str, Any] = {}
    query_words: list[str] = []
    toks = _TOKEN_RE.findall(text.strip())
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        nxt = toks[i + 1] if i + 1 < len(toks) else None

        def num(s: str) -> Optional[float]:
            s = s.replace(" ", "").replace("лей", "").replace("lei", "").replace("ron", "")
            s = s.replace(".", "").replace(",", ".") if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s) else s.replace(",", ".")
            try:
                return float(s)
            except ValueError:
                return None

        if low.startswith(("http://", "https://")):
            spec.setdefault("urls", []).append(t)
        elif "=" in t and t.split("=", 1)[0].lower() in {
            "max", "min", "discount", "stores", "skip", "exclude", "include", "any_of", "categories", "drop_pct", "name"
        }:
            k, v = t.split("=", 1)
            k = k.lower()
            if k in ("max", "min", "discount", "drop_pct"):
                spec[{"max": "max_price", "min": "min_price", "discount": "min_discount"}.get(k, k)] = num(v)
            elif k in ("stores", "skip", "exclude", "include", "any_of", "categories"):
                key = {"skip": "skip_stores", "include": "must_include"}.get(k, k)
                spec.setdefault(key, []).extend(_as_list(v))
            else:
                spec["name"] = v
        elif low in ("до", "max", "<=") and nxt and num(nxt) is not None:
            spec["max_price"] = num(nxt)
            i += 1
        elif low in ("от", "min", ">=") and nxt and num(nxt) is not None:
            spec["min_price"] = num(nxt)
            i += 1
        elif low in ("скидка", "discount") and nxt and num(nxt.rstrip("%")) is not None:
            spec["min_discount"] = num(nxt.rstrip("%"))
            i += 1
        elif t.startswith("<") and num(t.lstrip("<=")) is not None:
            spec["max_price"] = num(t.lstrip("<="))
        elif t.startswith(">") and num(t.lstrip(">=")) is not None:
            spec["min_price"] = num(t.lstrip(">="))
        elif (t.startswith("%") and num(t[1:]) is not None) or (t.endswith("%") and num(t[:-1]) is not None):
            spec["min_discount"] = num(t.strip("%"))
        elif t.startswith("-") and len(t) > 1 and not t[1:].replace(".", "").isdigit():
            spec.setdefault("exclude", []).append(t[1:].strip('"'))
        elif t.startswith("+") and len(t) > 1:
            spec.setdefault("must_include", []).append(t[1:].strip('"'))
        elif t.startswith("@") and len(t) > 1:
            spec.setdefault("stores", []).extend(_as_list(t[1:].lower()))
        elif t.startswith("^") and len(t) > 1:
            spec.setdefault("skip_stores", []).extend(_as_list(t[1:].lower()))
        elif t.startswith("#") and len(t) > 1:
            spec.setdefault("categories", []).extend(_as_list(t[1:].lower()))
        elif t.startswith("?") and len(t) > 1:
            spec.setdefault("any_of", []).extend([x for x in t[1:].split("|") if x])
        elif low == "!all":
            spec["in_stock"] = False
        elif low == "~":
            spec["strict"] = False
        else:
            query_words.append(t.strip('"'))
        i += 1
    if query_words:
        spec["query"] = " ".join(query_words)
    return spec
